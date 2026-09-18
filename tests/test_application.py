import json

from sqlalchemy import select

from app.models import Credential, Document, FAQ, Job, Question
from app.schemas import AgentConfig, ChatInput, FAQInput, IntegrationInput
from app.services.agents import AgentService
from app.services.connections import IntegrationService
from app.services.conversations import ConversationService, MemoryService
from app.services.credentials import CredentialService
from app.services.jobs import JobService
from app.services.knowledge import DocumentService, FAQService, KnowledgeService
from app.services.runtime_hooks import ApplicationRuntimeHooks
from app.services.webhooks import WebhookService
import pytest


async def create(client, name="Soporte"):
    response = await client.post("/api/agents", json={"name": name})
    assert response.status_code == 201, response.text
    return response.json()


async def test_auth_and_agent_prompt_versioning(client):
    assert (await client.get("/api/agents", headers={"Authorization": "Bearer wrong"})).status_code == 401
    agent = await create(client)
    id = agent["id"]
    original = (await client.get(f"/api/agents/{id}/prompts")).json()[0]
    saved = await client.post(f"/api/agents/{id}/prompts", json={"body": "Nueva versión"})
    assert saved.status_code == 200
    assert (await client.get(f"/api/agents/{id}")).json()["system_prompt"] == "Nueva versión"
    await client.post(f"/api/agents/{id}/prompts/{original['id']}/restore")
    assert (await client.get(f"/api/agents/{id}")).json()["system_prompt"] == original["body"]
    bad = await client.patch(f"/api/agents/{id}", json={"chunk_overlap": 2000, "chunk_size": 100})
    assert bad.status_code == 422
    assert (await client.patch(f"/api/agents/{id}", json={"unknown": 1})).status_code == 422


async def test_credentials_encrypted_and_never_returned(client, database):
    secret = "test-key-is-never-a-real-key"
    response = await client.put("/api/credentials/openai", json={"api_key": secret})
    assert response.status_code == 200
    assert secret not in response.text
    assert secret not in (await client.get("/api/credentials")).text
    async with database() as session:
        row = await session.scalar(select(Credential))
        assert secret not in row.ciphertext
        assert await CredentialService(session).get(row.name) == secret
    invalid = await client.put("/api/credentials/openai", json={"api_key": {"nested": secret}})
    assert invalid.status_code == 422 and secret not in invalid.text


async def test_faq_crud_cross_agent_and_csv_atomicity(client, database):
    a, b = await create(client), await create(client, "Otro")
    route = f"/api/agents/{a['id']}/faqs"
    faq = (await client.post(route, json={"question": "=formula", "answer": "Respuesta", "tags": ["envíos"]})).json()
    assert faq["status"] == "PENDING"
    assert (await client.patch(f"/api/agents/{b['id']}/faqs/{faq['id']}", json={"answer": "No"})).status_code == 404
    exported = await client.get(route + "/export")
    assert "'=formula" in exported.text
    imported = await client.post(f"/api/agents/{b['id']}/faqs/import", files={"file": ("faqs.csv", exported.content, "text/csv")})
    assert imported.json()["imported"] == 1
    assert (await client.get(f"/api/agents/{b['id']}/faqs")).json()[0]["question"] == "=formula"
    invalid = b"question,answer\ngood,yes\nbad,\n"
    assert (await client.post(route + "/import", files={"file": ("bad.csv", invalid)})).status_code == 400
    assert len((await client.get(route)).json()) == 1
    await client.delete(route + "/" + faq["id"])
    assert (await client.get(route)).json()[0]["status"] == "DELETING"
    async with database() as session:
        assert len(list(await session.scalars(select(Job)))) == 3


async def test_knowledge_and_deliverable_file_separation(client, database):
    a, b = await create(client), await create(client, "Otro")
    base = f"/api/agents/{a['id']}"
    document = await client.post(base + "/documents", files={"file": ("../manual.txt", b"Solo conocimiento", "text/plain")})
    assert document.status_code == 202
    assert document.json()["name"] == "manual.txt"
    file = (await client.post(base + "/files", files={"file": ("manual.txt", b"Archivo para usuario", "text/plain")})).json()
    assert file["kind"] == "DELIVERABLE" and "path" not in file
    assert len((await client.get(base + "/documents")).json()) == 1
    assert len((await client.get(base + "/files")).json()) == 1
    assert (await client.get(f"/api/agents/{b['id']}/files/{file['id']}/download")).status_code == 404
    assert (await client.get(base + f"/files/{file['id']}/download")).content == b"Archivo para usuario"
    assert (await client.post(base + "/documents", files={"file": ("bad.exe", b"bad")})).status_code == 400
    assert (await client.post(base + "/documents/url", json={"url": "https://127.0.0.1/private"})).status_code == 400


@pytest.mark.parametrize("raw", [
    b"question,answer\nValid,Answer\nInvalid,Answer,extra\n",
    b"question,answer\nValid,Answer\nMissing answer\n",
    b'question,answer\nValid,Answer\n"Unclosed,Answer\n',
])
async def test_faq_csv_malformed_rows_return_validation_error_atomically(client, raw):
    agent = await create(client)
    route = f"/api/agents/{agent['id']}/faqs"
    response = await client.post(route + "/import", files={"file": ("invalid.csv", raw, "text/csv")})
    assert response.status_code == 400
    assert (await client.get(route)).json() == []


async def test_faq_csv_missing_optional_cells_use_defaults(client):
    agent = await create(client)
    route = f"/api/agents/{agent['id']}/faqs"
    raw = b"question,answer,tags,priority,file_id,active\nShort row,Answer\nEmpty cells,Answer,,,,\n"
    response = await client.post(route + "/import", files={"file": ("optional.csv", raw, "text/csv")})
    assert response.status_code == 200
    assert response.json() == {"imported": 2}
    rows = (await client.get(route)).json()
    assert len(rows) == 2
    assert all(row["tags"] == [] and row["priority"] == 0 and row["file_id"] is None and row["active"] for row in rows)


class FakeHooks:
    tokens = 12
    def __init__(self, *args):
        pass
    async def classify_scope(self, state):
        return "OUT_OF_SCOPE" if "fuera" in state["question"] else "IN_SCOPE"
    async def retrieve_faqs(self, state):
        return [{"page_content": "Respuesta", "metadata": {"source_id": "faq-id"}}]
    async def retrieve_documents(self, state):
        return []
    async def decide_tools(self, state):
        return []
    async def execute_tool(self, call, state):
        return {}
    async def generate(self, state):
        return {"response": "Respuesta de prueba", "answered": True, "tokens": 12}


async def test_memory_counts_audit_and_closed_conversation(session):
    agent = await AgentService(session).create(AgentConfig(name="Límites", max_interactions=2))
    service = ConversationService(session, FakeHooks)
    first = await service.chat(agent["id"], ChatInput(message="Primera"))
    data = ChatInput(message="Segunda", conversation_id=first["conversation_id"])
    second = await service.chat(agent["id"], data)
    assert second["conversation_status"] == "CLOSED"
    third = await service.chat(agent["id"], data)
    assert third["interaction_count"] == 2 and third["conversation_status"] == "CLOSED"
    questions = list(await session.scalars(select(Question).order_by(Question.created_at)))
    assert len(questions) == 3
    assert questions[0].faq_used and questions[0].tokens == 12 and questions[0].answered
    assert not questions[-1].answered
    await MemoryService(session).clear(agent["id"])
    assert (await service.get(first["conversation_id"]))["messages"] == []
    assert (await service.chat(agent["id"], data))["interaction_count"] == 2
    with pytest.raises(Exception) as error:
        await service.chat(agent["id"], ChatInput(message="Ataque", conversation_id=first["conversation_id"], user_id="another"))
    assert error.value.status == 403


async def test_http_chat_scope_analytics_conversion(client, monkeypatch):
    for name in ("classify_scope", "retrieve_faqs", "retrieve_documents", "decide_tools", "execute_tool", "generate"):
        monkeypatch.setattr(ApplicationRuntimeHooks, name, getattr(FakeHooks, name))
    a = await create(client)
    id = a["id"]
    await client.patch(f"/api/agents/{id}", json={"out_of_scope_policy": "CLOSE"})
    response = (await client.post(f"/api/agents/{id}/chat", json={"message": "fuera de alcance"})).json()
    assert response["conversation_status"] == "CLOSED"
    assert response["scope_status"] == "OUT_OF_SCOPE"
    analytics = (await client.get("/api/analytics")).json()
    assert analytics["total_questions"] == analytics["unanswered"] == analytics["out_of_scope"] == 1
    question = (await client.get("/api/questions", params={"unanswered": True})).json()[0]
    assert (await client.post(f"/api/questions/{question['id']}/faq", json={})).status_code == 400
    assert (await client.post(f"/api/questions/{question['id']}/faq", json={"answer": "Respuesta revisada"})).status_code == 200
    graph = (await client.get("/api/runtime/graph")).json()["mermaid"]
    assert "ScopeGuard" in graph and "DatabaseTool" in graph


async def test_document_and_faq_indexing_with_injected_embeddings(session, monkeypatch):
    from langchain_core.embeddings import DeterministicFakeEmbedding
    stores = {}
    class Vector:
        async def upsert(self, agent_id, kind, docs, ids, embedding, profile):
            for id, doc in zip(ids, docs):
                stores[(agent_id, kind, profile, id)] = doc
        async def delete(self, agent_id, kind, ids, embedding, profile):
            for id in ids:
                stores.pop((agent_id, kind, profile, id), None)
    async def embedding(self, p):
        return DeterministicFakeEmbedding(size=8)
    monkeypatch.setattr(KnowledgeService, "vectors", lambda self: Vector())
    monkeypatch.setattr(KnowledgeService, "embedding", embedding)
    a = await AgentService(session).create(AgentConfig(name="Índices"))
    doc = await DocumentService(session).upload(a["id"], "test.txt", ("Texto de manual. " * 100).encode())
    await DocumentService(session).index(a["id"], doc["id"])
    stored = await session.get(Document, doc["id"])
    assert stored.status == "INDEXED" and stored.chunk_count > 1
    faq = await FAQService(session).save(a["id"], FAQInput(question="Horario", answer="De lunes a viernes").model_dump())
    await FAQService(session).index(a["id"], faq["id"])
    assert (await session.get(FAQ, faq["id"])).status == "INDEXED"
    await FAQService(session).save(a["id"], {"active": False}, faq["id"])
    await FAQService(session).index(a["id"], faq["id"])
    assert (await session.get(FAQ, faq["id"])).status == "INACTIVE"
    assert all(key[1] == "documents" for key in stores)


async def test_webhook_persistence_duplicates_and_private_chats(session):
    a = await AgentService(session).create(AgentConfig(name="Telegram"))
    await IntegrationService(session).save(a["id"], IntegrationInput(channel="telegram", enabled=True, secrets={"bot_token": "fake", "webhook_secret": "test-secret"}))
    payload = {"update_id": 42, "message": {"message_id": 12, "from": {"id": 7}, "chat": {"id": 7, "type": "private"}, "text": "Hola"}}
    service = WebhookService(session)
    result = await service.receive(a["id"], "telegram", json.dumps(payload).encode(), "test-secret")
    assert result["accepted"] == 1
    assert (await service.receive(a["id"], "telegram", json.dumps(payload).encode(), "test-secret"))["accepted"] == 0
    payload["message"]["chat"]["type"] = "group"
    payload["update_id"] = 43
    assert (await service.receive(a["id"], "telegram", json.dumps(payload).encode(), "test-secret"))["accepted"] == 0
    jobs = list(await session.scalars(select(Job)))
    assert len(jobs) == 1 and jobs[0].status == "PENDING"
    jobs[0].status = "NEEDS_REVIEW"
    jobs[0].payload = {**jobs[0].payload, "send_in_progress": True}
    await session.commit()
    with pytest.raises(Exception) as error:
        await JobService(session).retry(jobs[0].id)
    assert error.value.status == 409
    assert (await JobService(session).retry(jobs[0].id, allow_resend=True))["status"] == "PENDING"
    assert not jobs[0].payload["send_in_progress"]
