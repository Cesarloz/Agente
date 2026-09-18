"""Verifica reducción de inicializaciones RAG sin red ni umbrales de tiempo inestables."""

import asyncio
import time
from concurrent.futures import ThreadPoolExecutor
from threading import Barrier
from types import SimpleNamespace
from unittest.mock import Mock

import pytest
from langchain_core.documents import Document
from langchain_core.embeddings import Embeddings

from app.providers import ChromaVectorStoreProvider, FAISSVectorStoreProvider
from app.providers.vectors import collection_name
from app.services.knowledge import KnowledgeService


class CountingEmbeddings(Embeddings):
    """Cada texto nuevo produce un vector local; los contadores detectan repeticiones."""

    def __init__(self):
        self.texts = []

    def embed_documents(self, texts):
        self.texts.extend(texts)
        return [[1.0, float(len(text))] for text in texts]

    def embed_query(self, text):
        raise AssertionError("La prueba de indexación no debe embeber consultas")


class LocalCollection:
    """Sustituye sólo la persistencia Chroma; el adaptador LangChain sí es el real."""

    def __init__(self):
        self.records = {}
        self.writes = 0

    def get(self, ids=None, **kwargs):
        selected = [identifier for identifier in ids or [] if identifier in self.records]
        return {"ids": selected,
                "documents": [self.records[identifier]["content"] for identifier in selected],
                "metadatas": [self.records[identifier]["metadata"] for identifier in selected]}

    def upsert(self, ids, documents, metadatas, embeddings):
        self.writes += 1
        for identifier, content, metadata, vector in zip(ids, documents, metadatas, embeddings, strict=True):
            self.records[identifier] = {"content": content, "metadata": metadata, "vector": vector}


class LocalClient:
    def __init__(self):
        self.collections = {}

    def get_or_create_collection(self, name, **kwargs):
        return self.collections.setdefault(name, LocalCollection())


@pytest.mark.parametrize("kind,provider_type", [("chroma", ChromaVectorStoreProvider), ("faiss", FAISSVectorStoreProvider)])
def test_service_reuses_provider_only_for_its_own_request(monkeypatch, tmp_path, kind, provider_type):
    settings = SimpleNamespace(vector_provider=kind, storage_dir=tmp_path, chroma_host="unused.test", chroma_port=8000)
    monkeypatch.setattr("app.services.knowledge.get_settings", lambda: settings)
    first = KnowledgeService(None)
    second = KnowledgeService(None)
    assert isinstance(first.vectors(), provider_type)
    assert first.vectors() is first.vectors()
    assert second.vectors() is not first.vectors()
    if kind == "chroma":
        assert first.vectors()._client is None


@pytest.mark.parametrize("persistent", [False, True])
async def test_hundred_batches_reuse_one_client_without_extra_embeddings(monkeypatch, tmp_path, persistent):
    client = LocalClient()
    factory = Mock(return_value=client)
    factory_name = "PersistentClient" if persistent else "HttpClient"
    monkeypatch.setattr(f"chromadb.{factory_name}", factory)
    provider = ChromaVectorStoreProvider(host="unused.test", port=8123, persist_dir=tmp_path if persistent else None)
    embeddings = CountingEmbeddings()
    batches = [[Document(page_content=f"horario {number}", metadata={"source_id": "manual"})] for number in range(100)]
    factory.assert_not_called()
    for number, batch in enumerate(batches):
        await provider.upsert("agent", "documents", batch, [str(number)], embeddings, "model")
    # Reindexar los mismos lotes reutiliza transporte y los embeddings persistidos.
    for number, batch in enumerate(batches):
        await provider.upsert("agent", "documents", batch, [str(number)], embeddings, "model")
    factory.assert_called_once_with(**({"path": str(tmp_path)} if persistent else {"host": "unused.test", "port": 8123}))
    collection = client.collections[collection_name("agent", "documents", "model")]
    assert len(collection.records) == 100
    assert collection.writes == 100
    assert len(embeddings.texts) == 100
    # Otro agente/modelo usa su propia colección y conserva su adaptador de embeddings.
    other_embeddings = CountingEmbeddings()
    other = await asyncio.to_thread(provider._store, "other-agent", "faq", other_embeddings, "other-model")
    assert other.embeddings is other_embeddings
    assert len(client.collections) == 2
    assert factory.call_count == 1


def test_concurrent_collection_initialization_has_one_client(monkeypatch):
    client = LocalClient()
    start = Barrier(8)

    def construct(**kwargs):
        # Simula una inicialización bloqueante y libera el GIL; no mide latencia ni usa red.
        time.sleep(0.02)
        return client

    factory = Mock(side_effect=construct)
    monkeypatch.setattr("chromadb.HttpClient", factory)
    provider = ChromaVectorStoreProvider(host="unused.test")

    def build(index):
        start.wait(timeout=5)
        return provider._store("agent", "faq", None, f"model-{index}")

    with ThreadPoolExecutor(max_workers=8) as executor:
        stores = list(executor.map(build, range(8)))
    assert len(stores) == 8
    assert len(client.collections) == 8
    factory.assert_called_once()


def test_failed_client_initialization_can_be_retried_without_poisoning_cache(monkeypatch):
    client = LocalClient()
    factory = Mock(side_effect=[RuntimeError("fallo local simulado"), client])
    monkeypatch.setattr("chromadb.HttpClient", factory)
    provider = ChromaVectorStoreProvider(host="unused.test")
    with pytest.raises(RuntimeError, match="simulado"):
        provider._store("agent", "faq", None, "model")
    assert provider._client is None
    assert provider._store("agent", "faq", None, "model").embeddings is None
    assert factory.call_count == 2


async def test_empty_operations_do_not_create_chroma_client(monkeypatch):
    factory = Mock(side_effect=AssertionError("No debe abrirse una conexión para operaciones vacías"))
    monkeypatch.setattr("chromadb.HttpClient", factory)
    provider = ChromaVectorStoreProvider(host="unused.test")
    embeddings = CountingEmbeddings()
    await provider.upsert("agent", "documents", [], [], embeddings, "model")
    await provider.delete("agent", "documents", [], None, "model")
    assert await provider.retrieve("agent", "documents", "horario", embeddings, "model", source_ids=set()) == []
    assert embeddings.texts == []
    factory.assert_not_called()
