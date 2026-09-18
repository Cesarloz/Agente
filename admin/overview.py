from __future__ import annotations

from uuid import uuid4

import streamlit as st

from admin.api import api
from admin.components import agent_selector, agents, download_file, empty, filter_params, page, rows, saved, tags_from_text


def metrics(data: dict) -> None:
    columns = st.columns(3)
    columns[0].metric("Total de preguntas", f"{data.get('total_questions', 0):,}")
    columns[1].metric("Sin respuesta", f"{data.get('unanswered', 0):,}")
    columns[2].metric("Fuera de alcance", f"{data.get('out_of_scope', 0):,}")


def activity_chart(data: dict) -> None:
    st.subheader("Preguntas por día")
    days = data.get("by_day", [])
    if days:
        st.bar_chart(days, x="day", y="count", color="#0891B2", x_label="Día", y_label="Preguntas")
    else:
        empty("Aún no hay actividad", "Las preguntas aparecerán aquí cuando pruebes un agente o conectes un canal.")


@page("Dashboard", "Una vista de tus agentes, su actividad y el conocimiento que utilizan.")
def dashboard_page() -> None:
    available = agents()
    data = api.get("/api/analytics")
    head = st.columns(4)
    head[0].metric("Agentes", len(available))
    head[1].metric("Activos", sum(bool(item.get("active", True)) for item in available))
    head[2].metric("Preguntas", data.get("total_questions", 0))
    head[3].metric("Sin respuesta", data.get("unanswered", 0))
    st.divider()
    main, aside = st.columns([2, 1], gap="large")
    with main:
        activity_chart(data)
        st.subheader("Tus agentes")
        if not available:
            empty("Comienza con tu primer agente", "Abre Agentes, elige Crear un agente y define su nombre y propósito.")
        for item in available:
            with st.container(border=True):
                title, status = st.columns([3, 1])
                title.markdown(f"**{item['name']}**")
                status.caption("● Activo" if item.get("active", True) else "○ Inactivo")
                st.caption(item.get("description") or "Sin descripción")
                st.caption(f"{item.get('provider', 'Sin proveedor')} · {item.get('model') or 'Modelo pendiente'} · Máximo {item.get('max_interactions', 10)} interacciones")
                if item.get("published") and item.get("public_url"):
                    st.link_button("Abrir conversación pública", item["public_url"])
                else:
                    st.caption("Página de conversación sin publicar · Configúrala en Agentes → Publicación.")
    with aside:
        with st.container(border=True):
            st.subheader("Puesta en marcha")
            st.markdown("1. **Conecta un proveedor** en Configuración.\n2. **Configura tu agente:** prompt, modelo y alcance.\n3. **Personaliza su logo** en Agentes → Apariencia.\n4. **Agrega conocimiento:** FAQ y documentos.\n5. **Prueba las respuestas** en Playground.\n6. **Publica la página de conversación** en Agentes → Publicación y comparte su enlace.\n7. **Conecta Telegram o WhatsApp** si necesitas otros canales.")
        with st.container(border=True):
            st.subheader("Atención pendiente")
            st.metric("Preguntas fuera de alcance", data.get("out_of_scope", 0))
            st.caption("Revisa Preguntas de usuarios para detectar vacíos de conocimiento y convertir consultas en FAQ.")


def render_attachments(agent_id: str, attachments: list, prefix: str) -> None:
    for index, attachment in enumerate(attachments):
        if not isinstance(attachment, dict):
            continue
        file_id = attachment.get("file_id") or attachment.get("id")
        if file_id:
            name = attachment.get("name") or attachment.get("filename") or "archivo"
            st.caption(f"Archivo adjunto: {name}")
            download_file(agent_id, file_id, name, f"{prefix}_{index}_{file_id}")


def render_messages(agent_id: str, messages: list, prefix: str = "history") -> None:
    for index, message in enumerate(messages):
        role = message.get("role", "assistant")
        if role not in ("user", "assistant"):
            continue
        with st.chat_message(role):
            st.markdown(message.get("content") or message.get("text") or "")
            metadata = message.get("metadata") or message.get("extra") or {}
            if not isinstance(metadata, dict):
                metadata = {}
            attachments = message.get("attachments") or metadata.get("attachments") or []
            render_attachments(agent_id, attachments, f"{prefix}_{index}")


@page("Playground", "Prueba el comportamiento real del agente con su conocimiento, herramientas y memoria persistente.")
def playground_page() -> None:
    agent = agent_selector(key="ui_playground_agent")
    if not agent:
        return
    key = f"ui_playground_cid_{agent['id']}"
    trace_key = f"ui_playground_trace_{agent['id']}"
    conversation_id = st.session_state.get(key)
    left, right = st.columns([3, 1])
    left.caption(f"{agent.get('provider', '')} / {agent.get('model') or 'Selecciona un modelo en Agentes'}")
    if right.button("Nueva conversación", width="stretch"):
        st.session_state.pop(key, None)
        st.session_state.pop(trace_key, None)
        st.rerun()
    conversation = api.get(f"/api/conversations/{conversation_id}") if conversation_id else None
    status = (conversation or {}).get("status", (conversation or {}).get("conversation_status", "active"))
    closed = str(status).lower() in ("closed", "ended", "completed", "end")
    if conversation:
        st.caption(f"Conversación {conversation_id} · {conversation.get('interaction_count', 0)} / {agent.get('max_interactions', 10)} interacciones · {status}")
        render_messages(agent["id"], conversation.get("messages", []), f"playground_{conversation_id}")
    elif agent.get("welcome_message"):
        with st.chat_message("assistant"):
            st.markdown(agent["welcome_message"])
    else:
        empty("Una nueva conversación", "Envía una pregunta para probar el agente. Puedes revisar después el historial y las métricas.")
    if closed:
        st.info("Esta conversación ha finalizado. Inicia una nueva para seguir probando.")
    if not agent.get("active", True):
        st.warning("El agente está inactivo. Actívalo en Agentes → General.")
    trace = st.session_state.get(trace_key)
    if trace and trace.get("conversation_id") == conversation_id:
        if trace.get("error"):
            st.error(trace["error"])
        with st.expander("Ejecución del agente"):
            if trace.get("timings"):
                timing = trace["timings"]
                st.caption(f"Total: {timing.get('total_ms', 0) / 1000:.2f} s · Espera inicial: {timing.get('wait_ms', 0) / 1000:.2f} s")
            usage = trace.get("usage")
            if usage is not None:
                st.caption(f"Llamadas al LLM: {usage.get('llm_calls', 0)} · Tokens reportados: {usage.get('total_tokens', 0)} · Entrada: {usage.get('input_tokens', 0)} · Salida: {usage.get('output_tokens', 0)}. No incluye embeddings.")
                if not usage.get("usage_complete", False):
                    st.warning("El proveedor no informó el consumo de alguna llamada; el total mostrado es parcial.")
            stages = [{"Etapa": event.get("node", ""), "Tiempo (ms)": event.get("duration_ms", 0), "Estado": event.get("status", ""),
                       "Tokens LLM": event.get("llm_usage", {}).get("total_tokens") if event.get("llm_usage", {}).get("usage_reported") else None}
                      for event in trace.get("trace", []) if isinstance(event, dict)]
            if stages:
                st.dataframe(stages, hide_index=True, width="stretch")
            st.json(trace)
    message = st.chat_input("Escribe una pregunta para el agente…", disabled=closed or not agent.get("active", True))
    if message and not closed and agent.get("active", True):
        with st.chat_message("user"):
            st.markdown(message)
        with st.spinner("El agente está preparando la respuesta…"):
            result = api.post(f"/api/agents/{agent['id']}/chat", json={"message": message, "conversation_id": conversation_id, "user_id": "playground", "channel": "playground"})
        st.session_state[key] = result["conversation_id"]
        # Only an ephemeral diagnostic view is kept here; all chat content is
        # reloaded through MemoryService, including authenticated attachments.
        st.session_state[trace_key] = {"conversation_id": result["conversation_id"], "scope_status": result.get("scope_status"), "conversation_status": result.get("conversation_status"), "trace": result.get("trace", []), "error": result.get("error"), "timings": result.get("timings"), "usage": result.get("usage")}
        st.rerun()


@page("Conversaciones", "Historial de Playground, la página pública, Telegram y WhatsApp.")
def conversations_page() -> None:
    agent = agent_selector(optional=True, key="ui_conversations_agent")
    items = rows(api.get("/api/conversations", params=filter_params(agent)), "conversations")
    if not items:
        empty("No hay conversaciones", "Prueba un agente en Playground o recibe una consulta desde un canal conectado.")
        return
    st.dataframe([
        {"ID": item["id"], "Agente": item.get("agent_id"), "Usuario": item.get("user_id"), "Canal": item.get("channel"), "Estado": item.get("status", item.get("conversation_status")), "Interacciones": item.get("interaction_count", 0), "Fecha": item.get("created_at")}
        for item in items
    ], hide_index=True, width="stretch")
    selected = st.selectbox("Abrir conversación", items, format_func=lambda item: f"{item.get('channel', '')} · {item.get('user_id', '')} · {item.get('created_at', '')} · {item['id'][:8]}")
    detail = api.get(f"/api/conversations/{selected['id']}")
    st.subheader("Mensajes")
    render_messages(detail.get("agent_id", selected.get("agent_id", "")), detail.get("messages", []), f"conversation_{selected['id']}")
    if not detail.get("messages"):
        st.info("Esta conversación todavía no tiene mensajes.")
    with st.expander("Enviar respuesta por correo"):
        if st.checkbox("Preparar un correo para esta conversación", key=f"prepare_email_{selected['id']}"):
            email_composer(detail)
    status = detail.get("status", detail.get("conversation_status", "active"))
    if str(status).lower() not in ("closed", "ended", "completed", "end"):
        if st.button("Finalizar conversación"):
            api.post(f"/api/conversations/{selected['id']}/close")
            saved("Conversación finalizada.")


def email_composer(conversation: dict) -> None:
    account = api.get("/api/email/gmail")
    if not account.get("configured"):
        st.info("Conecta tu cuenta en Configuración → Gmail para enviar correos.")
        return
    conversation_id = conversation["id"]
    st.caption(f"Remitente: {account['sender']}. Revisa el destinatario y el contenido antes de pulsar Enviar.")
    latest = next((message.get("content", "") for message in reversed(conversation.get("messages", [])) if message.get("role") == "assistant"), "")
    request_key = f"email_request_{conversation_id}"
    if request_key not in st.session_state:
        st.session_state[request_key] = str(uuid4())
    if st.button("Preparar nuevo correo", key=f"new_email_{conversation_id}"):
        st.session_state[request_key] = str(uuid4())
        st.rerun()
    with st.form(f"email_{conversation_id}_{st.session_state[request_key]}"):
        recipient = st.text_input("Correo del destinatario", placeholder="persona@ejemplo.com")
        subject = st.text_input("Asunto del correo", value="Respuesta e información de tu consulta")
        response = st.text_area("Respuesta para enviar", value=latest, height=190)
        extra = st.text_area("Información adicional", placeholder="Puedes agregar detalles y enlaces de seguimiento.", height=100)
        if st.form_submit_button("Enviar correo por Gmail", type="primary"):
            content = "\n\n".join(part.strip() for part in (response, extra) if part.strip())
            if not recipient.strip() or not subject.strip() or not content:
                st.error("Completa el destinatario, asunto y contenido del correo.")
            else:
                result = api.post(f"/api/conversations/{conversation_id}/email", json={"recipient": recipient.strip(), "subject": subject.strip(), "body": content, "request_id": st.session_state[request_key]})
                st.session_state[f"email_job_{conversation_id}"] = result["job_id"]
                st.success("Correo registrado en la cola. Consulta su estado abajo; registrarlo aún no confirma su entrega.")
    job_id = st.session_state.get(f"email_job_{conversation_id}")
    if job_id:
        if st.button("Actualizar estado del correo", key=f"check_email_{conversation_id}"):
            st.rerun()
        jobs = rows(api.get("/api/jobs", params={"agent_id": conversation["agent_id"]}), "jobs")
        job = next((item for item in jobs if item["id"] == job_id), None)
        if job:
            labels = {"DONE": "Gmail aceptó el correo para envío", "PENDING": "Pendiente", "RUNNING": "Enviando", "FAILED": "Falló", "NEEDS_REVIEW": "Revisa Enviados en Gmail: el resultado es incierto"}
            st.caption(f"Estado: {labels.get(job['status'], job['status'])} · {job_id}")
            if job.get("error"):
                st.error(job["error"])
            if job["status"] in {"FAILED", "NEEDS_REVIEW"}:
                st.caption("Puedes revisar y reintentar este envío en Agentes → Canales → Trabajos pendientes de atención.")


def convert_question(question: dict, *, prefix: str = "question") -> None:
    st.markdown("**Convertir en FAQ**")
    st.caption("La nueva FAQ se guarda y vectoriza para el agente que recibió la pregunta.")
    with st.form(f"{prefix}_faq_{question['id']}"):
        st.markdown(question.get("question", ""))
        answer = st.text_area("Respuesta revisada", value=question.get("response") or "", height=160)
        tags = st.text_input("Tags separados por coma")
        priority = st.number_input("Prioridad", min_value=0, max_value=100, value=0)
        if st.form_submit_button("Crear FAQ desde esta pregunta", type="primary"):
            if not answer.strip():
                st.error("Escribe una respuesta antes de crear la FAQ.")
            else:
                with st.spinner("Creando FAQ…"):
                    api.post(f"/api/questions/{question['id']}/faq", json={"answer": answer, "tags": tags_from_text(tags), "priority": priority})
                saved("Pregunta convertida en FAQ. La indexación continúa en el backend.")


@page("Preguntas de usuarios", "Identifica consultas sin resolver y transforma preguntas recurrentes en conocimiento útil.")
def questions_page() -> None:
    agent = agent_selector(optional=True, key="ui_questions_agent")
    left, middle, right = st.columns([1, 1, 2])
    unanswered = left.checkbox("Solo sin respuesta")
    out_of_scope = middle.checkbox("Solo fuera de alcance")
    search = right.text_input("Buscar pregunta", label_visibility="collapsed", placeholder="Buscar en las preguntas…")
    params = filter_params(agent)
    if unanswered:
        params["unanswered"] = "true"
    if out_of_scope:
        params["out_of_scope"] = "true"
    items = rows(api.get("/api/questions", params=params), "questions")
    items = [item for item in items if search.casefold() in item.get("question", "").casefold()]
    if not items:
        empty("No hay preguntas con estos filtros", "Ajusta los filtros o prueba un agente para comenzar a registrar actividad.")
        return
    st.caption(f"{len(items)} preguntas")
    st.dataframe([
        {"Pregunta": item["question"], "Respondida": bool(item.get("answered")), "Alcance": item.get("scope_status"), "Canal": item.get("channel"), "FAQ": bool(item.get("faq_used")), "RAG": bool(item.get("rag_used")), "SQL": bool(item.get("database_used")), "Modelo": item.get("model"), "Tokens": item.get("tokens"), "Latencia (ms)": item.get("latency"), "Fecha": item.get("timestamp", item.get("created_at"))}
        for item in items
    ], hide_index=True, width="stretch")
    selected = st.selectbox("Detalle de pregunta", items, format_func=lambda item: f"{item['question'][:110]} · {item['id'][:8]}")
    with st.expander("Registro completo"):
        st.json(selected)
    convert_question(selected)


def ranking(title: str, entries: list[dict], name_label: str) -> None:
    st.subheader(title)
    if not entries:
        empty("Sin recuperaciones registradas", "Los resultados aparecerán cuando el agente utilice estas fuentes.")
    else:
        st.dataframe([{name_label: entry.get("name") or entry.get("id"), "Usos": entry.get("count", 0)} for entry in entries], hide_index=True, width="stretch")


@page("Analítica", "Mide la actividad y encuentra oportunidades para mejorar el conocimiento de tus agentes.")
def analytics_page() -> None:
    agent = agent_selector(optional=True, key="ui_analytics_agent")
    data = api.get("/api/analytics", params=filter_params(agent))
    metrics(data)
    st.divider()
    activity_chart(data)
    left, right = st.columns(2, gap="large")
    with left:
        ranking("FAQ más utilizadas", data.get("top_faqs", []), "Pregunta frecuente")
    with right:
        ranking("Documentos más utilizados", data.get("top_documents", []), "Documento")
    if data.get("usage_definition"):
        st.caption(data["usage_definition"])
    st.divider()
    st.subheader("Preguntas similares")
    st.caption(data.get("similarity_method") or "Grupos propuestos por el backend para detectar posibles nuevas FAQ. Revisa siempre la respuesta antes de publicarla como conocimiento.")
    similar = data.get("similar_questions", [])
    if not similar:
        empty("Aún no hay grupos de preguntas similares", "Los grupos aparecerán cuando exista suficiente actividad comparable.")
    else:
        st.dataframe([{"Pregunta representativa": item.get("question"), "Consultas": item.get("count", 0)} for item in similar], hide_index=True, width="stretch")
        selected = st.selectbox("Grupo de preguntas", similar, format_func=lambda item: f"{item.get('question', '')} · {item.get('count', 0)} consultas")
        ids = selected.get("question_ids", [])
        if ids:
            questions = rows(api.get("/api/questions", params=filter_params(agent)), "questions")
            matches = [question for question in questions if question["id"] in ids]
            if matches:
                question = st.selectbox("Pregunta para convertir", matches, format_func=lambda item: item["question"])
                convert_question(question, prefix="analytics")
