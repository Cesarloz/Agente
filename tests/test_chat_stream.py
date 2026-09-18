"""Verifica el protocolo streaming sin proveedores, conexiones SQL ni canales reales."""

import asyncio
import json
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest

from app.errors import AppError
from app.services import chat_stream


class TrackedSession:
    """Sesión de prueba que permite observar propiedad, commit y cierre asíncrono."""

    def __init__(self):
        self.open = False
        self.closed = False
        self.committed = False
        self.audit_saved = False

    async def __aenter__(self):
        self.open = True
        return self

    async def __aexit__(self, *exception):
        await asyncio.sleep(0)
        self.open = False
        self.closed = True


def install_chat(monkeypatch, scenario):
    """Inyecta un servicio controlable, manteniendo el productor y la cola reales."""
    sessions, producers = [], []

    def session_factory():
        session = TrackedSession()
        sessions.append(session)
        return session

    def service(session):
        async def chat(agent_id, data, on_response):
            producers.append(asyncio.current_task())
            assert session.open
            return await scenario(session, agent_id, data, on_response)

        return SimpleNamespace(chat=chat)

    monkeypatch.setattr(chat_stream, "ConversationService", service)
    return session_factory, sessions, producers


def decoded(line):
    assert line.endswith("\n")
    assert len(line.splitlines()) == 1
    return json.loads(line)


async def collect(stream):
    return [decoded(line) async for line in stream]


async def test_incremental_text_precedes_final_result_and_session_survives_until_finalize(monkeypatch):
    continue_chat, finish_finalize = asyncio.Event(), asyncio.Event()
    data = object()

    async def scenario(session, agent_id, received, emit):
        assert agent_id == "agent" and received is data
        await emit("Ho")
        await continue_chat.wait()
        await emit("Hola\n¡Qué tal!")
        session.committed = True
        return {"response": "Hola\n¡Qué tal!", "attachments": [{"id": "file"}]}

    factory, sessions, producers = install_chat(monkeypatch, scenario)

    async def finalize(session, result):
        assert session is sessions[0] and session.open and session.committed
        await finish_finalize.wait()
        return {**result, "attachments": [{"id": "file", "url": "/public/files/file"}]}

    stream = chat_stream.chat_events(factory, "agent", data, finalize)
    assert sessions == []
    try:
        assert decoded(await anext(stream)) == {"type": "text", "text": "Ho"}
        assert len(sessions) == 1 and sessions[0].open and not sessions[0].committed
        continue_chat.set()
        assert decoded(await anext(stream)) == {"type": "text", "text": "Hola\n¡Qué tal!"}
        assert sessions[0].open and sessions[0].committed
        finish_finalize.set()
        final = decoded(await anext(stream))
        assert final == {"type": "done", "result": {
            "response": "Hola\n¡Qué tal!", "attachments": [{"id": "file", "url": "/public/files/file"}],
        }}
        assert sessions[0].closed
        with pytest.raises(StopAsyncIteration):
            await anext(stream)
    finally:
        await stream.aclose()
    assert all(task.done() for task in producers)


async def test_slow_consumer_receives_latest_snapshot_without_an_unbounded_backlog(monkeypatch):
    begin_burst, burst_done, finish_chat = asyncio.Event(), asyncio.Event(), asyncio.Event()

    async def scenario(session, agent_id, data, emit):
        await emit("Inicial")
        await begin_burst.wait()
        for index in range(1000):
            await emit(f"Acumulado {index}")
        burst_done.set()
        await finish_chat.wait()
        session.committed = True
        return {"response": "Acumulado 999"}

    factory, sessions, producers = install_chat(monkeypatch, scenario)
    stream = chat_stream.chat_events(factory, "agent", None, AsyncMock(side_effect=lambda session, result: result))
    try:
        assert decoded(await anext(stream))["text"] == "Inicial"
        begin_burst.set()
        await asyncio.wait_for(burst_done.wait(), 1)
        assert decoded(await anext(stream)) == {"type": "text", "text": "Acumulado 999"}
        finish_chat.set()
        assert (await collect(stream)) == [{"type": "done", "result": {"response": "Acumulado 999"}}]
    finally:
        await stream.aclose()
    assert sessions[0].closed and all(task.done() for task in producers)


@pytest.mark.parametrize("stage", ["chat", "finalize"])
@pytest.mark.parametrize("public_error", [False, True])
async def test_errors_are_terminal_sanitized_and_close_the_owned_session(monkeypatch, stage, public_error):
    error = AppError("El agente está desactivado", 409) if public_error else RuntimeError("SECRET_TOKEN=do-not-expose")

    async def scenario(session, agent_id, data, emit):
        if stage == "chat":
            raise error
        session.committed = True
        return {"response": "Guardada"}

    factory, sessions, producers = install_chat(monkeypatch, scenario)
    finalize = AsyncMock(side_effect=error if stage == "finalize" else None)
    events = await collect(chat_stream.chat_events(factory, "agent", None, finalize))
    assert events == [{
        "type": "error", "status": 409 if public_error else 500,
        "message": "El agente está desactivado" if public_error else chat_stream.STREAM_ERROR,
    }]
    assert "SECRET_TOKEN" not in json.dumps(events)
    assert sessions[0].closed and all(task.done() for task in producers)
    assert finalize.await_count == int(stage == "finalize")


async def test_disconnect_closes_generator_after_cancellation_audit_and_leaves_no_producer(monkeypatch):
    cancellation_seen, finish_audit = asyncio.Event(), asyncio.Event()

    async def scenario(session, agent_id, data, emit):
        await emit("Parcial")
        try:
            await asyncio.Event().wait()
        except asyncio.CancelledError:
            cancellation_seen.set()
            await finish_audit.wait()
            session.audit_saved = True
            raise

    factory, sessions, producers = install_chat(monkeypatch, scenario)
    finalize = AsyncMock()
    stream = chat_stream.chat_events(factory, "agent", None, finalize)
    assert decoded(await anext(stream))["type"] == "text"
    closing = asyncio.create_task(stream.aclose())
    try:
        await asyncio.wait_for(cancellation_seen.wait(), 1)
        assert not closing.done() and sessions[0].open
        finish_audit.set()
        await asyncio.wait_for(closing, 1)
    finally:
        finish_audit.set()
        await closing
    assert sessions[0].audit_saved and sessions[0].closed
    assert producers[0].done() and producers[0].cancelled()
    finalize.assert_not_awaited()


async def test_cancelling_a_consumer_waiting_for_text_cancels_producer_and_closes_session(monkeypatch):
    started = asyncio.Event()

    async def scenario(session, agent_id, data, emit):
        started.set()
        await asyncio.Event().wait()

    factory, sessions, producers = install_chat(monkeypatch, scenario)
    stream = chat_stream.chat_events(factory, "agent", None, AsyncMock())
    tasks_before = asyncio.all_tasks()
    consumer = asyncio.create_task(anext(stream))
    await asyncio.wait_for(started.wait(), 1)
    consumer.cancel()
    with pytest.raises(asyncio.CancelledError):
        await consumer
    await stream.aclose()
    assert sessions[0].closed
    assert producers[0].done() and producers[0].cancelled()
    assert asyncio.all_tasks() <= tasks_before


async def test_service_cannot_suppress_disconnect_and_continue_to_finalize(monkeypatch):
    async def scenario(session, agent_id, data, emit):
        await emit("Parcial")
        try:
            await asyncio.Event().wait()
        except asyncio.CancelledError:
            session.audit_saved = True
            return {"response": "La cancelación fue absorbida"}

    factory, sessions, producers = install_chat(monkeypatch, scenario)
    finalize = AsyncMock()
    stream = chat_stream.chat_events(factory, "agent", None, finalize)
    await anext(stream)
    await asyncio.wait_for(stream.aclose(), 1)
    assert sessions[0].closed and sessions[0].audit_saved
    assert producers[0].done()
    finalize.assert_not_awaited()


async def test_heartbeat_waits_without_restart_and_then_emits_one_confirmed_result(monkeypatch):
    release = asyncio.Event()
    monkeypatch.setattr(chat_stream, "PING_SECONDS", 0.01)

    async def scenario(session, agent_id, data, emit):
        await release.wait()
        session.committed = True
        return {"response": "Lista"}

    factory, sessions, producers = install_chat(monkeypatch, scenario)
    stream = chat_stream.chat_events(factory, "agent", None, AsyncMock(side_effect=lambda session, result: result))
    try:
        assert decoded(await anext(stream)) == {"type": "ping"}
        assert len(sessions) == len(producers) == 1
        assert sessions[0].open and not sessions[0].committed
        release.set()
        events = await collect(stream)
        assert [event for event in events if event["type"] != "ping"] == [{"type": "done", "result": {"response": "Lista"}}]
    finally:
        await stream.aclose()
    assert sessions[0].closed and producers[0].done()


async def test_non_json_final_payload_becomes_sanitized_error_and_closes_session(monkeypatch):
    async def scenario(session, agent_id, data, emit):
        session.committed = True
        return {"response": "Guardada"}

    factory, sessions, producers = install_chat(monkeypatch, scenario)
    events = await collect(chat_stream.chat_events(factory, "agent", None, AsyncMock(return_value={"bad": object()})))
    assert events == [{"type": "error", "message": chat_stream.STREAM_ERROR, "status": 500}]
    assert sessions[0].closed and producers[0].done()
