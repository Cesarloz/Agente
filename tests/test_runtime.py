import json

import pytest

from app.runtime import AgentRuntime


class Hooks:
    def __init__(self, scope="IN_SCOPE", tools=None, fail=None):
        self.scope = scope
        self.tools = tools or []
        self.fail = fail
        self.calls = []

    def called(self, name):
        self.calls.append(name)
        if self.fail == name:
            raise RuntimeError("SECRET_API_KEY should never be exposed")

    async def classify_scope(self, state):
        self.called("scope")
        return self.scope

    async def retrieve_faqs(self, state):
        self.called("faq")
        return [{"content": "Abierto de 9 a 5", "metadata": {"faq_id": "faq-1"}}]

    async def retrieve_documents(self, state):
        self.called("rag")
        return [{"content": "Horario de oficina", "metadata": {"document_id": "doc-1"}}]

    async def decide_tools(self, state):
        self.called("decision")
        return self.tools

    async def execute_tool(self, call, state):
        self.called(call["type"])
        return {"type": call["type"], "result": "ok", "attachments": [{"file_id": "f-1"}] if call["type"] == "file" else []}

    async def generate(self, state):
        self.called("llm")
        assert "faq-1" in state["context"]
        assert "doc-1" in state["context"]
        return {"response": "Abrimos de 9 a 5.", "tokens": 42, "answered": True}


def initial(**overrides):
    return {"conversation_id": "c-1", "agent_id": "a-1", "question": "¿Cuál es el horario?",
            "messages": [], "config": {}, "interaction_count": 0, "max_interactions": 10, **overrides}


@pytest.mark.asyncio
async def test_retrieval_and_tool_branches_preserve_order_and_attachments():
    hooks = Hooks(tools=[{"type": "database"}, {"type": "file"}, {"type": "current_time"}])
    result = await AgentRuntime(hooks).run(initial())
    assert hooks.calls == ["scope", "faq", "rag", "decision", "database", "file", "current_time", "llm"]
    assert result["interaction_count"] == 1
    assert result["conversation_status"] == "ACTIVE"
    assert result["attachments"] == [{"file_id": "f-1"}]
    assert result["answered"] is True
    assert result["tokens"] == 42
    assert [event["node"] for event in result["trace"]] == [
        "ConversationGuard", "ScopeGuard", "FAQRetriever", "RAGRetriever", "ToolDecision",
        "DatabaseTool", "FileTool", "OtherTools", "ContextBuilder", "LLM", "Response",
        "InteractionCounter", "PostConversationGuard", "Continue",
    ]
    assert result["messages"][0]["role"] == "user"
    assert result["messages"][-1]["role"] == "assistant"


@pytest.mark.asyncio
@pytest.mark.parametrize("policy,status", [("CLOSE", "CLOSED"), ("WARN", "ACTIVE")])
async def test_out_of_scope_close_or_warn_never_retrieves_or_calls_tools(policy, status):
    hooks = Hooks(scope="OUT_OF_SCOPE")
    result = await AgentRuntime(hooks).run(initial(config={"out_of_scope_policy": policy, "out_of_scope_message": "Solo soporte técnico."}))
    assert hooks.calls == ["scope"]
    assert result["response"] == "Solo soporte técnico."
    assert result["conversation_status"] == status
    assert result["interaction_count"] == 1
    assert not result["answered"]


@pytest.mark.asyncio
async def test_out_of_scope_continue_and_uncertain_do_process_question():
    for scope in ["OUT_OF_SCOPE", "UNCERTAIN", "unknown"]:
        hooks = Hooks(scope=scope)
        result = await AgentRuntime(hooks).run(initial(config={"out_of_scope_policy": "CONTINUE"}))
        assert hooks.calls[-1] == "llm"
        assert result["scope_status"] == ("UNCERTAIN" if scope == "unknown" else scope)


@pytest.mark.asyncio
async def test_interaction_limit_closes_exactly_at_max_and_rejects_followup():
    hooks = Hooks()
    runtime = AgentRuntime(hooks)
    result = await runtime.run(initial(interaction_count=1, max_interactions=2))
    assert result["conversation_status"] == "CLOSED"
    assert result["interaction_count"] == 2
    calls = hooks.calls[:]
    followup = await runtime.run({**result, "question": "Una pregunta más"})
    assert followup["interaction_count"] == 2
    assert hooks.calls == calls
    assert [item["node"] for item in followup["trace"]] == ["ConversationGuard", "Finalize"]


@pytest.mark.asyncio
@pytest.mark.parametrize("stage", ["scope", "faq", "rag", "decision", "database", "llm"])
async def test_errors_are_sanitized_counted_once_and_recoverable(stage):
    hooks = Hooks(tools=[{"type": "database"}], fail=stage)
    result = await AgentRuntime(hooks).run(initial())
    assert result["interaction_count"] == 1
    assert result["error"]["code"] == "RUNTIME_ERROR"
    assert result["conversation_status"] == "ERROR"
    assert not result["answered"]
    assert "SECRET_API_KEY" not in json.dumps(result)
    assert sum(item["node"] == "InteractionCounter" for item in result["trace"]) == 1
    assert sum(item["node"] == "Error" for item in result["trace"]) == 1
    hooks.fail = None
    recovered = await AgentRuntime(hooks).run({**result, "question": "Intentar de nuevo"})
    assert recovered["conversation_status"] == "ACTIVE"
    assert recovered["interaction_count"] == 2


@pytest.mark.asyncio
async def test_tool_budget_fails_before_executing_any_tool():
    hooks = Hooks(tools=[{"type": "database"}] * 9)
    result = await AgentRuntime(hooks).run(initial())
    assert result["error"]["node"] == "ToolDecision"
    assert "database" not in hooks.calls


@pytest.mark.asyncio
async def test_does_not_mutate_callers_state_or_duplicate_current_question():
    state = initial(messages=[{"role": "user", "content": "¿Cuál es el horario?"}])
    result = await AgentRuntime(Hooks()).run(state)
    assert len(state["messages"]) == 1
    assert len(result["messages"]) == 2
    assert "trace" not in state


def test_graph_exports_visible_state_transitions():
    diagram = AgentRuntime(Hooks()).mermaid()
    for node in ["ConversationGuard", "ScopeGuard", "FAQRetriever", "RAGRetriever", "DatabaseTool", "FileTool", "OtherTools", "Error", "Finalize"]:
        assert node in diagram
