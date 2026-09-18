"""Streaming visible y consumo: SDK simulados, sin sockets ni credenciales reales."""

import asyncio
import json
import socket
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock

import pytest
from langchain_core.messages import AIMessage, AIMessageChunk, HumanMessage
from langchain_core.outputs import ChatGenerationChunk

from app.runtime.answer_stream import AnswerStream, collect_answer_stream
from app.services.runtime_hooks import Answer, ApplicationRuntimeHooks, ScopeResult, ToolPlan


@pytest.fixture(autouse=True)
async def forbid_network(monkeypatch):
    def forbidden(*args, **kwargs):
        raise AssertionError("La prueba de streaming no puede abrir conexiones de red")

    monkeypatch.setattr(socket.socket, "connect", forbidden)
    monkeypatch.setattr(socket.socket, "connect_ex", forbidden)
    monkeypatch.setattr(socket, "create_connection", forbidden)
    monkeypatch.setattr(socket, "getaddrinfo", forbidden)
    yield  # Instala el bloqueo después de crear el socketpair interno del event loop de Windows.


def hooks_with(runnable, sink=None):
    hooks = ApplicationRuntimeHooks(None, "offline-agent", {})
    model = MagicMock()
    model.with_structured_output.return_value = runnable
    hooks.model = AsyncMock(return_value=model)
    hooks.on_response = sink
    return hooks


async def feed(observer, message):
    # El token deliberadamente es inutilizable: el observador debe inspeccionar el bloque tipado.
    await observer.on_llm_new_token("RAW_TOKEN_MUST_NOT_ESCAPE", chunk=ChatGenerationChunk(message=message))


async def test_text_snapshots_decode_escapes_and_split_unicode_without_json_leaks():
    snapshots = []

    async def sink(text):
        snapshots.append(text)
        text.encode("utf-8")  # Un surrogate a medio recibir nunca debe alcanzar el transporte.

    observer = AnswerStream(sink)
    answer = 'Hola "Ana"\nRuta C:\\datos 😀'
    wire = json.dumps({"response": answer, "answered": True}, ensure_ascii=True)
    for character in wire:  # Divide también comillas escapadas, barras y cada dígito del par Unicode.
        await feed(observer, AIMessageChunk(content=character))
    assert snapshots[-1] == answer
    assert all(current.startswith(previous) for previous, current in zip(snapshots, snapshots[1:]))
    assert len(snapshots) == len(set(snapshots))
    assert all(answer.startswith(snapshot) for snapshot in snapshots)


async def test_tool_arguments_continue_when_id_and_name_only_exist_in_first_chunk():
    sink = AsyncMock()
    observer = AnswerStream(sink)
    fragments = ['{"response":"Ho', 'la', '","answered":true}']
    for index, args in enumerate(fragments):
        message = AIMessageChunk(content="", tool_call_chunks=[{
            "name": "Answer" if index == 0 else None,
            "id": "provider-tool-id" if index == 0 else None,
            "index": 0,
            "args": args,
        }])
        await feed(observer, message)
    assert [call.args[0] for call in sink.await_args_list] == ["Ho", "Hola"]


async def test_thinking_reasoning_signatures_and_other_fields_are_never_published():
    sink = AsyncMock()
    observer = AnswerStream(sink)
    await feed(observer, AIMessageChunk(content=[
        {"type": "thinking", "thinking": '{"response":"private thought"}', "signature": "secret-signature"},
        {"type": "reasoning", "reasoning": '{"response":"private reasoning"}'},
        {"type": "text", "text": '{"response":"Visible","answered":true}'},
    ]))
    await observer.on_llm_new_token('{"response":"untyped raw token"}')
    assert [call.args[0] for call in sink.await_args_list] == ["Visible"]
    for content in ['{"answered":true}', '{"scope_status":"IN_SCOPE"}', '{"calls":[]}',
                    '{"response":{"secret":"object"}}', '{"response":["array"]}']:
        await feed(AnswerStream(sink), AIMessageChunk(content=content))
    assert sink.await_count == 1


@pytest.mark.parametrize("provider", ["openai", "gemini"])
async def test_actual_sdk_wrapper_emits_callback_before_end_with_one_request(monkeypatch, provider):
    from langchain_google_genai import ChatGoogleGenerativeAI
    from langchain_openai import ChatOpenAI

    cls = ChatOpenAI if provider == "openai" else ChatGoogleGenerativeAI
    state = {"calls": 0, "finished": False}
    observations = []

    async def fake_stream(self, messages, **kwargs):
        state["calls"] += 1
        for text in ['{"response":"Ho', 'la', '","answered":true}']:
            yield ChatGenerationChunk(message=AIMessageChunk(content=text))
            await asyncio.sleep(0)
        yield ChatGenerationChunk(message=AIMessageChunk(
            content="", additional_kwargs={"parsed": {"response": "Hola", "answered": True}},
            usage_metadata={"input_tokens": 11, "output_tokens": 7, "total_tokens": 18},
        ))
        state["finished"] = True

    async def no_nonstream_call(*args, **kwargs):
        raise AssertionError("No se permite una segunda generación ni fallback a ainvoke")

    async def sink(text):
        observations.append((text, state["finished"]))

    monkeypatch.setattr(cls, "_astream", fake_stream)
    monkeypatch.setattr(cls, "_agenerate", no_nonstream_call)
    if provider == "openai":
        model = cls(api_key="offline-placeholder", model="gpt-5", max_retries=0)
        assert model.stream_usage is True
    else:
        model = cls(google_api_key="offline-placeholder", model="gemini-2.5-flash", max_retries=1)
    hooks = ApplicationRuntimeHooks(None, "offline-agent", {})
    hooks._model = model
    hooks.on_response = sink
    result = await hooks.structured(Answer, [HumanMessage("Prueba sin red")])
    assert result == Answer(response="Hola", answered=True)
    assert state == {"calls": 1, "finished": True}
    assert observations == [("Ho", False), ("Hola", False)]
    assert hooks.tokens == 18
    assert hooks.usage[0]["usage_reported"] is True
    assert hooks.first_response_ms is not None and hooks.first_response_ms >= 0


async def test_collect_stream_combines_raw_fragments_and_keeps_latest_parsed():
    async def stream(messages, config):
        yield {"raw": AIMessageChunk(content='{"response":"Hola",',
                                    usage_metadata={"input_tokens": 10, "output_tokens": 0, "total_tokens": 10})}
        yield {"raw": AIMessageChunk(content='"answered":true}',
                                    usage_metadata={"input_tokens": 0, "output_tokens": 4, "total_tokens": 4})}
        yield {"parsing_error": None}
        yield {"parsed": Answer(response="Hola", answered=True)}

    output = await collect_answer_stream(SimpleNamespace(astream=stream), [], AnswerStream(AsyncMock()))
    assert output["raw"].content == '{"response":"Hola","answered":true}'
    assert output["raw"].usage_metadata["total_tokens"] == 14
    assert output["parsed"].response == "Hola" and output["parsing_error"] is None


async def test_sdk_without_early_callbacks_delivers_final_snapshot_without_new_request(monkeypatch):
    state = {"calls": 0}
    async def stream(messages, config):
        state["calls"] += 1
        yield {"raw": AIMessage(content='{"response":"Final","answered":true}',
                                usage_metadata={"input_tokens": 3, "output_tokens": 2, "total_tokens": 5}),
               "parsed": Answer(response="Final", answered=True), "parsing_error": None}

    sink = AsyncMock()
    monkeypatch.setattr("app.services.runtime_hooks.perf_counter", MagicMock(side_effect=[10, 10.25]))
    hooks = hooks_with(SimpleNamespace(astream=stream), sink)
    assert (await hooks.structured(Answer, [])).response == "Final"
    sink.assert_awaited_once_with("Final")
    assert hooks.first_response_ms == 250
    assert hooks.tokens == 5 and state["calls"] == 1


@pytest.mark.parametrize("schema,parsed", [(ScopeResult, ScopeResult(scope_status="IN_SCOPE")),
                                           (ToolPlan, ToolPlan(calls=[]))])
async def test_non_answer_stages_remain_nonstream_and_do_not_publish(schema, parsed):
    runnable = SimpleNamespace(ainvoke=AsyncMock(return_value={"raw": None, "parsed": parsed}),
                               astream=MagicMock(side_effect=AssertionError("Etapa interna no debe emitirse")))
    sink = AsyncMock()
    hooks = hooks_with(runnable, sink)
    assert await hooks.structured(schema, []) == parsed
    runnable.ainvoke.assert_awaited_once_with([])
    runnable.astream.assert_not_called()
    sink.assert_not_awaited()
    assert hooks.first_response_ms is None


async def test_without_sink_answer_keeps_original_ainvoke_contract():
    parsed = Answer(response="Normal", answered=True)
    runnable = SimpleNamespace(ainvoke=AsyncMock(return_value={"raw": None, "parsed": parsed}),
                               astream=MagicMock(side_effect=AssertionError("Streaming no solicitado")))
    hooks = hooks_with(runnable)
    assert await hooks.structured(Answer, []) == parsed
    runnable.ainvoke.assert_awaited_once_with([])
    runnable.astream.assert_not_called()
    assert hooks.first_response_ms is None


async def test_final_parse_failure_keeps_usage_and_never_retries():
    state = {"calls": 0}
    async def stream(messages, config):
        state["calls"] += 1
        observer = config["callbacks"][0]
        raw = AIMessageChunk(content='{"response":"Parcial","answered":"invalid"}',
                             usage_metadata={"input_tokens": 7, "output_tokens": 3, "total_tokens": 10})
        await feed(observer, raw)
        yield {"raw": raw, "parsed": None, "parsing_error": ValueError("invalid")}

    sink = AsyncMock()
    runnable = SimpleNamespace(astream=stream, ainvoke=AsyncMock(side_effect=AssertionError("No reparar con LLM")))
    hooks = hooks_with(runnable, sink)
    with pytest.raises(ValueError, match="estructurada"):
        await hooks.structured(Answer, [])
    assert hooks.tokens == 10 and hooks.usage[0]["usage_reported"] is True
    assert state["calls"] == 1
    runnable.ainvoke.assert_not_awaited()
    sink.assert_awaited_once_with("Parcial")


@pytest.mark.parametrize("with_usage", [True, False])
async def test_interrupted_stream_preserves_known_usage_without_retry(with_usage):
    state = {"calls": 0}
    async def stream(messages, config):
        state["calls"] += 1
        usage = {"input_tokens": 7, "output_tokens": 2, "total_tokens": 9} if with_usage else None
        await feed(config["callbacks"][0], AIMessageChunk(content='{"response":"Parcial', usage_metadata=usage))
        raise TimeoutError("Desconexión simulada")
        yield  # Hace de esta función un generador asíncrono aun cuando termina con error.

    sink = AsyncMock()
    runnable = SimpleNamespace(astream=stream, ainvoke=AsyncMock(side_effect=AssertionError("No retry")))
    hooks = hooks_with(runnable, sink)
    with pytest.raises(TimeoutError):
        await hooks.structured(Answer, [])
    assert hooks.tokens == (9 if with_usage else 0)
    assert hooks.usage[0]["usage_reported"] is with_usage
    assert state["calls"] == 1
    runnable.ainvoke.assert_not_awaited()
    sink.assert_awaited_once_with("Parcial")


async def test_oversized_preview_does_not_truncate_the_final_answer():
    sink = AsyncMock()
    observer = AnswerStream(sink)
    observer._preview_limit = 32
    response = "x" * 200
    await feed(observer, AIMessageChunk(content=json.dumps({"response": response})))
    sink.assert_not_awaited()
    await observer.publish(response)
    sink.assert_awaited_once_with(response)
