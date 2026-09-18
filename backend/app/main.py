from contextlib import asynccontextmanager

from cryptography.fernet import Fernet
from fastapi import FastAPI, Request
from fastapi.exceptions import RequestValidationError
from fastapi.responses import JSONResponse
from sqlalchemy import text

from app.api import router, webhooks
from app.auth import initialize_auth, router as auth_router
from app.config import get_settings
from app.db import Session, engine
from app.errors import AppError
from app.public import router as public_router


@asynccontextmanager
async def lifespan(app):
    settings = get_settings()
    if len(settings.admin_token) < 32:
        raise RuntimeError("ADMIN_TOKEN debe tener al menos 32 caracteres")
    Fernet(settings.encryption_key.encode())
    settings.storage_dir.mkdir(parents=True, exist_ok=True)
    initialize_auth(settings)
    yield
    await engine.dispose()


app = FastAPI(title="Agente · API", version="0.1.0", lifespan=lifespan)
app.include_router(router)
app.include_router(webhooks)
app.include_router(auth_router)
app.include_router(public_router)


@app.exception_handler(AppError)
async def application_error(request: Request, error: AppError):
    return JSONResponse({"detail": error.message}, status_code=error.status)


@app.exception_handler(RequestValidationError)
async def validation_error(request: Request, error: RequestValidationError):
    # Pydantic input/context fields may contain secrets; never echo request bodies.
    errors = [{"loc": e["loc"], "msg": e["msg"], "type": e["type"]} for e in error.errors()]
    return JSONResponse({"detail": errors}, status_code=422)


@app.exception_handler(Exception)
async def unexpected_error(request: Request, error: Exception):
    return JSONResponse({"detail": "Error interno; consulta el estado del servicio"}, status_code=500)


@app.get("/health")
async def health():
    try:
        async with Session() as session:
            await session.execute(text("SELECT 1"))
        return {"status": "ok", "database": "ok"}
    except Exception:
        return JSONResponse({"status": "unavailable", "database": "unavailable"}, status_code=503)
