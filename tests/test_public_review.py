"""Focused public-page regressions for visitor cookies and delivered-file authorization."""

import httpx

from app.main import app
from app.models import Conversation, Message
from app.repositories import Repository
from app.services.conversations import ConversationService


async def publish_agent(client, name):
    response = await client.post("/api/agents", json={"name": name, "model": "test-model"})
    assert response.status_code == 201
    agent_id = response.json()["id"]
    assert (await client.put("/api/credentials/openai", json={"api_key": "test-provider-key"})).status_code == 200
    assert (await client.post(f"/api/agents/{agent_id}/publish")).status_code == 200
    return agent_id


async def test_one_browser_keeps_distinct_visitor_identity_for_each_agent(client, monkeypatch):
    first = await publish_agent(client, "Primero")
    second = await publish_agent(client, "Segundo")
    received = []

    async def chat(self, agent_id, data):
        received.append((agent_id, data.user_id, data.channel))
        return {"conversation_id": "review-conversation", "response": "Hola", "conversation_status": "ACTIVE"}

    monkeypatch.setattr(ConversationService, "chat", chat)
    async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="http://test") as visitor:
        for agent_id in (first, second):
            response = await visitor.get(f"/public/agents/{agent_id}")
            assert response.status_code == 200
            assert f"Path=/public/agents/{agent_id}" in response.headers["set-cookie"]
            assert "HttpOnly" in response.headers["set-cookie"]
            assert "SameSite=strict" in response.headers["set-cookie"]
        for agent_id in (first, second, first):
            response = await visitor.post(f"/public/agents/{agent_id}/chat", headers={"X-Chat-Request": "1"}, json={"message": "Hola"})
            assert response.status_code == 200, response.text
    assert received[0] == received[2]
    assert received[0][1] != received[1][1]
    assert all(item[1].startswith("visitor:") and item[2] == "web" for item in received)


async def test_download_requires_own_web_conversation_and_recorded_delivery(client, database):
    agent_id = await publish_agent(client, "Archivos")
    base = f"/public/agents/{agent_id}"
    uploaded = await client.post(f"/api/agents/{agent_id}/files", files={"file": ("manual.txt", b"Contenido autorizado", "text/plain")})
    assert uploaded.status_code == 201
    file_id = uploaded.json()["id"]
    async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="http://test") as visitor:
        assert (await visitor.get(base)).status_code == 200
        cookie = next(cookie for cookie in visitor.cookies.jar if cookie.name == "agente_visitor")
        user_id = "visitor:" + cookie.value.split(".")[1]
        async with database() as session:
            own = await Repository(session, Conversation).add(agent_id=agent_id, user_id=user_id, channel="web")
            other_channel = await Repository(session, Conversation).add(agent_id=agent_id, user_id=user_id, channel="playground")
            other_user = await Repository(session, Conversation).add(agent_id=agent_id, user_id="visitor:someone-else", channel="web")
            for conversation in (other_channel, other_user):
                await Repository(session, Message).add(conversation_id=conversation.id, role="assistant", content="Archivo", attachments=[{"id": file_id}])
            await session.commit()
            own_id, wrong_channel_id, other_id = own.id, other_channel.id, other_user.id
        download = f"{base}/files/{file_id}"
        for forbidden in (wrong_channel_id, other_id):
            response = await visitor.get(download, params={"conversation_id": forbidden})
            assert response.status_code == 403
        assert (await visitor.get(download, params={"conversation_id": own_id})).status_code == 404
        async with database() as session:
            await Repository(session, Message).add(conversation_id=own_id, role="user", content="Quiero este archivo", attachments=[{"id": file_id}])
            await session.commit()
        assert (await visitor.get(download, params={"conversation_id": own_id})).status_code == 404
        async with database() as session:
            await Repository(session, Message).add(conversation_id=own_id, role="assistant", content="Aquí está", attachments=[{"id": file_id}])
            await session.commit()
        response = await visitor.get(download, params={"conversation_id": own_id})
        assert response.status_code == 200
        assert response.content == b"Contenido autorizado"
        assert response.headers["content-disposition"].startswith("attachment;")
        assert response.headers["content-type"] == "application/octet-stream"
        assert response.headers["cache-control"] == "no-store"
        async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="http://test") as stranger:
            assert (await stranger.get(download, params={"conversation_id": own_id})).status_code == 401
            assert (await stranger.get(base)).status_code == 200
            assert (await stranger.get(download, params={"conversation_id": own_id})).status_code == 403
        assert (await client.delete(f"/api/agents/{agent_id}/publish")).status_code == 200
        assert (await visitor.get(download, params={"conversation_id": own_id})).status_code == 404
