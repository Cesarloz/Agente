"""Regresiones de consumo y evidencia: ninguna prueba llama a proveedores externos."""

import json
from copy import deepcopy
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock

import pytest

from app.runtime.prompt_budget import bounded_history, build_evidence, compact_json
from app.schemas import AgentConfig
from app.services.runtime_hooks import Answer, ApplicationRuntimeHooks, ScopeResult


def source(text, id="manual"):
    return {"content": text, "page_content": text,
            "metadata": {"source_id": id, "source": "Guía", "answer": text, "page": 2}}


def test_evidence_deduplicates_aliases_and_same_source_without_modifying_audit():
    records = [source("Horario: de nueve a cinco.")] * 2
    original = deepcopy(records)
    result = json.loads(build_evidence([], records, []))
    assert len(result["documents"]) == 1
    assert "page_content" not in result["documents"][0]
    assert "answer" not in result["documents"][0]["metadata"]
    assert result["documents"][0]["metadata"]["page"] == 2
    assert records == original
    different = json.loads(build_evidence([], [source("Igual", "a"), source("Igual", "b")], []))
    assert len(different["documents"]) == 2


@pytest.mark.parametrize("text", ['"\\\n' * 3000, "Información 🤖 " * 2000], ids=["escapes", "unicode"])
def test_context_budget_counts_escapes_and_always_preserves_valid_json(text):
    serialized = build_evidence([source(text, "faq")], [source(text)], [], 1000)
    assert len(serialized) <= 1000
    evidence = json.loads(serialized)
    assert evidence["truncated"] is True
    assert evidence["faqs"] and evidence["documents"]
    assert all(r["truncated"] for r in evidence["faqs"] + evidence["documents"])


def test_tool_budget_keeps_complete_rows_and_reports_omissions():
    tool = {"name": "database", "result": {"ok": True, "rows": [{"value": "dato" * 60}] * 30,
                                            "row_count": 30, "max_rows": 100}}
    original = deepcopy(tool)
    evidence = json.loads(build_evidence([], [], [tool], 1000))
    result = evidence["tools"][0]["result"]
    assert evidence["truncated"]
    assert result["row_count"] == len(result["rows"]) < 30
    assert result["original_row_count"] == 30
    assert all(row == {"value": "dato" * 60} for row in result["rows"])
    assert tool == original


def test_excessive_source_metadata_is_omitted_without_corrupting_context():
    large = source("Dato", "id" * 10000)
    evidence = json.loads(build_evidence([large], [source("Dato útil")], [], 1000))
    assert evidence["truncated"] and not evidence["faqs"]
    assert evidence["documents"][0]["content"] == "Dato útil"


def test_evidence_preserves_all_text_when_combined_sources_fit_the_budget():
    text = "x" * 6000
    evidence = json.loads(build_evidence([source("FAQ breve", "faq")], [source(text)], [], 10000))
    assert not evidence["truncated"]
    assert evidence["documents"][0]["content"] == text


def test_sql_nonfinite_values_are_text_in_standard_json():
    rows = [{"value": float("nan")}, {"value": float("inf")}, {"value": float("-inf")}]
    serialized = build_evidence([], [], [{"name": "database", "result": {"rows": rows}}])

    def invalid_constant(value):
        raise AssertionError(f"No es JSON estándar: {value}")

    result = json.loads(serialized, parse_constant=invalid_constant)
    assert [row["value"] for row in result["tools"][0]["result"]["rows"]] == ["nan", "inf", "-inf"]


def test_history_omits_current_question_and_applies_window_after_removal():
    messages = [{"role": "user", "content": "Me llamo Ana"},
                {"role": "assistant", "content": "Hola Ana"},
                {"role": "user", "content": "¿Cómo me llamo?"}]
    assert bounded_history(messages, "¿Cómo me llamo?", 2, 1000) == messages[:2]
    assert len(messages) == 3
    assert bounded_history(messages, "¿Cómo me llamo?", 0, 1000) == []


def test_history_keeps_complete_recent_messages_with_a_size_limit():
    messages = [{"role": "user", "content": "x" * 5000},
                {"role": "assistant", "content": "Reciente"}]
    history = bounded_history(messages, "Pregunta", 20, 1000)
    assert history == messages[-1:]
    assert len(compact_json(history)) <= 1000


async def test_generation_sends_one_question_and_evidence_object():
    hooks = ApplicationRuntimeHooks(None, "agent", AgentConfig(name="Prueba").model_dump())
    hooks.structured = AsyncMock(return_value=Answer(response="Ana", answered=True))
    await hooks.generate({"question": "¿Cómo me llamo?",
                          "messages": [{"role": "user", "content": "Me llamo Ana"},
                                       {"role": "user", "content": "¿Cómo me llamo?"}],
                          "context": build_evidence([], [source("Información")], [])})
    messages = hooks.structured.call_args.args[1]
    payload = json.loads(messages[1].content)
    assert isinstance(payload["evidence"], dict)
    assert payload["history"] == [{"role": "user", "content": "Me llamo Ana"}]
    assert messages[1].content.count("¿Cómo me llamo?") == 1


async def test_enabled_tools_without_resources_do_not_call_llm(session):
    hooks = ApplicationRuntimeHooks(session, "missing", AgentConfig(name="Prueba", tools_enabled=["file", "database"]).model_dump())
    hooks.structured = AsyncMock(side_effect=AssertionError("No hay herramientas ejecutables"))
    assert await hooks.decide_tools({"question": "Hola"}) == []
    hooks.structured.assert_not_called()


async def test_oversized_inventory_stops_before_spending_tokens(session, monkeypatch):
    from app.errors import AppError

    hooks = ApplicationRuntimeHooks(session, "agent", AgentConfig(name="Prueba", tools_enabled=["file"], max_tool_context_chars=1000).model_dump())
    monkeypatch.setattr("app.services.runtime_hooks.Repository.list", AsyncMock(return_value=[SimpleNamespace(id="file", name="x" * 2000)]))
    hooks.structured = AsyncMock(side_effect=AssertionError("Inventario fuera de presupuesto"))
    with pytest.raises(AppError, match="inventario"):
        await hooks.decide_tools({"question": "Dame archivo"})
    hooks.structured.assert_not_called()


async def test_token_accounting_includes_parse_failures_and_reuses_schema_adapter():
    hooks = ApplicationRuntimeHooks(None, "agent", {})
    runnable = SimpleNamespace(ainvoke=AsyncMock(side_effect=[
        {"raw": SimpleNamespace(usage_metadata={"input_tokens": 10, "output_tokens": 2, "total_tokens": 12}),
         "parsed": ScopeResult(scope_status="IN_SCOPE")},
        {"raw": SimpleNamespace(usage_metadata=None, response_metadata={"token_usage": {"prompt_tokens": 20, "completion_tokens": 3}}),
         "parsed": None},
    ]))
    model = MagicMock()
    model.with_structured_output.return_value = runnable
    hooks.model = AsyncMock(return_value=model)
    await hooks.structured(ScopeResult, [])
    with pytest.raises(ValueError, match="estructurada"):
        await hooks.structured(ScopeResult, [])
    assert hooks.tokens == 35
    assert len(hooks.usage) == 2 and all(item["usage_reported"] for item in hooks.usage)
    model.with_structured_output.assert_called_once()


async def test_timeout_is_recorded_as_unknown_usage_instead_of_zero_cost():
    hooks = ApplicationRuntimeHooks(None, "agent", {})
    model = MagicMock()
    model.with_structured_output.return_value = SimpleNamespace(ainvoke=AsyncMock(side_effect=TimeoutError))
    hooks.model = AsyncMock(return_value=model)
    with pytest.raises(TimeoutError):
        await hooks.structured(Answer, [])
    assert len(hooks.usage) == 1
    assert hooks.usage[0]["usage_reported"] is False


def test_prompt_budget_settings_are_validated_and_persistable():
    config = AgentConfig(name="A", max_context_chars=5000, max_history_chars=2000, max_tool_context_chars=3000)
    assert config.model_dump()["max_context_chars"] == 5000
    with pytest.raises(ValueError):
        AgentConfig(name="A", max_context_chars=999)
