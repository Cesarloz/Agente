from app import worker
from app.models import Job
from app.repositories import Repository
from app.schemas import AgentConfig, IntegrationInput
from app.services.agents import AgentService
from app.services.connections import IntegrationService


async def setup_channel(session):
    agent = await AgentService(session).create(AgentConfig(name="Worker"))
    integration = await IntegrationService(session).save(agent["id"], IntegrationInput(channel="telegram", enabled=True, secrets={"bot_token": "123:fake", "webhook_secret": "secret"}))
    job = await Repository(session, Job).add(kind="channel", payload={"agent_id": agent["id"], "integration_id": integration["id"], "channel": "telegram", "user_id": "7", "recipient": "7", "text": "Hola", "response_result": {"response": "Hola también", "attachments": []}})
    await session.commit()
    return job.id


async def test_ambiguous_send_requires_review_never_blind_retry(database, monkeypatch):
    monkeypatch.setattr(worker, "Session", database)
    calls = []
    class Adapter:
        async def send_text(self, recipient, text):
            calls.append(text)
            raise TimeoutError("Provider may already have accepted the request")
    async def adapter(self, integration):
        return Adapter()
    monkeypatch.setattr(IntegrationService, "adapter", adapter)
    async with database() as session:
        id = await setup_channel(session)
    await worker.process(id)
    async with database() as session:
        job = await session.get(Job, id)
        assert job.status == "NEEDS_REVIEW" and job.payload["send_in_progress"]
    await worker.process(id)
    assert len(calls) == 1


async def test_successful_send_is_recorded_once(database, monkeypatch):
    monkeypatch.setattr(worker, "Session", database)
    calls = []
    class Adapter:
        async def send_text(self, recipient, text):
            calls.append(text)
            return {"ok": True}
    async def adapter(self, integration):
        return Adapter()
    monkeypatch.setattr(IntegrationService, "adapter", adapter)
    async with database() as session:
        id = await setup_channel(session)
    await worker.process(id)
    await worker.process(id)
    async with database() as session:
        job = await session.get(Job, id)
        assert job.status == "DONE" and job.payload["text_sent"]
    assert calls == ["Hola también"]


async def test_missing_projection_is_obsolete_not_retried(database, monkeypatch):
    monkeypatch.setattr(worker, "Session", database)
    async with database() as session:
        job = await Repository(session, Job).add(kind="delete_document", payload={"agent_id": "deleted", "id": "missing"})
        await session.commit()
        id = job.id
    await worker.process(id)
    async with database() as session:
        assert (await session.get(Job, id)).status == "DONE"
