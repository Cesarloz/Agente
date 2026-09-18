"""Regresiones de consumo y flujo: todos los modelos/canales son dobles locales."""

import asyncio
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock

import pytest
from sqlalchemy import select

from app import worker
from app.models import Conversation, Job, Question
from app.repositories import Repository
from app.schemas import AgentConfig, ChatInput, IntegrationInput
from app.services.agents import AgentService
from app.services.connections import IntegrationService
from app.services.conversations import ConversationService
from app.services.knowledge import FAQService, KnowledgeService
from app.services.runtime_hooks import Answer, ApplicationRuntimeHooks, ScopeResult, ToolPlan


def fake_model(monkeypatch, *, scope="IN_SCOPE", invalid_stage=None):
    """Ejercita structured real, sustituyendo únicamente el transporte y la recuperación."""
    schemas = (ScopeResult, ToolPlan, Answer)
    parsed = (ScopeResult(scope_status=scope), ToolPlan(calls=[]), Answer(response="Respuesta local", answered=True))
    counters = ((3, 1), (5, 2), (8, 3))
    adapters = {}
    for schema, value, (input_tokens, output_tokens) in zip(schemas, parsed, counters):
        raw = SimpleNamespace(usage_metadata={
            "input_tokens": input_tokens,
            "output_tokens": output_tokens,
            "total_tokens": input_tokens + output_tokens,
        })
        output = {"raw": raw, "parsed": None if invalid_stage == schema.__name__ else value}
        adapters[schema.__name__] = SimpleNamespace(ainvoke=AsyncMock(return_value=output))
    model = MagicMock()
    model.with_structured_output.side_effect = lambda schema, **kwargs: adapters[schema.__name__]
    monkeypatch.setattr(ApplicationRuntimeHooks, "model", AsyncMock(return_value=model))
    monkeypatch.setattr(KnowledgeService, "retrieve", AsyncMock(return_value=[]))
    return adapters


def check_usage(result, expected_stages, expected_tokens):
    """Verifica que la API exponga intentos, sumas y datos por etapa coherentes."""
    usage = result["usage"]
    assert usage["llm_calls"] == len(expected_stages)
    assert [event["stage"] for event in usage["stages"]] == expected_stages
    assert usage["total_tokens"] == expected_tokens
    assert usage["input_tokens"] + usage["output_tokens"] == expected_tokens
    assert usage["usage_complete"] is True


async def test_chat_api_reports_usage_and_persists_each_stage_in_trace(client, database, monkeypatch):
    adapters = fake_model(monkeypatch)
    created = await client.post("/api/agents", json={
        "name": "Auditoría LLM", "model": "local-test", "scope_description": "Soporte",
        "tools_enabled": ["current_time"],
    })
    assert created.status_code == 201
    response = await client.post(f"/api/agents/{created.json()['id']}/chat", json={"message": "¿Cómo funciona?"})
    assert response.status_code == 200
    result = response.json()
    assert result["error"] is None
    check_usage(result, ["ScopeResult", "ToolPlan", "Answer"], 22)
    assert all(adapter.ainvoke.await_count == 1 for adapter in adapters.values())
    expected = {"ScopeGuard": "ScopeResult", "ToolDecision": "ToolPlan", "LLM": "Answer"}
    async with database() as session:
        question = await session.scalar(select(Question))
        assert question.tokens == 22
        assert question.trace == result["trace"]
        for node, stage in expected.items():
            event = next(event for event in question.trace if event["node"] == node)
            assert event["llm_usage"]["stage"] == stage
            assert event["llm_usage"]["total_tokens"] > 0


@pytest.mark.parametrize("scope,policy", [("IN_SCOPE", "WARN"), ("OUT_OF_SCOPE", "WARN"), ("OUT_OF_SCOPE", "CLOSE")])
async def test_scope_cost_remains_visible_when_faq_or_policy_skips_generation(session, monkeypatch, scope, policy):
    adapters = fake_model(monkeypatch, scope=scope)
    agent = await AgentService(session).create(AgentConfig(
        name="Corte temprano", model="local-test", scope_description="Solo soporte", out_of_scope_policy=policy,
    ))
    await FAQService(session).save(agent["id"], {"question": "Horario", "answer": "De nueve a cinco"})
    result = await ConversationService(session).chat(agent["id"], ChatInput(message="Horario"))
    check_usage(result, ["ScopeResult"], 4)
    adapters["ScopeResult"].ainvoke.assert_awaited_once()
    adapters["ToolPlan"].ainvoke.assert_not_awaited()
    adapters["Answer"].ainvoke.assert_not_awaited()
    question = await session.scalar(select(Question))
    await session.refresh(question)
    assert question.tokens == 4
    scope_trace = next(event for event in question.trace if event["node"] == "ScopeGuard")
    assert scope_trace["llm_usage"]["total_tokens"] == 4
    assert question.answered is (scope == "IN_SCOPE")


@pytest.mark.parametrize("stage,nodes,total", [
    ("ScopeResult", ["ScopeResult"], 4),
    ("ToolPlan", ["ScopeResult", "ToolPlan"], 11),
    ("Answer", ["ScopeResult", "ToolPlan", "Answer"], 22),
])
async def test_invalid_structured_result_preserves_paid_usage_without_retry(session, monkeypatch, stage, nodes, total):
    adapters = fake_model(monkeypatch, invalid_stage=stage)
    agent = await AgentService(session).create(AgentConfig(
        name="Parseo fallido", model="local-test", scope_description="Soporte", tools_enabled=["current_time"],
    ))
    result = await ConversationService(session).chat(agent["id"], ChatInput(message="Ayuda"))
    assert result["error"] is not None
    assert result["conversation_status"] == "ERROR"
    check_usage(result, nodes, total)
    assert sum(adapter.ainvoke.await_count for adapter in adapters.values()) == len(nodes)
    question = await session.scalar(select(Question))
    await session.refresh(question)
    assert question.tokens == total
    failed = next(event for event in question.trace if event["status"] == "error")
    assert failed["llm_usage"]["stage"] == stage
    assert failed["llm_usage"]["usage_reported"] is True


async def test_unreported_transport_failure_is_counted_as_unknown_not_zero_cost(session, monkeypatch):
    adapters = fake_model(monkeypatch)
    adapters["Answer"].ainvoke.side_effect = TimeoutError("No provider response")
    agent = await AgentService(session).create(AgentConfig(name="Transporte", model="local-test"))
    result = await ConversationService(session).chat(agent["id"], ChatInput(message="Ayuda"))
    assert result["error"] is not None
    assert result["usage"]["llm_calls"] == 1
    assert result["usage"]["usage_complete"] is False
    assert result["usage"]["stages"][0]["usage_reported"] is False
    adapters["Answer"].ainvoke.assert_awaited_once()
    question = await session.scalar(select(Question))
    await session.refresh(question)
    failed = next(event for event in question.trace if event["node"] == "LLM")
    assert failed["llm_usage"]["usage_reported"] is False


async def test_global_timeout_preserves_known_cost_and_interrupted_stage_in_audit(session, monkeypatch):
    """La cancelación del grafo conserva el coste anterior y el intento aún sin respuesta."""
    adapters = fake_model(monkeypatch)
    answer_started = asyncio.Event()

    async def never_finishes(messages):
        answer_started.set()
        await asyncio.Event().wait()

    adapters["Answer"].ainvoke.side_effect = never_finishes
    actual_wait_for = asyncio.wait_for

    async def short_runtime_timeout(awaitable, timeout):
        if timeout != 150:
            return await actual_wait_for(awaitable, timeout=timeout)
        task = asyncio.create_task(awaitable)
        try:
            # Sincroniza la cancelación con la etapa que se verifica, sin depender de la velocidad del equipo.
            await actual_wait_for(answer_started.wait(), timeout=10)
            return await actual_wait_for(task, timeout=0)
        finally:
            if not task.done():
                task.cancel()
            await asyncio.gather(task, return_exceptions=True)

    monkeypatch.setattr("app.services.conversations.asyncio.wait_for", short_runtime_timeout)
    agent = await AgentService(session).create(AgentConfig(
        name="Timeout global", model="local-test", scope_description="Soporte",
    ))
    result = await ConversationService(session).chat(agent["id"], ChatInput(message="Ayuda"))
    assert answer_started.is_set()
    assert result["error"] is not None
    assert result["usage"]["llm_calls"] == 2
    assert result["usage"]["total_tokens"] == 4
    assert result["usage"]["usage_complete"] is False
    adapters["ScopeResult"].ainvoke.assert_awaited_once()
    adapters["Answer"].ainvoke.assert_awaited_once()
    question = await session.scalar(select(Question))
    await session.refresh(question)
    assert question.tokens == 4
    assert question.trace == result["trace"]
    assert [event["node"] for event in question.trace] == ["ScopeGuard", "LLM"]
    assert all(event["status"] == "interrupted" for event in question.trace)
    assert all(event["duration_ms"] is None and event["duration_known"] is False for event in question.trace)
    assert question.trace[0]["llm_usage"]["usage_reported"] is True
    assert question.trace[1]["llm_usage"]["usage_reported"] is False


async def test_channel_retry_reuses_committed_response_after_adapter_failure(database, monkeypatch):
    """Falla antes de enviar, luego reintenta entrega sin volver a ejecutar el agente."""
    adapters = fake_model(monkeypatch)
    monkeypatch.setattr(worker, "Session", database)
    channel = SimpleNamespace(send_text=AsyncMock(return_value={"ok": True}))
    adapter = AsyncMock(side_effect=[RuntimeError("Adapter temporarily unavailable"), channel])
    monkeypatch.setattr(IntegrationService, "adapter", adapter)
    async with database() as session:
        agent = await AgentService(session).create(AgentConfig(name="Entrega", model="local-test"))
        integration = await IntegrationService(session).save(agent["id"], IntegrationInput(
            channel="telegram", enabled=True, secrets={"bot_token": "test-local-token", "webhook_secret": "test-local-secret"},
        ))
        job = await Repository(session, Job).add(kind="channel", payload={
            "agent_id": agent["id"], "integration_id": integration["id"], "channel": "telegram",
            "user_id": "7", "recipient": "7", "text": "Hola",
        })
        await session.commit()
        job_id = job.id
    await worker.process(job_id)
    async with database() as session:
        job = await session.get(Job, job_id)
        assert job.status == "PENDING"
        assert job.payload["response_result"]["response"] == "Respuesta local"
        assert job.payload["response_result"]["usage"]["total_tokens"] == 11
        assert not job.payload.get("send_in_progress")
    await worker.process(job_id)
    adapters["Answer"].ainvoke.assert_awaited_once()
    channel.send_text.assert_awaited_once_with("7", "Respuesta local")
    async with database() as session:
        job = await session.get(Job, job_id)
        assert job.status == "DONE"
        assert job.payload["text_sent"] is True
        questions = list(await session.scalars(select(Question)))
        assert len(questions) == 1 and questions[0].tokens == 11
        conversation = await session.scalar(select(Conversation))
        assert conversation.interaction_count == 1
