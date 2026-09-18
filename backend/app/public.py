"""Public browser chat, without exposing administrative configuration or bearer keys."""
import asyncio
from contextlib import closing
import hashlib
import hmac
from pathlib import Path
import secrets
import sqlite3
import time
from typing import Annotated
from urllib.parse import urlsplit

from fastapi import APIRouter, Depends, Header, Query, Request, Response
from fastapi.responses import FileResponse, StreamingResponse
from pydantic import Field
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from app.config import get_settings
from app.db import get_session
from app.errors import AppError
from app.models import Conversation, File, Message
from app.repositories import Repository
from app.schemas import ChatInput, Input
from app.services.conversations import ConversationService
from app.services.knowledge import FileService
from app.services.publication import PublicationService

router = APIRouter()
SessionDep = Annotated[AsyncSession, Depends(get_session)]
STATIC = Path(__file__).parent / "static"
COOKIE_NAME = "agente_visitor"
SESSION_SECONDS = 24 * 60 * 60
PUBLIC_HEADERS = {"Cache-Control": "no-store", "X-Content-Type-Options": "nosniff", "Referrer-Policy": "no-referrer"}


class PublicChatInput(Input):
    message: str = Field(min_length=1, max_length=20000)
    conversation_id: str | None = Field(default=None, max_length=36)


def signature(payload):
    key = get_settings().encryption_key
    if not key:
        raise AppError("Servicio no configurado", 503)
    return hmac.new(key.encode(), f"public-chat:{payload}".encode(), hashlib.sha256).hexdigest()


def visitor(request: Request, agent_id: str):
    token = request.cookies.get(COOKIE_NAME, "")
    if len(token) > 256:
        return None
    try:
        owner, user, expires, signed = token.split(".")
        payload = f"{owner}.{user}.{expires}"
        if owner != agent_id or len(user) != 48 or int(expires) <= time.time():
            return None
        if not hmac.compare_digest(signed, signature(payload)):
            return None
        return "visitor:" + user
    except (ValueError, UnicodeError):
        return None


def require_visitor(request, agent_id):
    user = visitor(request, agent_id)
    if not user:
        raise AppError("Tu sesión ha caducado. Recarga la página e inicia una nueva conversación", 401)
    return user


def check_chat_origin(request, marker):
    # A custom header + JSON prevent cross-site form submission. No CORS is enabled.
    if marker != "1":
        raise AppError("Abre la página del agente para conversar", 403)
    origin = request.headers.get("origin")
    configured = urlsplit(get_settings().public_base_url)
    allowed = {f"{configured.scheme}://{configured.netloc}", f"{request.url.scheme}://{request.url.netloc}"}
    if origin and origin not in allowed:
        raise AppError("Origen de la solicitud no permitido", 403)


def consume_chat_quota(agent_id: str, address: str):
    """Fixed minute budgets shared by API processes and retained over restarts."""
    folder = get_settings().storage_dir / ".public"
    folder.mkdir(parents=True, exist_ok=True, mode=0o700)
    window = int(time.time() // 60)
    ip_hash = hashlib.sha256(address.encode()).hexdigest()
    budgets = [(f"ip:{agent_id}:{ip_hash}", 30), (f"agent:{agent_id}", 120)]
    with closing(sqlite3.connect(folder / "rate.sqlite3", timeout=10)) as db, db:
        db.execute("CREATE TABLE IF NOT EXISTS quota (key TEXT, window INTEGER, count INTEGER, PRIMARY KEY(key, window))")
        db.execute("BEGIN IMMEDIATE")
        db.execute("DELETE FROM quota WHERE window < ?", (window,))
        for key, limit in budgets:
            row = db.execute("SELECT count FROM quota WHERE key=? AND window=?", (key, window)).fetchone()
            if row and row[0] >= limit:
                raise AppError("Se alcanzó el límite de mensajes. Espera un minuto e inténtalo de nuevo", 429)
        for key, _ in budgets:
            db.execute("INSERT INTO quota VALUES (?, ?, 1) ON CONFLICT(key, window) DO UPDATE SET count=count+1", (key, window))


@router.get("/chat/{agent_id}", include_in_schema=False)
async def chat_page(agent_id: str, session: SessionDep):
    await PublicationService(session).public_agent(agent_id)
    return FileResponse(STATIC / "chat.html", headers={
        **PUBLIC_HEADERS,
        "Content-Security-Policy": "default-src 'none'; script-src 'self'; style-src 'self'; img-src 'self' https: http:; connect-src 'self'; base-uri 'none'; frame-ancestors 'none'; form-action 'self'",
        "X-Frame-Options": "DENY",
    })


@router.get("/public/assets/{filename}", include_in_schema=False)
async def chat_asset(filename: str):
    if filename not in {"chat.js", "chat.css", "links.js", "stream.js"}:
        raise AppError("Archivo no encontrado", 404)
    return FileResponse(STATIC / filename, headers={"X-Content-Type-Options": "nosniff", "Cache-Control": "public, max-age=300"})


@router.get("/public/agents/{agent_id}")
async def public_info(agent_id: str, request: Request, response: Response, session: SessionDep):
    agent = await PublicationService(session).public_agent(agent_id)
    response.headers.update(PUBLIC_HEADERS)
    if not visitor(request, agent_id):
        payload = f"{agent.id}.{secrets.token_hex(24)}.{int(time.time()) + SESSION_SECONDS}"
        response.set_cookie(COOKIE_NAME, f"{payload}.{signature(payload)}", httponly=True, samesite="strict",
                            secure=request.url.scheme == "https" or get_settings().public_base_url.startswith("https://"),
                            max_age=SESSION_SECONDS, path=f"/public/agents/{agent.id}")
    return {"id": agent.id, "name": agent.name, "description": agent.config.get("description", ""),
            "avatar": agent.config.get("avatar", "🤖"), "welcome_message": agent.config.get("welcome_message", ""),
            "response_links": agent.config.get("response_links", []),
            "logo_url": f"/public/agents/{agent.id}/logo" if agent.logo_filename else None}


@router.get("/public/agents/{agent_id}/logo")
async def public_logo(agent_id: str, session: SessionDep):
    service = PublicationService(session)
    await service.public_agent(agent_id)
    return FileResponse(await service.logo(agent_id), media_type="image/png", headers=PUBLIC_HEADERS)


@router.post("/public/agents/{agent_id}/chat")
async def public_chat(agent_id: str, data: PublicChatInput, request: Request, response: Response, session: SessionDep,
                      x_chat_request: Annotated[str | None, Header()] = None):
    check_chat_origin(request, x_chat_request)  # Verifica que el envío provenga del chat autorizado.
    user_id = require_visitor(request, agent_id)  # Identidad del servidor; no confía en un user_id del navegador.
    await PublicationService(session).public_agent(agent_id)  # Solo permite conversar con un agente publicado.
    # Aplica cuota antes del trabajo de recuperación y generación potencialmente facturable.
    await asyncio.to_thread(consume_chat_quota, agent_id, request.client.host if request.client else "unknown")
    chat_input = ChatInput(message=data.message, conversation_id=data.conversation_id, user_id=user_id, channel="web")
    if "application/x-ndjson" in request.headers.get("accept", ""):
        from app.services.chat_stream import chat_events
        # El productor posee su sesión: desconectar el HTTP no cierra la transacción antes de guardar el consumo.
        factory = async_sessionmaker(session.bind, expire_on_commit=False)

        async def finish(stream_session, result):
            return await public_chat_result(stream_session, agent_id, result)

        return StreamingResponse(chat_events(factory, agent_id, chat_input, finish), media_type="application/x-ndjson",
                                 headers={**PUBLIC_HEADERS, "X-Accel-Buffering": "no"})
    # Reutiliza el mismo servicio/grafo del Playground con memoria privada y canal web.
    result = await ConversationService(session).chat(agent_id, chat_input)
    response.headers.update(PUBLIC_HEADERS)
    return await public_chat_result(session, agent_id, result)


async def public_chat_result(session, agent_id, result):
    """Misma salida pública para JSON y streaming: nunca expone trazas ni claves."""
    attachments = []
    for attachment in result.get("attachments", []):
        file_id = attachment.get("id")
        if not file_id:
            continue
        row = await session.scalar(select(File).where(File.id == file_id, File.agent_id == agent_id, File.kind == "DELIVERABLE"))
        if row:
            attachments.append({"name": row.name, "url": f"/public/agents/{agent_id}/files/{row.id}?conversation_id={result['conversation_id']}"})
    return {"conversation_id": result["conversation_id"], "response": result["response"],
            "conversation_status": result["conversation_status"], "error": result.get("error"), "attachments": attachments}


@router.get("/public/agents/{agent_id}/files/{file_id}")
async def public_file(agent_id: str, file_id: str, request: Request, session: SessionDep,
                      conversation_id: Annotated[str, Query(max_length=36)]):
    user_id = require_visitor(request, agent_id)
    await PublicationService(session).public_agent(agent_id)
    conversation = await Repository(session, Conversation).get(conversation_id, agent_id)
    if conversation.channel != "web" or conversation.user_id != user_id:
        raise AppError("Archivo no disponible en esta conversación", 403)
    delivered = await session.scalars(select(Message.attachments).where(Message.conversation_id == conversation.id, Message.role == "assistant"))
    if not any(any(item.get("id") == file_id for item in (items or [])) for items in delivered):
        raise AppError("Archivo no entregado en esta conversación", 404)
    row = await Repository(session, File).get(file_id, agent_id)
    if row.kind != "DELIVERABLE":
        raise AppError("Archivo no disponible", 404)
    return FileResponse(FileService.checked_path(row.path), filename=row.name, media_type="application/octet-stream", headers=PUBLIC_HEADERS)
