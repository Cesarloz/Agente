"""Regresiones del RAG con embeddings deterministas y almacenamiento local; sin APIs."""

from unittest.mock import AsyncMock

import pytest
from langchain_core.documents import Document as Chunk
from langchain_core.embeddings import Embeddings
from sqlalchemy import func, select

from app.models import Agent, Document, FAQ, Job
from app.providers import ChromaVectorStoreProvider, FAISSVectorStoreProvider
from app.providers.vectors import collection_name
from app.schemas import AgentConfig
from app.services.agents import AgentService
from app.services.knowledge import DocumentService, FAQService, KnowledgeService, profile


class CountingEmbeddings(Embeddings):
    """Cuenta el texto enviado a embeddings y evita cualquier conexión externa."""

    def __init__(self):
        self.document_batches = []
        self.queries = []

    @staticmethod
    def vector(text):
        return [float("horario" in text.lower()), float("precio" in text.lower()), 0.1]

    def embed_documents(self, texts):
        self.document_batches.append(list(texts))
        return [self.vector(text) for text in texts]

    def embed_query(self, text):
        self.queries.append(text)
        return self.vector(text)


@pytest.fixture(params=["chroma", "faiss"])
def vector_store(request, tmp_path):
    if request.param == "chroma":
        pytest.importorskip("langchain_chroma")
        return ChromaVectorStoreProvider(persist_dir=tmp_path / "chroma")
    pytest.importorskip("faiss")
    return FAISSVectorStoreProvider(tmp_path / "faiss")


async def test_unchanged_upsert_has_zero_new_embedding_calls(vector_store):
    embedding = CountingEmbeddings()
    doc = Chunk(page_content="horario nueve", metadata={"source_id": "manual"})
    await vector_store.upsert("agent", "documents", [doc], ["one"], embedding, "model-v1")
    await vector_store.upsert("agent", "documents", [doc], ["one"], embedding, "model-v1")
    assert embedding.document_batches == [["horario nueve"]]
    changed = Chunk(page_content="horario diez", metadata=doc.metadata)
    await vector_store.upsert("agent", "documents", [changed], ["one"], embedding, "model-v1")
    assert embedding.document_batches == [["horario nueve"], ["horario diez"]]
    await vector_store.upsert("agent", "documents", [changed], ["one"], embedding, "model-v2")
    assert embedding.document_batches[-1] == ["horario diez"]
    assert len(embedding.document_batches) == 3


async def test_filter_runs_before_top_k_and_empty_index_spends_no_query_embedding(vector_store):
    embedding = CountingEmbeddings()
    assert await vector_store.retrieve("agent", "documents", "horario", embedding, "model") == []
    assert embedding.queries == []
    documents = [
        Chunk(page_content="horario", metadata={"source_id": "deleted"}),
        Chunk(page_content="precio", metadata={"source_id": "active"}),
    ]
    await vector_store.upsert("agent", "documents", documents, ["old", "live"], embedding, "model")
    for search_type in ("similarity", "mmr"):
        result = await vector_store.retrieve("agent", "documents", "horario", embedding, "model",
                                             k=1, search_type=search_type, source_ids={"active"})
        assert [row["id"] for row in result] == ["live"]
    embedding.queries.clear()
    assert await vector_store.retrieve("agent", "documents", "horario", embedding, "model", source_ids=set()) == []
    assert await vector_store.retrieve("agent", "documents", "horario", embedding, "model", source_ids={"missing"}) == []
    assert embedding.queries == []


async def test_faiss_metadata_update_reuses_vector(tmp_path):
    provider = FAISSVectorStoreProvider(tmp_path)
    embedding = CountingEmbeddings()
    await provider.upsert("agent", "faq", [Chunk(page_content="horario", metadata={"priority": 1})], ["one"], embedding, "model")
    await provider.upsert("agent", "faq", [Chunk(page_content="horario", metadata={"priority": 2})], ["one"], embedding, "model")
    assert embedding.document_batches == [["horario"]]
    assert provider._read(collection_name("agent", "faq", "model"))["one"]["metadata"]["priority"] == 2


async def test_saving_identical_indexed_faq_does_not_queue_or_hide_it(session):
    agent = await AgentService(session).create(AgentConfig(name="FAQ"))
    saved = await FAQService(session).save(agent["id"], {"question": "Horario", "answer": "Nueve"})
    row = await session.get(FAQ, saved["id"])
    row.status, row.index_profile = "INDEXED", profile(agent)
    await session.commit()
    before = await session.scalar(select(func.count()).select_from(Job))
    result = await FAQService(session).save(agent["id"], {"answer": "Nueve"}, row.id)
    assert result["status"] == "INDEXED"
    assert await session.scalar(select(func.count()).select_from(Job)) == before
    await FAQService(session).save(agent["id"], {"answer": "Diez"}, row.id)
    assert row.status == "PENDING"
    assert await session.scalar(select(func.count()).select_from(Job)) == before + 1


async def test_sql_passes_only_active_matching_sources_and_skips_empty_search(session, monkeypatch):
    agent = await AgentService(session).create(AgentConfig(name="Filtros"))
    service = KnowledgeService(session)
    store = AsyncMock()
    store.retrieve.return_value = []
    embeddings = AsyncMock(return_value=CountingEmbeddings())
    monkeypatch.setattr(service, "vectors", lambda: store)
    monkeypatch.setattr(service, "embedding", embeddings)
    assert await service.retrieve(agent["id"], "faq", "horario", agent) == []
    embeddings.assert_not_awaited()
    for label, active, index_profile in (("live", True, profile(agent)), ("inactive", False, profile(agent)), ("old-model", True, "old")):
        saved = await FAQService(session).save(agent["id"], {"question": label, "answer": "Respuesta", "active": active})
        row = await session.get(FAQ, saved["id"])
        row.status, row.index_profile = "INDEXED", index_profile
        if label == "live":
            live_id = row.id
    await session.commit()
    await service.retrieve(agent["id"], "faq", "horario", agent)
    assert store.retrieve.call_args.kwargs["source_ids"] == {live_id}


async def test_document_reindex_reuses_chunks_removes_obsolete_and_changes_profile(session, monkeypatch, vector_store):
    embedding = CountingEmbeddings()
    monkeypatch.setattr(KnowledgeService, "vectors", lambda self: vector_store)
    monkeypatch.setattr(KnowledgeService, "embedding", AsyncMock(return_value=embedding))
    agent = await AgentService(session).create(AgentConfig(name="Documentos", chunk_size=100, chunk_overlap=10))
    saved = await DocumentService(session).upload(agent["id"], "manual.txt", ("horario lunes de nueve a cinco. " * 30).encode())
    service = DocumentService(session)
    await service.index(agent["id"], saved["id"])
    row = await session.get(Document, saved["id"])
    assert row.chunk_count > 1
    calls = list(embedding.document_batches)
    await service.reindex(agent["id"], row.id)
    await service.index(agent["id"], row.id)
    assert embedding.document_batches == calls
    from pathlib import Path
    Path(row.path).write_text("horario nuevo", encoding="utf-8")
    await service.reindex(agent["id"], row.id)
    await service.index(agent["id"], row.id)
    assert row.chunk_count == 1
    old_profile = row.index_profile
    result = await vector_store.retrieve(agent["id"], "documents", "horario", embedding, old_profile, k=20)
    assert [item["id"] for item in result] == [row.chunk_ids[0]]
    agent_row = await session.get(Agent, agent["id"])
    agent_row.config = {**agent_row.config, "embedding_model": "different-test-model"}
    await session.commit()
    await service.index(agent["id"], row.id)
    assert row.index_profile != old_profile
    assert await vector_store.retrieve(agent["id"], "documents", "horario", embedding, old_profile) == []
    assert embedding.document_batches[-1] == ["horario nuevo"]


async def test_partial_index_persists_all_ids_for_purge(session, monkeypatch, tmp_path):
    provider = FAISSVectorStoreProvider(tmp_path / "vectors")
    embedding = CountingEmbeddings()
    monkeypatch.setattr(KnowledgeService, "vectors", lambda self: provider)
    monkeypatch.setattr(KnowledgeService, "embedding", AsyncMock(return_value=embedding))
    agent = await AgentService(session).create(AgentConfig(name="Parcial", chunk_size=100, chunk_overlap=0))
    saved = await DocumentService(session).upload(agent["id"], "manual.txt", ("horario de atención lunes a viernes. " * 500).encode())
    original_upsert = provider.upsert
    calls = 0

    async def fail_second_batch(*args, **kwargs):
        nonlocal calls
        calls += 1
        if calls == 2:
            raise RuntimeError("Fallo local simulado")
        await original_upsert(*args, **kwargs)

    monkeypatch.setattr(provider, "upsert", fail_second_batch)
    service = DocumentService(session)
    with pytest.raises(RuntimeError, match="simulado"):
        await service.index(agent["id"], saved["id"])
    row = await session.get(Document, saved["id"])
    assert len(row.chunk_ids) > 100
    name = collection_name(agent["id"], "documents", row.index_profile)
    assert len(provider._read(name)) == 100
    await service.purge(agent["id"], row.id)
    assert provider._read(name) == {}
    assert await session.get(Document, saved["id"]) is None
