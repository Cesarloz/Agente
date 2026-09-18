"""Historial durable y coordinación de un turno de chat con auditoría de fuentes y uso."""

import asyncio  # Impone un tiempo máximo a la ejecución completa del grafo.
import hashlib  # Genera una clave estable de bloqueo entre procesos para cada conversación.
import time  # Mide la espera, el tiempo de ejecución y la latencia total.

from sqlalchemy import delete, select, text  # Construye consultas de memoria y bloqueos de PostgreSQL.

from app.errors import AppError  # Errores de aplicación con mensaje y estado HTTP controlados.
from app.models import Agent, Conversation, Message, Question, User  # Entidades persistidas durante el chat.
from app.repositories import Repository, serialize  # Acceso transaccional y conversión a diccionarios públicos.
from app.schemas import ChatInput  # Mensaje entrante validado: texto, usuario, canal y conversación opcional.
from app.services.runtime_hooks import ApplicationRuntimeHooks  # Conecta el grafo con modelos y RAG.


class MemoryService:
    """Lee y escribe mensajes; leer memoria no consulta un modelo ni genera un resumen LLM."""

    def __init__(self, session):
        self.session = session  # Reutiliza la transacción del servicio de conversación.

    async def messages(self, conversation_id, limit=200):
        # Consulta solo los mensajes recientes; el límite se aplica en SQL, antes de cargarlos.
        result = list(await self.session.scalars(select(Message).where(Message.conversation_id == conversation_id).order_by(Message.created_at.desc(), Message.id.desc()).limit(limit)))
        return [serialize(m) for m in reversed(result)]  # Restaura el orden cronológico para el prompt y la UI.

    async def append(self, conversation_id, role, content, attachments=None):
        # Añade el mensaje en la transacción actual; el llamador decide cuándo confirmar todo el turno.
        return await Repository(self.session, Message).add(conversation_id=conversation_id, role=role, content=content, attachments=attachments or [])

    async def clear(self, agent_id):
        # Borra únicamente mensajes de conversaciones del agente solicitado.
        await self.session.execute(delete(Message).where(Message.conversation_id.in_(select(Conversation.id).where(Conversation.agent_id == agent_id))))
        # Conserva contadores y auditoría: limpiar memoria no permite eludir límites de conversación.
        await self.session.commit()  # Confirma el borrado de historial.
        return {"cleared": True}  # Devuelve una confirmación sin datos privados de mensajes.


class ConversationService:
    """Ejecuta un mensaje y guarda estado, respuesta y consumo en una misma transacción."""

    def __init__(self, session, hooks_factory=ApplicationRuntimeHooks):
        self.session, self.hooks_factory = session, hooks_factory  # Permite hooks falsos en pruebas sin APIs.

    async def list(self, agent_id=None):
        filters = [Conversation.agent_id == agent_id] if agent_id else []  # Filtra por agente cuando se indica.
        return [serialize(c) for c in await Repository(self.session, Conversation).list(*filters)]  # Lista pública.

    async def get(self, id):
        row = await Repository(self.session, Conversation).get(id)  # Verifica que la conversación exista.
        return {**serialize(row), "messages": await MemoryService(self.session).messages(id)}  # Añade historial reciente.

    async def close(self, id):
        row = await Repository(self.session, Conversation).get(id, lock=True)  # Serializa cambios concurrentes.
        row.status = "CLOSED"  # ConversationGuard rechazará posteriores llamadas al modelo.
        await self.session.commit()  # Hace durable el cierre explícito.
        return serialize(row)  # Devuelve el estado ya guardado.

    async def chat(self, agent_id, data: ChatInput, result_job=None, on_response=None):
        request_started = time.perf_counter()  # Incluye carga de configuración y espera por bloqueos.
        from app.runtime import AgentRuntime  # Carga el grafo cuando se necesita ejecutar una pregunta.
        agent = await Repository(self.session, Agent).get(agent_id)  # Obtiene configuración vigente.
        if not agent.config["active"]:
            raise AppError("El agente está desactivado", 409)  # Rechaza el turno antes de gastar tokens.
        # PostgreSQL serializa turnos entre procesos API/worker; SQLite de pruebas no simula esta garantía.
        key = f"{agent_id}:{data.conversation_id or data.channel + ':' + data.user_id}"
        if self.session.bind.dialect.name == "postgresql":
            lock_id = int.from_bytes(hashlib.sha256(key.encode()).digest()[:8], signed=True)  # Entero de 64 bits estable.
            await self.session.execute(text("SELECT pg_advisory_xact_lock(:key)"), {"key": lock_id})  # Hasta el commit.
        wait_ms = round((time.perf_counter() - request_started) * 1000, 1)  # Espera y preparación anteriores al chat.
        if data.conversation_id:
            # Bloquea la fila y comprueba que pertenece al agente solicitado.
            conversation = await Repository(self.session, Conversation).get(data.conversation_id, agent_id, lock=True)
            if conversation.user_id != data.user_id or conversation.channel != data.channel:
                raise AppError("La conversación pertenece a otro usuario o canal", 403)  # Evita cruzar historiales.
        else:
            conversation = None  # Sin identificador explícito se decide si reusar o crear una conversación.
            if data.channel in {"telegram", "whatsapp"}:
                # Los canales continúan la conversación más reciente del mismo agente, usuario y canal.
                conversation = await self.session.scalar(select(Conversation).where(Conversation.agent_id == agent_id, Conversation.user_id == data.user_id, Conversation.channel == data.channel).order_by(Conversation.created_at.desc()).limit(1).with_for_update())
                if data.message.strip() == "/start":
                    conversation = None  # El comando explícito abre una conversación nueva.
            if conversation is None:
                # API/web sin ID y canales sin conversación previa crean estado durable nuevo.
                conversation = await Repository(self.session, Conversation).add(agent_id=agent_id, user_id=data.user_id, channel=data.channel)
        user_key = f"{agent_id}:{data.channel}:{data.user_id}"  # Aísla la identidad externa por agente y canal.
        if not await self.session.scalar(select(User).where(User.external_id == user_key)):
            await Repository(self.session, User).add(external_id=user_key)  # Registra al usuario la primera vez.
        memory = MemoryService(self.session)  # Comparte transacción con historial, pregunta y conversación.
        # La ventana limita mensajes persistidos que se leen; hooks aplica además su presupuesto de prompt.
        history = await memory.messages(conversation.id, agent.config["memory_window"]) if agent.config["memory_enabled"] and agent.config["memory_window"] else []
        # Inicia auditoría del turno con proveedor/modelo configurados, sin guardar credenciales.
        question = await Repository(self.session, Question).add(agent_id=agent_id, conversation_id=conversation.id, user_id=data.user_id, channel=data.channel, question=data.message, provider=agent.config["provider"], model=agent.config["model"])
        await memory.append(conversation.id, "user", data.message)  # Persiste el mensaje actual una sola vez.
        started = time.perf_counter()  # Mide solo la fase de ejecución y su auditoría inmediata.
        hooks = self.hooks_factory(self.session, agent_id, agent.config)  # Cachés y contador de uso por petición.
        hooks.on_response = on_response  # Callback opcional: solo publica el texto de generación final.
        # Al modelo llega solo role/content del historial; IDs internos y adjuntos no se copian como memoria.
        state = {"agent_id": agent_id, "conversation_id": conversation.id, "config": agent.config, "question": data.message, "messages": [{"role": h["role"], "content": h["content"]} for h in history], "interaction_count": conversation.interaction_count, "max_interactions": agent.config["max_interactions"], "conversation_status": conversation.status}
        try:
            result = await asyncio.wait_for(AgentRuntime(hooks).run(state), timeout=150)  # Límite global del turno.
        except asyncio.CancelledError:
            if on_response is None:
                raise  # Mantiene la semántica de cancelación de procesos y canales sin streaming.
            # Al desconectar se detiene la generación; la sesión del productor sigue viva para guardar uso conocido.
            result = {**state, "response": "La respuesta se interrumpió. Puedes enviar una nueva pregunta.", "answered": False,
                      "error": {"code": "STREAM_INTERRUPTED"}, "scope_status": "UNCERTAIN",
                      "interaction_count": min(conversation.interaction_count + 1, agent.config["max_interactions"]),
                      "conversation_status": "ERROR"}
        except Exception:
            # Un timeout o fallo fuera de un nodo produce respuesta local; no reintenta el LLM.
            result = {**state, "response": "No fue posible procesar la pregunta. Inténtalo de nuevo.", "answered": False, "error": {"code": "RUNTIME_FAILURE"}, "scope_status": "UNCERTAIN", "interaction_count": min(conversation.interaction_count + 1, agent.config["max_interactions"]), "conversation_status": "ERROR"}
        conversation.interaction_count = result["interaction_count"]  # Persiste el contador decidido por el grafo.
        conversation.status = result["conversation_status"]  # Conserva cierre, actividad o error recuperable.
        question.scope_status = result.get("scope_status", "UNCERTAIN")  # Registra la clasificación del turno.
        # Deduplica IDs de fuentes para auditoría, descartando fragmentos sin source_id.
        question.faq_ids = list({r.get("metadata", {}).get("source_id") for r in result.get("retrieved_faqs", [])} - {None})
        question.document_ids = list({r.get("metadata", {}).get("source_id") for r in result.get("retrieved_documents", [])} - {None})
        question.faq_used, question.rag_used = bool(question.faq_ids), bool(question.document_ids)  # Fuentes recuperadas.
        question.database_used = any(r.get("name") == "database" for r in result.get("tool_results", []))  # SQL ejecutado.
        question.answered, question.response = result.get("answered", False), result.get("response", "")  # Salida auditada.
        # Usa el acumulado de hooks si no hubo generación final: incluye alcance/planificación ya consumidos.
        question.tokens = result.get("tokens", 0) or getattr(hooks, "tokens", 0)
        runtime_ms = round((time.perf_counter() - started) * 1000, 1)  # Duración de procesamiento en milisegundos.
        question.latency = int((time.perf_counter() - request_started) * 1000)  # Latencia observada hasta auditoría.
        question.trace = result.get("trace", [])  # Guarda nodos y tiempos, sin mensajes internos del proveedor.
        usage = getattr(hooks, "usage", [])  # Los hooks de pruebas antiguos pueden no exponer desglose LLM.
        usage_nodes = {"ScopeResult": "ScopeGuard", "ToolPlan": "ToolDecision", "Answer": "LLM"}  # Esquema a nodo.
        for event in usage:
            for node in question.trace:
                if node.get("node") == usage_nodes.get(event.get("stage")):
                    node["llm_usage"] = dict(event)  # Persiste uso incluso si el parseo del nodo falló.
                    break  # Cada etapa LLM aparece una sola vez por turno en este grafo.
            else:
                # Un timeout global pierde el estado parcial de LangGraph, pero hooks retiene los intentos.
                question.trace.append({
                    "node": usage_nodes.get(event.get("stage"), "LLM"),  # Reconstruye la etapa identificable.
                    "status": "interrupted",  # La traza del turno quedó interrumpida; no infiere éxito del nodo.
                    "duration_ms": None,  # No inventa un tiempo cero para una duración que no se recuperó.
                    "duration_known": False,  # Distingue estas entradas de los nodos cronometrados normalmente.
                    "llm_usage": dict(event),  # Conserva consumo conocido o indica que el proveedor no lo reportó.
                })
        question.error = "Error al procesar la pregunta" if result.get("error") else None  # Error público uniforme.
        await memory.append(conversation.id, "assistant", question.response, result.get("attachments", []))  # Respuesta durable.
        # La respuesta de API/canal contiene estado, salida y diagnóstico público del turno.
        response = {"conversation_id": conversation.id, "response": question.response, "interaction_count": conversation.interaction_count, "conversation_status": conversation.status, "scope_status": question.scope_status, "attachments": result.get("attachments", []), "trace": question.trace, "error": question.error}
        response["timings"] = {"total_ms": question.latency, "wait_ms": wait_ms, "runtime_ms": runtime_ms}  # Desglose temporal.
        if getattr(hooks, "first_response_ms", None) is not None:
            response["timings"]["first_response_ms"] = round((started - request_started) * 1000 + hooks.first_response_ms, 1)
        # Son tokens LLM reportados; embeddings e intentos sin respuesta del proveedor no están incluidos.
        response["usage"] = {
            "llm_calls": len(usage),  # Número de invocaciones registradas por los hooks del turno.
            "input_tokens": sum(event.get("input_tokens", 0) for event in usage),  # Entrada acumulada conocida.
            "output_tokens": sum(event.get("output_tokens", 0) for event in usage),  # Salida acumulada conocida.
            "total_tokens": sum(event.get("total_tokens", 0) for event in usage),  # Total informado por proveedor.
            "usage_complete": all(event.get("usage_reported", False) for event in usage),  # Detecta datos ausentes.
            "stages": usage,  # Permite identificar cuánto costaron alcance, planificación y respuesta.
        }
        if result_job is not None:
            # Guarda resultado y conversación juntos: un reintento de entrega no vuelve a gastar tokens.
            result_job.payload = {**result_job.payload, "response_result": response}
        await self.session.commit()  # Confirma mensajes, contador, uso y, cuando existe, resultado del job.
        return response  # El canal entrega exactamente la respuesta ya persistida.
