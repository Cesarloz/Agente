import asyncio
from unittest.mock import AsyncMock, MagicMock

from langchain_core.embeddings import Embeddings
from sqlalchemy import select

from app.models import Question
from app.providers.cached_embeddings import RequestEmbeddings
from app.providers.llm import GeminiLLMProvider, OpenAILLMProvider
from app.schemas import AgentConfig, ChatInput
from app.services.agents import AgentService
from app.services.conversations import ConversationService
from app.services.knowledge import FAQService
from app.services.runtime_hooks import ApplicationRuntimeHooks


class CountingEmbeddings(Embeddings):
    def __init__(self):
        self.calls = []

    def embed_query(self, text):
        self.calls.append(text)
        return [1.0, 0.0]

    def embed_documents(self, texts):
        return [self.embed_query(text) for text in texts]


async def test_same_query_embedding_is_computed_once_for_faq_and_documents():
    provider = CountingEmbeddings()
    request = RequestEmbeddings(provider)
    results = await asyncio.gather(request.aembed_query("horario"), request.aembed_query("horario"))
    assert results == [[1.0, 0.0], [1.0, 0.0]]
    assert provider.calls == ["horario"]
    results[0][0] = 99
    assert request.embed_query("horario") == [1.0, 0.0]
    request.embed_query("precio")
    RequestEmbeddings(provider).embed_query("horario")
    assert provider.calls == ["horario", "precio", "horario"]


async def test_exact_faq_answers_without_llm_or_embeddings_and_preserves_audit(session, monkeypatch):
    agent = await AgentService(session).create(AgentConfig(name="Rápido", model="unused"))
    await FAQService(session).save(agent["id"], {"question": "¿Cómo agendo una cita?", "answer": "Agenda en [Bookings](https://outlook.office.com/bookwithme/)."})
    generate = AsyncMock(side_effect=AssertionError("No LLM call should be needed"))
    retrieve = AsyncMock(side_effect=AssertionError("No document embeddings should be needed"))
    monkeypatch.setattr(ApplicationRuntimeHooks, "generate", generate)
    monkeypatch.setattr(ApplicationRuntimeHooks, "retrieve_documents", retrieve)
    result = await ConversationService(session).chat(agent["id"], ChatInput(message="  cómo AGENDO una cita  "))
    assert not result["error"] and "https://outlook.office.com/bookwithme/" in result["response"]
    generate.assert_not_called()
    retrieve.assert_not_called()
    assert "LLM" not in [event["node"] for event in result["trace"]]
    assert all(event["duration_ms"] >= 0 for event in result["trace"])
    assert result["timings"]["total_ms"] >= 0
    question = await session.scalar(select(Question))
    assert question.answered and question.faq_used and question.tokens == 0


async def test_exact_faq_does_not_bypass_scope_policy(session, monkeypatch):
    agent = await AgentService(session).create(AgentConfig(name="Alcance", scope_description="Solo soporte", out_of_scope_policy="CLOSE"))
    await FAQService(session).save(agent["id"], {"question": "Pregunta", "answer": "No debe enviarse"})
    monkeypatch.setattr(ApplicationRuntimeHooks, "classify_scope", AsyncMock(return_value="OUT_OF_SCOPE"))
    result = await ConversationService(session).chat(agent["id"], ChatInput(message="Pregunta"))
    assert result["conversation_status"] == "CLOSED"
    assert "No debe enviarse" not in result["response"]


def test_provider_limits_have_no_hidden_retries_and_apply_explicit_options(monkeypatch):
    openai = MagicMock()
    gemini = MagicMock()
    monkeypatch.setattr("langchain_openai.ChatOpenAI", openai)
    monkeypatch.setattr("langchain_google_genai.ChatGoogleGenerativeAI", gemini)
    OpenAILLMProvider().build("fake", "test", timeout=25, max_output_tokens=2048, reasoning_effort="low")
    assert openai.call_args.kwargs["max_retries"] == 0
    assert openai.call_args.kwargs["timeout"] == 25
    assert openai.call_args.kwargs["max_tokens"] == 2048
    assert openai.call_args.kwargs["reasoning_effort"] == "low"
    GeminiLLMProvider().build("fake", "test", max_output_tokens=1024, reasoning_effort="low")
    assert gemini.call_args.kwargs["max_retries"] == 1  # Google cuenta el intento inicial, no solo reintentos.
    assert "reasoning_effort" not in gemini.call_args.kwargs


async def test_authorized_links_reach_model_prompt_and_reject_active_schemes(client, session):
    config = AgentConfig(name="Citas", response_links=[{"label": "Agendar", "url": "https://outlook.office.com/bookwithme/"}])
    hooks = ApplicationRuntimeHooks(session, "agent", config.model_dump())
    hooks.structured = AsyncMock(return_value=MagicMock(response="Agenda aquí", answered=True))
    await hooks.generate({"question": "Quiero una cita", "messages": []})
    prompt = hooks.structured.call_args.args[1][0].content
    assert "https://outlook.office.com/bookwithme/" in prompt
    assert "[texto](URL)" in prompt
    invalid = await client.post("/api/agents", json={"name": "XSS", "response_links": [{"label": "Unsafe", "url": "javascript:alert(1)"}]})
    assert invalid.status_code == 422
