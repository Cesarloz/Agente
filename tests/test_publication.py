from io import BytesIO
from pathlib import Path

import httpx
from PIL import Image
import pytest
from sqlalchemy import select

from app.config import get_settings
from app.main import app
from app.models import Conversation, Message
from app.public import consume_chat_quota
from app.errors import AppError
from app.services.conversations import ConversationService


def png():
    output = BytesIO()
    Image.new("RGB", (32, 24), "blue").save(output, "PNG")
    return output.getvalue()


async def create(client, **values):
    result = await client.post("/api/agents", json={"name": "Asistente", **values})
    assert result.status_code == 201, result.text
    return result.json()["id"]


async def published(client, **values):
    agent_id = await create(client, model="test-model", **values)
    await client.put("/api/credentials/openai", json={"api_key": "test-only-provider-key"})
    result = await client.post(f"/api/agents/{agent_id}/publish")
    assert result.status_code == 200, result.text
    return agent_id


@pytest.fixture
async def visitor(client):
    # client fixture installs the same database override. This client has no admin token.
    async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="http://test") as browser:
        yield browser


async def test_publication_requires_configuration_and_remains_explicit(client, visitor):
    agent_id = await create(client)
    base = f"/api/agents/{agent_id}"
    assert (await visitor.get(f"/chat/{agent_id}")).status_code == 404
    assert (await client.post(base + "/publish")).status_code == 409
    await client.patch(base, json={"model": "test-model"})
    assert (await client.post(base + "/publish")).status_code == 409
    await client.put("/api/credentials/openai", json={"api_key": "test-only-key"})
    await client.patch(base, json={"active": False})
    assert (await client.post(base + "/publish")).status_code == 409
    await client.patch(base, json={"active": True})
    result = await client.post(base + "/publish")
    assert result.json()["public_url"] == f"{get_settings().public_base_url}/chat/{agent_id}"
    assert (await visitor.get(f"/chat/{agent_id}")).status_code == 200
    # Generic config edits cannot overwrite protected publication or branding metadata.
    assert (await client.patch(base, json={"published": True})).status_code == 422
    assert (await client.patch(base, json={"logo_filename": "../secret"})).status_code == 422
    await client.patch(base, json={"description": "Actualizada"})
    assert (await client.get(base)).json()["published"] is True
    assert (await client.delete(base + "/publish")).json()["published"] is False
    assert (await visitor.get(f"/chat/{agent_id}")).status_code == 404
    assert (await visitor.get(f"/public/agents/{agent_id}")).status_code == 404


async def test_public_page_has_assets_and_no_private_configuration(client, visitor):
    agent_id = await published(client, system_prompt="PRIVATE SYSTEM PROMPT", description="Ayuda al cliente")
    page = await visitor.get(f"/chat/{agent_id}")
    assert page.status_code == 200 and 'id="chat-form"' in page.text
    assert "frame-ancestors 'none'" in page.headers["content-security-policy"]
    for asset in ("chat.js", "chat.css"):
        assert (await visitor.get(f"/public/assets/{asset}")).status_code == 200
    assert (await visitor.get("/public/assets/chat.html")).status_code == 404
    public = await visitor.get(f"/public/agents/{agent_id}")
    assert public.json()["description"] == "Ayuda al cliente"
    assert "system_prompt" not in public.text and "test-only-provider-key" not in public.text
    assert "HttpOnly" in public.headers["set-cookie"] and "SameSite=strict" in public.headers["set-cookie"]
    assert (await visitor.get("/api/agents")).status_code == 401
    assert (await visitor.post(f"/api/agents/{agent_id}/publish")).status_code == 401


async def test_logo_validation_storage_publication_and_replacement(client, visitor):
    agent_id = await create(client)
    base = f"/api/agents/{agent_id}"
    route = base + "/logo"
    assert (await visitor.post(route, files={"file": ("x.png", png(), "image/png")})).status_code == 401
    for content in (b"<svg onload='alert(1)'></svg>", b"not-an-image"):
        result = await client.post(route, files={"file": ("logo.png", content, "image/png")})
        assert result.status_code == 422
    too_large = await client.post(route, files={"file": ("large.jpg", b"x" * (2 * 1024 * 1024 + 1))})
    assert too_large.status_code == 413
    content = png() + b"<script>appended payload</script>"
    uploaded = await client.post(route, files={"file": ("../../logo.png", content, "image/png")})
    assert uploaded.json()["logo_url"] == route
    assert "logo_filename" not in uploaded.text
    saved = await client.get(route)
    assert saved.headers["content-type"] == "image/png" and b"script" not in saved.content
    assert Image.open(BytesIO(saved.content)).size == (32, 24)
    assert (await visitor.get(f"/public/agents/{agent_id}/logo")).status_code == 404
    assert (await client.get(base + "/files")).json() == []
    await client.patch(base, json={"name": "Nuevo nombre", "model": "test-model"})
    assert (await client.get(base)).json()["logo_url"] == route
    await client.put("/api/credentials/openai", json={"api_key": "test-only-key"})
    await client.post(base + "/publish")
    assert (await visitor.get(f"/public/agents/{agent_id}/logo")).content == saved.content
    await client.post(route, files={"file": ("new.png", png(), "image/png")})
    assert len(list((get_settings().storage_dir / "logos").iterdir())) == 1
    assert (await client.delete(route)).json()["logo_url"] is None
    assert (await visitor.get(f"/public/agents/{agent_id}/logo")).status_code == 404
    assert list((get_settings().storage_dir / "logos").iterdir()) == []


async def test_public_chat_uses_real_runtime_and_isolates_visitors(client, visitor, database, monkeypatch):
    from tests.test_application import FakeHooks
    # Keep real runtime, persistence, guards and ownership checks; replace external providers.
    original = ConversationService.__init__
    monkeypatch.setattr(ConversationService, "__init__", lambda self, session: original(self, session, FakeHooks))
    agent_id = await published(client)
    base = f"/public/agents/{agent_id}"
    assert (await visitor.post(base + "/chat", json={"message": "Hola"}, headers={"X-Chat-Request": "1"})).status_code == 401
    await visitor.get(base)
    assert (await visitor.post(base + "/chat", json={"message": "Hola"})).status_code == 403
    bad_origin = await visitor.post(base + "/chat", json={"message": "Hola"}, headers={"X-Chat-Request": "1", "Origin": "https://evil.invalid"})
    assert bad_origin.status_code == 403
    result = await visitor.post(base + "/chat", json={"message": "Hola"}, headers={"X-Chat-Request": "1"})
    assert result.status_code == 200, result.text
    data = result.json()
    assert data["response"] and not data["error"] and "trace" not in data
    async with database() as session:
        conversation = await session.get(Conversation, data["conversation_id"])
        assert conversation.channel == "web" and conversation.user_id.startswith("visitor:")
        assert len(list(await session.scalars(select(Message)))) == 2
    second = await visitor.post(base + "/chat", json={"message": "Gracias", "conversation_id": data["conversation_id"]}, headers={"X-Chat-Request": "1"})
    assert second.status_code == 200
    async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="http://test") as stranger:
        await stranger.get(base)
        stolen = await stranger.post(base + "/chat", json={"message": "Continúa", "conversation_id": data["conversation_id"]}, headers={"X-Chat-Request": "1"})
        assert stolen.status_code == 403
    forged = await visitor.post(base + "/chat", json={"message": "Hola", "user_id": "admin", "channel": "api"}, headers={"X-Chat-Request": "1"})
    assert forged.status_code == 422
    await client.delete(f"/api/agents/{agent_id}/publish")
    assert (await visitor.post(base + "/chat", json={"message": "Otra"}, headers={"X-Chat-Request": "1"})).status_code == 404


async def test_deactivation_hides_already_published_page(client, visitor):
    agent_id = await published(client)
    await client.patch(f"/api/agents/{agent_id}", json={"active": False})
    assert (await visitor.get(f"/chat/{agent_id}")).status_code == 404


async def test_public_chat_rate_limit_cannot_be_reset_by_new_cookie(database):
    for _ in range(30):
        consume_chat_quota("agent", "same-ip")
    with pytest.raises(AppError) as error:
        consume_chat_quota("agent", "same-ip")
    assert error.value.status == 429
    consume_chat_quota("other-agent", "same-ip")


async def test_missing_or_unsafe_base_url_cannot_be_published(client, monkeypatch):
    agent_id = await create(client, model="test-model")
    await client.put("/api/credentials/openai", json={"api_key": "test-key"})
    for bad in ("javascript:alert(1)", "https://user:pass@example.com", "https://example.com/subpath"):
        monkeypatch.setattr(get_settings(), "public_base_url", bad)
        assert (await client.post(f"/api/agents/{agent_id}/publish")).status_code == 409


def test_chat_static_files_are_packaged():
    import tomllib
    package = tomllib.loads(Path("pyproject.toml").read_text())
    assert "static/*.html" in package["tool"]["setuptools"]["package-data"]["app"]
