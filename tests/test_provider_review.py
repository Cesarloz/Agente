"""Regresiones de gasto y transporte; ninguna prueba necesita red ni credenciales reales."""

import asyncio
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock

import httpx
import pytest
from langchain_core.embeddings import Embeddings
from langchain_core.messages import HumanMessage, SystemMessage

from app.providers.cached_embeddings import RequestEmbeddings
from app.providers.embeddings import GeminiEmbeddingProvider, OpenAIEmbeddingProvider
from app.providers.llm import GeminiLLMProvider, OpenAILLMProvider


@pytest.fixture(autouse=True)
def forbid_network(monkeypatch):
    def forbidden(*args, **kwargs):
        raise AssertionError("Esta revisión debe ejecutarse sin peticiones de red")

    monkeypatch.setattr(httpx.Client, "send", forbidden)
    monkeypatch.setattr(httpx.AsyncClient, "send", forbidden)


def test_openai_transport_limits_reach_real_client_and_payload():
    model = OpenAILLMProvider().build(
        "offline-placeholder", "gpt-5", timeout=23, max_retries=0,
        max_output_tokens=512, reasoning_effort="low",
    )
    payload = model._get_request_payload([SystemMessage("Reglas"), HumanMessage("Pregunta")])
    assert payload["max_completion_tokens"] == 512
    assert payload["reasoning_effort"] == "low"
    assert [message["content"] for message in payload["messages"]] == ["Reglas", "Pregunta"]
    assert model.root_client.max_retries == model.root_async_client.max_retries == 0
    assert model.root_client.timeout == 23


@pytest.mark.parametrize("retries", [0, 1, 2])
def test_gemini_requested_retries_map_to_total_attempts_in_actual_payload(retries):
    model = GeminiLLMProvider().build(
        "offline-placeholder", "gemini-2.5-flash", timeout=23,
        max_retries=retries, max_output_tokens=512,
    )
    payload = model._prepare_request([SystemMessage("Reglas"), HumanMessage("Pregunta")])
    config = payload["config"]
    assert config.http_options.retry_options.attempts == retries + 1
    assert config.http_options.timeout == 23_000
    assert config.max_output_tokens == 512


def test_openai_embeddings_disable_retries_in_both_actual_clients():
    model = OpenAIEmbeddingProvider().build("offline-placeholder", "text-embedding-3-small")
    assert model.client._client.max_retries == 0
    assert model.async_client._client.max_retries == 0
    assert model.client._client.timeout == 45


async def test_gemini_embedding_limits_reach_all_four_call_paths(monkeypatch):
    response = SimpleNamespace(embeddings=[SimpleNamespace(values=[1.0, 0.0])])
    sync_embed = MagicMock(return_value=response)
    async_embed = AsyncMock(return_value=response)
    client = SimpleNamespace(
        models=SimpleNamespace(embed_content=sync_embed),
        aio=SimpleNamespace(models=SimpleNamespace(embed_content=async_embed)),
    )
    monkeypatch.setattr("langchain_google_genai.embeddings.Client", MagicMock(return_value=client))
    embeddings = GeminiEmbeddingProvider().build("offline-placeholder", "gemini-embedding-001")
    assert embeddings.embed_query("pregunta") == [1.0, 0.0]
    assert embeddings.embed_documents(["documento"]) == [[1.0, 0.0]]
    assert await embeddings.aembed_query("pregunta") == [1.0, 0.0]
    assert await embeddings.aembed_documents(["documento"]) == [[1.0, 0.0]]
    for calls in (sync_embed.call_args_list, async_embed.call_args_list):
        assert [call.kwargs["config"].task_type for call in calls] == [
            "RETRIEVAL_QUERY", "RETRIEVAL_DOCUMENT",
        ]
        for call in calls:
            assert call.kwargs["config"].http_options.timeout == 45_000
            assert call.kwargs["config"].http_options.retry_options.attempts == 1


class ControlledEmbeddings(Embeddings):
    def __init__(self):
        self.calls = []
        self.started = asyncio.Event()
        self.release = asyncio.Event()

    def embed_query(self, text):
        raise AssertionError("La ruta async debe usar el método nativo del delegado")

    def embed_documents(self, texts):
        return [[3.0] for _ in texts]

    async def aembed_query(self, text):
        self.calls.append(text)
        self.started.set()
        await self.release.wait()
        return [1.0, 0.0]


async def test_distinct_async_queries_progress_without_a_global_network_lock():
    delegate = ControlledEmbeddings()
    cache = RequestEmbeddings(delegate)
    first = asyncio.create_task(cache.aembed_query("horario"))
    await delegate.started.wait()
    delegate.started.clear()
    second = asyncio.create_task(cache.aembed_query("precio"))
    try:
        await asyncio.wait_for(delegate.started.wait(), timeout=2)
    finally:
        delegate.release.set()
        results = await asyncio.gather(first, second)
    assert results == [[1.0, 0.0], [1.0, 0.0]]
    assert delegate.calls == ["horario", "precio"]


async def test_cancelled_waiter_does_not_cancel_shared_provider_request():
    delegate = ControlledEmbeddings()
    cache = RequestEmbeddings(delegate)
    owner = asyncio.create_task(cache.aembed_query("horario"))
    await delegate.started.wait()
    waiter = asyncio.create_task(cache.aembed_query("horario"))
    await asyncio.sleep(0)
    waiter.cancel()
    with pytest.raises(asyncio.CancelledError):
        await waiter
    delegate.release.set()
    result = await asyncio.wait_for(owner, timeout=2)
    result[0] = 99
    assert cache.embed_query("horario") == [1.0, 0.0]
    assert delegate.calls == ["horario"]


async def test_sync_and_async_consumers_share_one_pending_request():
    delegate = ControlledEmbeddings()
    cache = RequestEmbeddings(delegate)
    owner = asyncio.create_task(cache.aembed_query("horario"))
    await delegate.started.wait()
    sync_consumer = asyncio.create_task(asyncio.to_thread(cache.embed_query, "horario"))
    delegate.release.set()
    assert await asyncio.gather(owner, sync_consumer) == [[1.0, 0.0], [1.0, 0.0]]
    assert delegate.calls == ["horario"]


async def test_failed_query_is_not_implicitly_retried_by_other_retrievers():
    delegate = ControlledEmbeddings()
    delegate.aembed_query = AsyncMock(side_effect=RuntimeError("fallo simulado"))
    cache = RequestEmbeddings(delegate)
    errors = await asyncio.gather(
        cache.aembed_query("horario"), cache.aembed_query("horario"), return_exceptions=True,
    )
    assert all(isinstance(error, RuntimeError) for error in errors)
    with pytest.raises(RuntimeError, match="fallo simulado"):
        cache.embed_query("horario")
    assert delegate.aembed_query.await_count == 1
    # Otra petición independiente puede intentarlo de nuevo: la caché no cruza usuarios ni perfiles.
    with pytest.raises(RuntimeError):
        await RequestEmbeddings(delegate).aembed_query("horario")
    assert delegate.aembed_query.await_count == 2


async def test_cancelled_owner_settles_other_waiters_instead_of_hanging():
    delegate = ControlledEmbeddings()
    cache = RequestEmbeddings(delegate)
    owner = asyncio.create_task(cache.aembed_query("horario"))
    await delegate.started.wait()
    waiter = asyncio.create_task(cache.aembed_query("horario"))
    await asyncio.sleep(0)
    owner.cancel()
    results = await asyncio.wait_for(asyncio.gather(owner, waiter, return_exceptions=True), timeout=2)
    assert all(isinstance(result, asyncio.CancelledError) for result in results)
    assert delegate.calls == ["horario"]
