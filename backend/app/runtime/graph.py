"""Grafo de un turno: controles, recuperación, herramientas y una respuesta final."""

from __future__ import annotations  # Aplaza la resolución de los tipos usados por los nodos.

import time  # Cronometra cada nodo sin depender de cambios del reloj de pared.
from collections.abc import Awaitable, Callable  # Describe funciones asíncronas inyectadas como nodos.
from copy import deepcopy  # Aísla el estado entrante para no modificar diccionarios del llamador.
from typing import Any  # Permite metadatos de traza con diferentes tipos de valor.

from langgraph.graph import END, START, StateGraph  # Construye transiciones explícitas entre nodos.

from .prompt_budget import build_evidence  # Acota y compacta la evidencia conservando JSON válido.
from .state import AgentState, RuntimeHooks  # Estado compartido e interfaz con los servicios de aplicación.

DEFAULT_CLOSING = "La conversación ha finalizado. Inicia una nueva para continuar."  # Cierre sin LLM.
DEFAULT_OUT_OF_SCOPE = "Tu pregunta está fuera del alcance de este agente."  # Política local sin generación.
DEFAULT_ERROR = "No pude completar la solicitud. Intenta de nuevo en un momento."  # Error sin datos internos.


class AgentRuntime:
    """Procesa un mensaje; PostgreSQL conserva el historial y el contador entre turnos.

    Continue termina esta invocación y espera el siguiente mensaje del usuario.
    El grafo no mantiene un bucle autónomo de razonamiento o generación.
    """

    def __init__(self, hooks: RuntimeHooks):
        self.hooks = hooks  # Inyecta los servicios; permite probar todo el flujo sin APIs reales.
        graph = StateGraph(AgentState)  # Declara el esquema de las actualizaciones parciales de estado.
        # Registra controles, recuperación y salida; solo LLM genera la respuesta completa.
        nodes = {
            "ConversationGuard": self._conversation_guard,  # Rechaza conversaciones ya cerradas.
            "ScopeGuard": self._scope_guard,  # Clasifica si la pregunta pertenece al alcance.
            "ScopePolicy": self._scope_policy,  # Aplica cierre, advertencia o continuación.
            "FAQRetriever": self._faq,  # Permite contestar una FAQ exacta sin generar texto.
            "RAGRetriever": self._rag,  # Recupera documentos antes de construir el prompt.
            "ToolDecision": self._tool_decision,  # Obtiene un plan acotado de herramientas.
            "DatabaseTool": self._tool,  # Ejecuta la siguiente consulta autorizada.
            "FileTool": self._tool,  # Prepara el siguiente archivo autorizado.
            "OtherTools": self._tool,  # Ejecuta el resto de herramientas soportadas.
            "ContextBuilder": self._context,  # Serializa evidencia con presupuesto de tamaño.
            "LLM": self._generate,  # Invoca una sola generación de respuesta por turno.
            "Response": self._response,  # Agrega al estado el mensaje del asistente.
            "InteractionCounter": self._counter,  # Cuenta una vez el mensaje aceptado.
            "PostConversationGuard": self._post_guard,  # Comprueba el límite tras responder.
            "Continue": self._continue,  # Termina dejando abierta la conversación.
            "Finalize": self._finalize,  # Termina dejando cerrada la conversación.
            "Error": self._error,  # Produce una respuesta pública saneada.
        }
        for name, node in nodes.items():
            graph.add_node(name, self._safe(name, node))  # Cada nodo registra duración y captura errores.
        graph.add_edge(START, "ConversationGuard")  # Ningún servicio externo se ejecuta antes del control.
        # Una conversación cerrada llega directamente a Finalize, sin recuperación ni LLM.
        graph.add_conditional_edges("ConversationGuard", self._route, {
            "scope": "ScopeGuard", "finalize": "Finalize", "error": "Error",
        })
        # Solo OUT_OF_SCOPE requiere aplicar la política; los demás estados buscan FAQs.
        graph.add_conditional_edges("ScopeGuard", self._route, {
            "policy": "ScopePolicy", "faq": "FAQRetriever", "error": "Error",
        })
        # WARN/CLOSE entregan un texto local; CONTINUE permite continuar la recuperación.
        graph.add_conditional_edges("ScopePolicy", self._route, {
            "response": "Response", "faq": "FAQRetriever", "error": "Error",
        })
        # Una FAQ exacta evita embeddings documentales, planificación y generación final.
        graph.add_conditional_edges("FAQRetriever", lambda s: s.get("route", "next"), {
            "next": "RAGRetriever", "response": "Response", "error": "Error",
        })
        self._connect(graph, "RAGRetriever", "ToolDecision")  # Decide herramientas tras recuperar evidencia.
        for node in ("ToolDecision", "DatabaseTool", "FileTool", "OtherTools"):
            # Consume la cola ya planificada; no vuelve a llamar al LLM entre herramientas.
            graph.add_conditional_edges(node, self._tool_route, {
                "database": "DatabaseTool", "file": "FileTool",
                "other": "OtherTools", "context": "ContextBuilder", "error": "Error",
            })
        self._connect(graph, "ContextBuilder", "LLM")  # Envía al generador la evidencia ya limitada.
        self._connect(graph, "LLM", "Response")  # Solo una respuesta validada pasa al historial.
        self._connect(graph, "Response", "InteractionCounter")  # Cuenta también respuestas locales.
        self._connect(graph, "InteractionCounter", "PostConversationGuard")  # Evalúa el contador actualizado.
        # El turno concluye; Continue no inicia otra petición al modelo.
        graph.add_conditional_edges("PostConversationGuard", self._route, {
            "continue": "Continue", "finalize": "Finalize", "error": "Error",
        })
        graph.add_edge("Error", "InteractionCounter")  # Un fallo de un turno aceptado también consume turno.
        graph.add_edge("Continue", END)  # Espera otra petición HTTP o mensaje de canal.
        graph.add_edge("Finalize", END)  # Finaliza sin respuesta adicional del modelo.
        self.graph = graph.compile()  # Prepara el ejecutor asíncrono del grafo.

    @staticmethod
    def _connect(graph: StateGraph, origin: str, destination: str) -> None:
        # Añade una transición normal y su desvío de error con el mismo comportamiento.
        graph.add_conditional_edges(origin, lambda s: "error" if s.get("route") == "error" else "next", {
            "error": "Error", "next": destination,
        })

    @staticmethod
    def _safe(name: str, node: Callable[[AgentState], Awaitable[dict]]):
        # Envuelve el nodo manteniendo una firma compatible con LangGraph.
        async def execute(state: AgentState) -> dict:
            started = time.perf_counter()  # Inicio de la medición local del nodo.
            try:
                update = await node(state)  # Espera la actualización parcial del nodo.
                decision = update.get("route")  # Conserva la decisión explícita, cuando exista.
                # La traza guarda tiempos y nombres, sin copiar documentos ni prompts.
                event: dict[str, Any] = {"node": name, "status": "ok", "duration_ms": round((time.perf_counter() - started) * 1000, 1)}
                if decision:
                    event["decision"] = decision  # Explica por qué se tomó la siguiente rama.
                # Reinicia route para que una decisión del nodo anterior no se reutilice.
                return {"route": "next", **update, "trace": [*state.get("trace", []), event]}
            except Exception:
                # Los errores del SDK pueden contener claves, consultas o URLs privadas.
                # Devuelve solo un código estable y conserva dónde y cuándo falló.
                return {
                    "error": {"code": "RUNTIME_ERROR", "node": name, "message": DEFAULT_ERROR},
                    "route": "error",
                    "trace": [*state.get("trace", []), {"node": name, "status": "error", "duration_ms": round((time.perf_counter() - started) * 1000, 1)}],
                }
        return execute  # Devuelve el envoltorio; todavía no ejecuta el nodo.

    @staticmethod
    def _route(state: AgentState) -> str:
        return state["route"]  # Lee la rama elegida por el nodo de control.

    @staticmethod
    def _tool_route(state: AgentState) -> str:
        if state.get("error"):
            return "error"  # Interrumpe el plan ante cualquier fallo anterior.
        pending = state.get("pending_tools", [])  # Consulta únicamente la cola restante.
        if not pending:
            return "context"  # Sin herramientas pendientes, prepara la generación final.
        call = pending[0]  # La ejecución respeta el orden del plan.
        # Tolera los alias del contrato de pruebas y el campo name de los hooks reales.
        kind = str(call.get("type", call.get("kind", call.get("name", "other")))).lower()
        if kind in {"database", "database_query", "sql"}:
            return "database"  # Usa el nodo visible de base de datos.
        if kind in {"file", "send_file", "deliverable_file"}:
            return "file"  # Usa el nodo visible de entrega de archivos.
        return "other"  # Agrupa las demás herramientas autorizadas.

    async def run(self, state: dict) -> dict:
        # Conserva el historial durable, pero limpia toda evidencia y resultado del turno anterior.
        initial: AgentState = {
            **deepcopy(state),  # Evita que herramientas o nodos muten el estado del llamador.
            "config": deepcopy(state.get("config", {})),  # Configuración independiente de este turno.
            "messages": deepcopy(state.get("messages", [])),  # Historial independiente de este turno.
            "interaction_count": int(state.get("interaction_count", 0)),  # Normaliza el contador persistido.
            "max_interactions": int(state.get("max_interactions", 10)),  # Normaliza el límite de turnos.
            "conversation_status": state.get("conversation_status", "ACTIVE"),  # Conserva cierres previos.
            # La recuperación se calcula para la nueva pregunta, sin arrastrar fuentes antiguas.
            "scope_status": "UNCERTAIN", "retrieved_documents": [], "retrieved_faqs": [],
            # Un plan anterior no se vuelve a ejecutar accidentalmente.
            "tool_calls": [], "pending_tools": [], "tool_results": [], "attachments": [],
            # Cada invocación contabiliza solo su propia respuesta y su uso.
            "context": "", "response": "", "tokens": 0, "answered": False,
            # Permite recuperar una conversación ERROR en un turno posterior.
            "trace": [], "error": None, "accepted": False,
        }
        if initial["max_interactions"] < 1 or initial["interaction_count"] < 0:
            raise ValueError("Los límites de conversación deben ser positivos.")  # Rechaza estados inválidos.
        # Como máximo se permiten ocho herramientas; 40 pasos dejan margen a los controles.
        return await self.graph.ainvoke(initial, config={"recursion_limit": 40})

    def mermaid(self) -> str:
        return self.graph.get_graph().draw_mermaid()  # Exporta las mismas transiciones que se ejecutan.

    async def _conversation_guard(self, state: AgentState) -> dict:
        closed = state["conversation_status"].upper() in {"CLOSED", "END", "ENDED"}  # Reconoce estados terminales.
        if closed or state["interaction_count"] >= state["max_interactions"]:
            # No acepta la pregunta ni consume tokens cuando la conversación terminó.
            return {
                "conversation_status": "CLOSED", "route": "finalize",
                "response": state["config"].get("closing_message") or DEFAULT_CLOSING,
            }
        messages = list(state["messages"])  # Actualiza una lista nueva de mensajes.
        question = state.get("question", "")  # Obtiene el texto del mensaje entrante.
        # Algunos llamadores ya incluyen la pregunta actual: evita duplicarla en el estado.
        if not messages or messages[-1].get("role") != "user" or messages[-1].get("content") != question:
            messages.append({"role": "user", "content": question})  # Agrega solo la pregunta que falta.
        # accepted se consumirá exactamente una vez en InteractionCounter.
        return {"accepted": True, "messages": messages, "conversation_status": "ACTIVE", "route": "scope"}

    async def _scope_guard(self, state: AgentState) -> dict:
        scope = str(await self.hooks.classify_scope(state)).upper()  # Usa el servicio de clasificación.
        if scope not in {"IN_SCOPE", "UNCERTAIN", "OUT_OF_SCOPE"}:
            scope = "UNCERTAIN"  # Un valor desconocido no se interpreta como rechazo categórico.
        # OUT_OF_SCOPE pasa por la política; una duda sigue a la recuperación.
        return {"scope_status": scope, "route": "policy" if scope == "OUT_OF_SCOPE" else "faq"}

    async def _scope_policy(self, state: AgentState) -> dict:
        policy = str(state["config"].get("out_of_scope_policy", "WARN")).upper()  # Lee la política administrativa.
        if policy not in {"CLOSE", "WARN", "CONTINUE"}:
            raise ValueError("Invalid scope policy")  # Evita aplicar una política no reconocida.
        if policy == "CONTINUE":
            return {"route": "faq"}  # Autoriza responder aunque la clasificación esté fuera de alcance.
        # WARN/CLOSE devuelven texto configurado sin recuperación, herramientas ni generación.
        return {
            "response": state["config"].get("out_of_scope_message") or DEFAULT_OUT_OF_SCOPE,
            "conversation_status": "CLOSED" if policy == "CLOSE" else "ACTIVE",
            "answered": False,
            "route": "response",
        }

    async def _faq(self, state: AgentState) -> dict:
        faqs = await self.hooks.retrieve_faqs(state)  # Busca primero coincidencias exactas, luego semánticas.
        if faqs and faqs[0].get("exact_answer") and state["config"].get("exact_faq_enabled", True):
            # La respuesta aprobada se reutiliza literalmente: no necesita otra llamada al LLM.
            return {"retrieved_faqs": faqs, "response": faqs[0]["exact_answer"], "answered": True, "route": "response"}
        return {"retrieved_faqs": faqs}  # Conserva las FAQs como evidencia y sigue a documentos.

    async def _rag(self, state: AgentState) -> dict:
        return {"retrieved_documents": await self.hooks.retrieve_documents(state)}  # Recupera fragmentos del agente.

    async def _tool_decision(self, state: AgentState) -> dict:
        calls = await self.hooks.decide_tools(state)  # Una única planificación prepara todas las llamadas.
        limit = max(0, min(int(state["config"].get("max_tool_calls", 8)), 8))  # Aplica un techo duro de ocho.
        if len(calls) > limit:
            raise ValueError("Tool call budget exceeded")  # Falla antes de ejecutar cualquier herramienta.
        return {"tool_calls": calls, "pending_tools": calls}  # Separa auditoría completa y cola pendiente.

    async def _tool(self, state: AgentState) -> dict:
        call = state["pending_tools"][0]  # Toma la siguiente llamada elegida por el enrutador.
        result = await self.hooks.execute_tool(call, state)  # Valida permisos y ejecuta en el servicio.
        attachments = list(state.get("attachments", []))  # Preserva adjuntos previos del mismo turno.
        for attachment in result.get("attachments", []):
            if attachment not in attachments:
                attachments.append(attachment)  # Evita repetir adjuntos idénticos.
        # Retira la llamada consumida y acumula evidencia sin replanificar con el modelo.
        return {
            "pending_tools": state["pending_tools"][1:],
            "tool_results": [*state.get("tool_results", []), result], "attachments": attachments,
        }

    async def _context(self, state: AgentState) -> dict:
        # El presupuesto es de caracteres, no de tokens: limita crecimiento de evidencia.
        maximum = max(1000, min(int(state["config"].get("max_context_chars", 32000)), 120000))
        # Recorta contenido y elimina duplicados conservando estructura y referencias de fuentes.
        return {"context": build_evidence(
            state.get("retrieved_faqs", []),  # FAQs relevantes o respuestas aprobadas.
            state.get("retrieved_documents", []),  # Fragmentos documentales recuperados.
            state.get("tool_results", []),  # Resultados de consultas y archivos autorizados.
            maximum,  # El JSON final completo debe caber dentro de este tamaño.
        )}

    async def _generate(self, state: AgentState) -> dict:
        result = await self.hooks.generate(state)  # Los hooks construyen mensajes y llaman al proveedor.
        response = str(result.get("response", "")).strip()  # Normaliza la salida textual.
        if not response:
            raise ValueError("Empty model response")  # Una salida vacía se trata como fallo controlado.
        # Los hooks acumulan clasificación, planificación y respuesta cuando reportan tokens.
        return {"response": response, "tokens": max(0, int(result.get("tokens", 0))),
                "answered": bool(result.get("answered", False))}

    async def _response(self, state: AgentState) -> dict:
        # Agrega al historial en memoria; ConversationService persiste la respuesta una sola vez.
        return {"messages": [*state["messages"], {"role": "assistant", "content": state["response"]}]}

    async def _counter(self, state: AgentState) -> dict:
        # Desactiva accepted después de sumar para impedir incrementos duplicados por rutas de error.
        return {"interaction_count": state["interaction_count"] + int(state.get("accepted", False)), "accepted": False}

    async def _post_guard(self, state: AgentState) -> dict:
        # Cierra exactamente al alcanzar el límite, o si ScopePolicy ya cerró la conversación.
        finish = state["interaction_count"] >= state["max_interactions"] or state["conversation_status"] == "CLOSED"
        return {"route": "finalize" if finish else "continue"}  # Ambas rutas terminan esta invocación.

    async def _continue(self, state: AgentState) -> dict:
        return {"conversation_status": "ERROR" if state.get("error") else "ACTIVE"}  # Permite otro turno.

    async def _finalize(self, state: AgentState) -> dict:
        return {"conversation_status": "CLOSED"}  # Impide llamadas al modelo en mensajes posteriores.

    async def _error(self, state: AgentState) -> dict:
        # No intenta reparar el fallo con otra llamada al modelo, evitando coste adicional oculto.
        return {
            "response": DEFAULT_ERROR, "answered": False,
            "messages": [*state.get("messages", []), {"role": "assistant", "content": DEFAULT_ERROR}],
        }
