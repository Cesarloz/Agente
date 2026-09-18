from __future__ import annotations

import streamlit as st

from admin.api import ApiError, api
from admin.components import agent_selector, download_file, empty, page, rows, saved, tags_from_text


SECTIONS = ["General", "Apariencia", "Enlaces", "System Prompt", "Modelo", "Alcance", "Conversación", "FAQ", "Knowledge Base", "Bases de datos", "Archivos", "Memoria", "Herramientas", "Canales", "Publicación"]
MAX_LOGO_BYTES = 2 * 1024 * 1024


@page("Agentes", "Diseña el comportamiento, conecta conocimiento y configura los canales de cada agente.")
def agent_page() -> None:
    with st.expander("＋ Crear un agente"):
        with st.form("create_agent", clear_on_submit=True):
            name = st.text_input("Nombre del agente", max_chars=160)
            description = st.text_area("Descripción", height=90)
            if st.form_submit_button("Crear agente", type="primary"):
                if not name.strip():
                    st.error("Escribe un nombre para el agente.")
                else:
                    created = api.post("/api/agents", json={"name": name.strip(), "description": description})
                    st.session_state["ui_agent"] = created["id"]
                    saved("Agente creado. Configura un proveedor y modelo para comenzar.")
    agent = agent_selector()
    if not agent:
        return
    agent = api.get(f"/api/agents/{agent['id']}")
    st.caption(f"ID: {agent['id']} · {'Activo' if agent.get('active', True) else 'Inactivo'}")
    section = st.selectbox("Sección del agente", SECTIONS, key="ui_agent_section")
    handlers = {
        "General": general, "Apariencia": appearance, "Enlaces": response_links, "System Prompt": prompts, "Modelo": model,
        "Alcance": scope, "Conversación": conversation_settings, "FAQ": faqs,
        "Knowledge Base": documents, "Bases de datos": databases, "Archivos": files,
        "Memoria": memory, "Herramientas": tools, "Canales": channels, "Publicación": publication,
    }
    handlers[section](agent)


def update(agent: dict, payload: dict) -> None:
    api.patch(f"/api/agents/{agent['id']}", payload)
    saved()


def general(agent: dict) -> None:
    st.subheader("Información general")
    with st.form(f"general_{agent['id']}"):
        name = st.text_input("Nombre", value=agent["name"])
        description = st.text_area("Descripción", value=agent.get("description", ""))
        avatar = st.text_input("Avatar", value=agent.get("avatar", "") or "", placeholder="URL de una imagen", help="Para subir tu propio logo, abre la sección Apariencia. El logo subido tiene prioridad sobre esta URL.")
        welcome = st.text_area("Mensaje de bienvenida", value=agent.get("welcome_message", "") or "")
        active = st.checkbox("Agente activo", value=agent.get("active", True))
        if st.form_submit_button("Guardar", type="primary"):
            if not name.strip():
                st.error("El nombre es obligatorio.")
            else:
                update(agent, {"name": name.strip(), "description": description, "avatar": avatar, "welcome_message": welcome, "active": active})


def appearance(agent: dict) -> None:
    st.subheader("Apariencia del agente")
    st.caption("Personaliza el logo que verán las personas al abrir la página de conversación del agente.")
    base = f"/api/agents/{agent['id']}/logo"
    if agent.get("logo_url"):
        try:
            st.image(api.get(base, raw=True), width=160, caption="Logo actual")
        except ApiError as exc:
            st.warning(f"No se pudo mostrar el logo actual. {exc}")
        if st.button("Eliminar logo", key=f"delete_logo_{agent['id']}"):
            api.delete(base)
            saved("Logo eliminado.")
    else:
        st.info("Todavía no has subido un logo para este agente.")
    uploaded = st.file_uploader(
        "Subir logo", type=["png", "jpg", "jpeg", "webp"],
        key=f"logo_upload_{agent['id']}", help="PNG, JPEG o WebP. Tamaño máximo: 2 MB.",
    )
    oversized = uploaded is not None and uploaded.size > MAX_LOGO_BYTES
    if oversized:
        st.error("El logo supera el tamaño máximo de 2 MB. Elige una imagen más pequeña.")
    if st.button("Guardar logo", type="primary", disabled=uploaded is None or oversized):
        with st.spinner("Guardando logo…"):
            api.upload(base, uploaded)
        saved("Logo guardado. La página pública utilizará esta imagen.")


def response_links(agent: dict) -> None:
    st.subheader("Enlaces para las respuestas")
    st.caption("El agente puede ofrecer estas ligas cuando sean pertinentes. Aparecen también como accesos en su página pública.")
    st.info("Para reservar citas, usa tu enlace personal de Microsoft Bookings. La dirección general abre Book With Me, pero no identifica tu agenda.")
    with st.form(f"links_{agent['id']}"):
        content = st.text_area("Una liga por línea: título | URL", value="\n".join(f"{item['label']} | {item['url']}" for item in agent.get("response_links", [])),
            placeholder="Agendar una cita | https://outlook.office.com/bookwithme/", height=180)
        if st.form_submit_button("Guardar enlaces", type="primary"):
            parsed = [line.split("|", 1) for line in content.splitlines() if line.strip()]
            if any(len(item) != 2 or not all(part.strip() for part in item) for item in parsed):
                st.error("Cada línea debe contener un título y una URL separados por |.")
            else:
                update(agent, {"response_links": [{"label": label.strip(), "url": url.strip()} for label, url in parsed]})


def publication(agent: dict) -> None:
    st.subheader("Publicar página del agente")
    st.caption("Crea una página de conversación con el nombre, logo y mensaje de bienvenida de este agente.")
    credentials = rows(api.get("/api/credentials"), "credentials")
    provider_ready = any(item.get("provider") == agent.get("provider") and item.get("configured") for item in credentials)
    checks = [
        (agent.get("active", True), "Agente activo", "Activa el agente en General."),
        (bool(agent.get("model")), "Modelo de conversación seleccionado", "Selecciona un modelo en Modelo."),
        (provider_ready, "Credencial del proveedor configurada", "Guarda la API key del proveedor en Configuración."),
    ]
    with st.container(border=True):
        st.markdown("**Preparación para publicar**")
        for ready, label, guidance in checks:
            st.write(f"{'✓' if ready else '○'} {label}")
            if not ready:
                st.caption(guidance)
    st.info("La página publicada permite que cualquier persona con el enlace converse con el agente. Las respuestas utilizan la API key configurada y pueden generar consumo del proveedor.")
    st.caption("En el deploy, PUBLIC_BASE_URL debe ser la dirección pública del backend FastAPI, accesible desde el navegador de tus visitantes. Esa dirección se utiliza para generar el enlace de conversación.")
    base = f"/api/agents/{agent['id']}/publish"
    if not agent.get("published"):
        if st.button("Publicar página", type="primary", disabled=not all(ready for ready, _, _ in checks)):
            with st.spinner("Publicando página de conversación…"):
                result = api.post(base)
            agent = {**agent, **result}
            st.success("Página publicada. Ya puedes compartir el enlace del agente.")
    if agent.get("published"):
        public_url = agent.get("public_url")
        st.success("Este agente tiene una página de conversación publicada.")
        if public_url:
            st.link_button("Abrir página del agente", public_url, type="primary")
            st.code(public_url, language=None, wrap_lines=True)
        else:
            st.warning("La API no devolvió la dirección pública. Revisa PUBLIC_BASE_URL en el backend.")
        if not all(ready for ready, _, _ in checks):
            st.warning("La página está publicada, pero el agente necesita completar los requisitos anteriores para responder.")
        st.caption("Los cambios que guardes en el agente se reflejan en su página. Retirar la publicación impide iniciar o continuar conversaciones desde ese enlace.")
        if st.button("Retirar publicación"):
            api.delete(base)
            saved("Publicación retirada. El enlace ya no permite conversar con el agente.")


def prompts(agent: dict) -> None:
    st.subheader("System Prompt")
    st.caption("Define la identidad, las instrucciones y el tono del agente. Guarda versiones para poder restaurarlas.")
    with st.form(f"prompt_{agent['id']}"):
        body = st.text_area("Instrucciones del sistema", value=agent.get("system_prompt", ""), height=380)
        left, right = st.columns(2)
        save = left.form_submit_button("Guardar", type="primary", width="stretch")
        version = right.form_submit_button("Crear nueva versión", width="stretch")
        if save:
            update(agent, {"system_prompt": body})
        if version:
            api.post(f"/api/agents/{agent['id']}/prompts", json={"body": body})
            saved("Nueva versión del prompt guardada.")
    versions = rows(api.get(f"/api/agents/{agent['id']}/prompts"), "prompts")
    if not versions:
        empty("Sin versiones", "Crea una versión para conservar un punto de restauración.")
        return
    with st.expander("Historial de versiones"):
        options = {item["id"]: item for item in versions}
        selected = st.selectbox("Versión", list(options), format_func=lambda key: f"Versión {options[key].get('version', key)} · {options[key].get('created_at', '')}")
        st.code(options[selected].get("body", ""), language=None, wrap_lines=True)
        if st.button("Restaurar versión"):
            api.post(f"/api/agents/{agent['id']}/prompts/{selected}/restore")
            saved("Versión restaurada.")


def model_options(provider: str, current: str = "", *, key: str = "ui_model", purpose: str = "chat") -> str:
    try:
        response = api.get(f"/api/providers/{provider}/models", params={"purpose": purpose})
        models = response.get("models", []) if isinstance(response, dict) else response
        models = [item if isinstance(item, str) else item.get("id", "") for item in models]
        models = list(dict.fromkeys(value for value in models if value))
    except ApiError as exc:
        st.warning(str(exc))
        models = []
    if current and current not in models:
        models.insert(0, current)
    if models:
        choices = models + ["Introducir ID de modelo…"]
        label = "Modelo de embeddings" if purpose == "embeddings" else "Modelo disponible"
        selected = st.selectbox(label, choices, index=choices.index(current) if current in choices else 0, key=f"{key}_{provider}")
        if selected != choices[-1]:
            return selected
    else:
        st.caption("Configura la API key en Configuración para consultar los modelos disponibles.")
    label = "ID de modelo de embeddings" if purpose == "embeddings" else "ID de modelo"
    return st.text_input(label, value=current, key=f"manual_{key}_{provider}")


def model(agent: dict) -> None:
    st.subheader("Proveedor y modelo")
    providers = ["openai", "gemini"]
    provider = st.selectbox("Proveedor", providers, index=providers.index(agent.get("provider", "openai")), format_func=lambda value: "OpenAI" if value == "openai" else "Google Gemini", key=f"provider_{agent['id']}")
    selected_model = model_options(provider, agent.get("model", "") if provider == agent.get("provider") else "", key=f"model_{agent['id']}")
    st.divider()
    st.markdown("**Embeddings**")
    st.caption("El modelo de embeddings es independiente del modelo de conversación. Al cambiarlo, el backend programa la reindexación del conocimiento.")
    embedding_provider = st.selectbox("Proveedor de embeddings", providers, index=providers.index(agent.get("embedding_provider", "openai")), key=f"embedding_provider_{agent['id']}")
    embedding_model = model_options(
        embedding_provider,
        agent.get("embedding_model", "") if embedding_provider == agent.get("embedding_provider") else "",
        key=f"embedding_model_{agent['id']}",
        purpose="embeddings",
    )
    if st.button("Guardar modelo", type="primary"):
        if not selected_model.strip() or not embedding_model.strip():
            st.error("Indica el modelo de conversación y el modelo de embeddings.")
        else:
            update(agent, {"provider": provider, "model": selected_model.strip(), "embedding_provider": embedding_provider, "embedding_model": embedding_model.strip()})
    with st.expander("Velocidad y límites de respuesta"):
        st.caption("Los tiempos por etapa aparecen en Playground → Ejecución del agente. Las esperas de red y el razonamiento del modelo influyen en la duración.")
        with st.form(f"performance_{agent['id']}"):
            timeout = st.number_input("Espera máxima por llamada al LLM (segundos)", min_value=5, max_value=120, value=agent.get("llm_timeout_seconds", 45))
            retries = st.number_input("Reintentos automáticos por llamada", min_value=0, max_value=2, value=agent.get("llm_max_retries", 0), help="Cada reintento agrega otra espera. El límite total del runtime es 150 segundos.")
            output_limit = st.number_input("Máximo de tokens de salida (0 = predeterminado del modelo)", min_value=0, max_value=32000, value=agent.get("llm_max_output_tokens") or 0, step=256,
                help="Incluye los tokens internos en algunos modelos de razonamiento; un límite demasiado bajo puede impedir una respuesta completa.")
            effort_choices = ["default", "low", "medium", "high"]
            effort = st.selectbox("Razonamiento de OpenAI", effort_choices, index=effort_choices.index(agent.get("llm_reasoning_effort", "default")),
                help="Usa default salvo que tu modelo admita reasoning_effort. Un nivel low puede reducir tiempo en modelos compatibles.")
            exact = st.checkbox("Responder FAQ exactas sin generar otra respuesta con el LLM", value=agent.get("exact_faq_enabled", True),
                help="Entrega la respuesta guardada para una pregunta idéntica, después de revisar el alcance. Desactívalo si siempre necesitas personalización.")
            context_limit = st.number_input("Máximo de caracteres de evidencia", min_value=1000, max_value=120000, value=agent.get("max_context_chars", 32000), step=1000,
                help="Presupuesto conjunto de FAQ, documentos y resultados. Los recortes conservan JSON válido y se indican al modelo; caracteres no equivalen a tokens.")
            tool_limit = st.number_input("Máximo de caracteres del inventario de herramientas", min_value=1000, max_value=120000, value=agent.get("max_tool_context_chars", 16000), step=1000,
                help="Si el inventario supera este límite, reduce las tablas o archivos habilitados antes de consultar.")
            if st.form_submit_button("Guardar rendimiento"):
                if 0 < output_limit < 256:
                    st.error("Usa 0 o al menos 256 tokens.")
                else:
                    update(agent, {"llm_timeout_seconds": timeout, "llm_max_retries": retries, "llm_max_output_tokens": output_limit or None, "llm_reasoning_effort": effort, "exact_faq_enabled": exact,
                        "max_context_chars": context_limit, "max_tool_context_chars": tool_limit})


def scope(agent: dict) -> None:
    st.subheader("Alcance del agente")
    with st.form(f"scope_{agent['id']}"):
        description = st.text_area("Temas y tareas permitidos", value=agent.get("scope_description", ""), height=200, placeholder="Describe las consultas que este agente puede atender y los límites de su conocimiento.")
        policies = ["CLOSE", "WARN", "CONTINUE"]
        labels = {"CLOSE": "Cerrar la conversación", "WARN": "Advertir y mantener abierta", "CONTINUE": "Continuar"}
        current = str(agent.get("out_of_scope_policy", "WARN")).upper()
        policy = st.selectbox("Ante una pregunta fuera de alcance", policies, index=policies.index(current) if current in policies else 1, format_func=labels.get)
        message = st.text_area("Mensaje fuera de alcance", value=agent.get("out_of_scope_message", ""))
        if st.form_submit_button("Guardar alcance", type="primary"):
            update(agent, {"scope_description": description, "out_of_scope_policy": policy, "out_of_scope_message": message})
    st.caption("El runtime distingue IN_SCOPE, UNCERTAIN y OUT_OF_SCOPE antes de recuperar conocimiento o usar herramientas.")


def conversation_settings(agent: dict) -> None:
    st.subheader("Control de conversación")
    with st.form(f"conversation_{agent['id']}"):
        limit = st.number_input("Máximo de interacciones", min_value=1, max_value=1000, value=agent.get("max_interactions", 10), step=1)
        closing = st.text_area("Mensaje de finalización", value=agent.get("closing_message", ""))
        if st.form_submit_button("Guardar", type="primary"):
            update(agent, {"max_interactions": limit, "closing_message": closing})
    st.info("El contador y el estado de cada conversación se conservan en PostgreSQL. Una conversación cerrada requiere iniciar una nueva.")
    with st.expander("Estados y transiciones del runtime"):
        graph = api.get("/api/runtime/graph")
        st.code(graph.get("mermaid", str(graph)), language="mermaid", wrap_lines=True)


def faq_fields(item: dict | None, available_files: list[dict]) -> dict:
    item = item or {}
    question = st.text_input("Pregunta", value=item.get("question", ""))
    answer = st.text_area("Respuesta", value=item.get("answer", ""), height=170)
    left, right = st.columns(2)
    tags = left.text_input("Tags separados por coma", value=", ".join(item.get("tags", [])))
    priority = right.number_input("Prioridad", min_value=0, max_value=100, value=item.get("priority", 0), step=1)
    file_map = {file["id"]: file.get("name", file["id"]) for file in available_files}
    ids = [None] + list(file_map)
    file_id = st.selectbox("Archivo asociado", ids, index=ids.index(item.get("file_id")) if item.get("file_id") in ids else 0, format_func=lambda value: file_map.get(value, "Sin archivo"))
    active = st.checkbox("Activa", value=item.get("active", True))
    return {"question": question, "answer": answer, "tags": tags_from_text(tags), "priority": priority, "file_id": file_id, "active": active}


def faqs(agent: dict) -> None:
    st.subheader("Preguntas frecuentes")
    base = f"/api/agents/{agent['id']}"
    available_files = rows(api.get(f"{base}/files"), "files")
    with st.expander("＋ Nueva pregunta"):
        with st.form(f"new_faq_{agent['id']}", clear_on_submit=True):
            payload = faq_fields(None, available_files)
            if st.form_submit_button("Crear FAQ", type="primary"):
                if not payload["question"].strip() or not payload["answer"].strip():
                    st.error("La pregunta y la respuesta son obligatorias.")
                else:
                    with st.spinner("Guardando FAQ…"):
                        api.post(f"{base}/faqs", json=payload)
                    saved("FAQ guardada. La indexación continúa en el backend.")
    with st.expander("Importar / exportar CSV"):
        st.caption("Columnas: question, answer, tags, priority, file_id, active. Utiliza una exportación como plantilla.")
        uploaded = st.file_uploader("Importar preguntas", type=["csv"], key=f"faq_csv_{agent['id']}")
        if st.button("Importar CSV", disabled=uploaded is None):
            with st.spinner("Importando preguntas…"):
                result = api.upload(f"{base}/faqs/import", uploaded)
            st.success("Importación completada.")
            if result:
                st.json(result)
        exported = api.get(f"{base}/faqs/export", raw=True)
        st.download_button("Exportar CSV", exported, file_name=f"faqs-{agent['id']}.csv", mime="text/csv")
    search = st.text_input("Buscar FAQ", placeholder="Pregunta, respuesta o tags")
    items = rows(api.get(f"{base}/faqs"), "faqs")
    filtered = [item for item in items if search.casefold() in f"{item.get('question', '')} {item.get('answer', '')} {' '.join(item.get('tags', []))}".casefold()]
    if not filtered:
        empty("No hay preguntas que mostrar", "Crea una FAQ, importa un CSV o ajusta la búsqueda.")
        return
    st.dataframe([
        {"Pregunta": item["question"], "Respuesta": item["answer"], "Tags": ", ".join(item.get("tags", [])), "Prioridad": item.get("priority", 0), "Archivo asociado": next((file["name"] for file in available_files if file["id"] == item.get("file_id")), "—"), "Estado": "Activa" if item.get("active", True) else "Inactiva", "Indexación": item.get("status", "—")}
        for item in filtered
    ], width="stretch", hide_index=True)
    selected = st.selectbox("Editar pregunta", filtered, format_func=lambda item: item["question"])
    if selected.get("error"):
        st.error(selected["error"])
    with st.form(f"edit_faq_{selected['id']}"):
        payload = faq_fields(selected, available_files)
        if st.form_submit_button("Guardar FAQ", type="primary"):
            if not payload["question"].strip() or not payload["answer"].strip():
                st.error("La pregunta y la respuesta son obligatorias.")
            else:
                api.patch(f"{base}/faqs/{selected['id']}", payload)
                saved("FAQ actualizada.")
    confirm = st.checkbox("Confirmo que deseo eliminar esta FAQ", key=f"delete_faq_confirm_{selected['id']}")
    if st.button("Eliminar FAQ", disabled=not confirm):
        api.delete(f"{base}/faqs/{selected['id']}")
        saved("FAQ eliminada.")


def documents(agent: dict) -> None:
    st.subheader("Knowledge Base")
    st.caption("Fuentes para recuperación semántica. Los archivos entregables se administran por separado en Archivos.")
    st.caption("Archivo recibido → Procesando → Generando chunks → Generando embeddings → Indexado")
    base = f"/api/agents/{agent['id']}/documents"
    with st.expander("＋ Cargar documento", expanded=True):
        uploaded = st.file_uploader("PDF, DOCX, TXT o Markdown", type=["pdf", "docx", "txt", "md"], key=f"document_{agent['id']}")
        if st.button("Cargar e indexar", type="primary", disabled=uploaded is None):
            # Show only observed server progress; never simulate embedding stages.
            with st.status("Enviando archivo a FastAPI…", expanded=True) as progress:
                result = api.upload(base, uploaded)
                st.write(f"Archivo recibido · {result.get('name', uploaded.name)}")
                status = result.get("status", "received")
                st.write(f"Estado del backend: {status}")
                if status in ("error", "failed", "ERROR", "FAILED"):
                    progress.update(label="La indexación falló", state="error")
                    st.error(result.get("error") or "Revisa el documento y la configuración de embeddings.")
                elif status.lower() == "indexed":
                    st.write(f"{result.get('chunk_count', 0)} chunks indexados")
                    progress.update(label="Documento indexado", state="complete")
                else:
                    progress.update(label=f"Archivo recibido · {status}", state="complete")
                    st.info("La indexación continúa en el backend. Usa Actualizar estado para consultar el progreso.")
    with st.expander("Agregar una URL"):
        with st.form(f"url_{agent['id']}", clear_on_submit=True):
            url = st.text_input("URL pública", placeholder="https://ejemplo.com/documentacion")
            if st.form_submit_button("Importar URL"):
                if not url.startswith("https://"):
                    st.error("Introduce una URL HTTPS válida y autorizada en URL_ALLOWED_HOSTS.")
                else:
                    with st.spinner("Extrayendo e indexando contenido…"):
                        api.post(f"{base}/url", json={"url": url})
                    saved("Fuente URL procesada. Consulta su estado en la lista.")
    with st.expander("Configuración de recuperación"):
        with st.form(f"rag_settings_{agent['id']}"):
            left, right = st.columns(2)
            chunk_size = left.number_input("Tamaño de chunk (caracteres)", min_value=100, max_value=8000, value=agent.get("chunk_size", 1000), step=100)
            overlap = right.number_input("Solapamiento (caracteres)", min_value=0, max_value=2000, value=agent.get("chunk_overlap", 150), step=50)
            retriever_k = left.number_input("Resultados por recuperación", min_value=1, max_value=20, value=agent.get("retriever_k", 4), step=1)
            types = ["similarity", "mmr"]
            retriever_type = right.selectbox("Estrategia", types, index=types.index(agent.get("retriever_type", "similarity")))
            if st.form_submit_button("Guardar recuperación"):
                if overlap >= chunk_size:
                    st.error("El solapamiento debe ser menor que el tamaño de chunk.")
                else:
                    update(agent, {"chunk_size": chunk_size, "chunk_overlap": overlap, "retriever_k": retriever_k, "retriever_type": retriever_type})
    if st.button("Actualizar estado", icon=":material/refresh:"):
        st.rerun()
    items = rows(api.get(base), "documents")
    if not items:
        empty("Tu base de conocimiento está vacía", "Carga el primer documento para que el agente responda con tus fuentes.")
    for item in items:
        with st.container(border=True):
            st.markdown(f"**{item.get('name', 'Documento')}**")
            st.caption(f"Estado: {item.get('status', 'received')} · Chunks: {item.get('chunk_count', 0)}")
            if item.get("error"):
                st.error(item["error"])
            left, right = st.columns(2)
            if left.button("Reindexar", key=f"index_{item['id']}"):
                with st.spinner("Reindexando…"):
                    result = api.post(f"{base}/{item['id']}/index")
                if result.get("status", "").lower() in ("failed", "error"):
                    st.error(result.get("error") or "La indexación falló.")
                else:
                    saved("Indexación procesada.")
            confirmed = right.checkbox("Confirmar eliminación", key=f"confirm_doc_{item['id']}")
            if right.button("Eliminar documento", key=f"delete_doc_{item['id']}", disabled=not confirmed):
                api.delete(f"{base}/{item['id']}")
                saved("Documento eliminado.")


def databases(agent: dict) -> None:
    st.subheader("Bases de datos")
    st.caption("El agente consulta a través de DatabaseQueryTool y SQLGuard. Las conexiones deben usar credenciales de solo lectura.")
    base = f"/api/agents/{agent['id']}/databases"
    with st.expander("＋ Nueva conexión"):
        with st.form(f"database_{agent['id']}", clear_on_submit=True):
            name = st.text_input("Nombre de la conexión")
            dialect = st.selectbox("Motor", ["postgresql", "mysql", "mssql", "sqlite"], format_func=lambda value: {"postgresql": "PostgreSQL", "mysql": "MySQL", "mssql": "SQL Server", "sqlite": "SQLite"}[value])
            dsn = st.text_input("Cadena de conexión (DSN)", type="password", help="Se envía al backend para su almacenamiento cifrado.")
            allowed = st.text_input("Tablas permitidas", placeholder="public.productos, public.categorias")
            max_rows = st.number_input("Máximo de filas", min_value=1, max_value=500, value=100)
            if st.form_submit_button("Guardar conexión", type="primary"):
                if not name.strip() or not dsn.strip() or not tags_from_text(allowed):
                    st.error("Indica el nombre, la conexión y al menos una tabla permitida.")
                else:
                    api.post(base, json={"name": name, "dialect": dialect, "dsn": dsn, "allowed_tables": tags_from_text(allowed), "max_rows": max_rows})
                    saved("Conexión guardada de forma segura.")
    items = rows(api.get(base), "databases")
    if not items:
        empty("Sin conexiones SQL", "Agrega una conexión para permitir consultas de datos estructurados.")
    for item in items:
        with st.expander(f"{item['name']} · {item['dialect']}"):
            st.caption("Solo lectura · Credenciales administradas por el backend")
            with st.form(f"db_edit_{item['id']}", clear_on_submit=True):
                name = st.text_input("Nombre", value=item["name"])
                allowed = st.text_input("Tablas permitidas", value=", ".join(item.get("allowed_tables", [])))
                max_rows = st.number_input("Límite de filas", min_value=1, max_value=500, value=item.get("max_rows", 100))
                replacement = st.text_input("Sustituir DSN (opcional)", type="password")
                if st.form_submit_button("Actualizar conexión"):
                    payload = {"name": name, "dialect": item["dialect"], "allowed_tables": tags_from_text(allowed), "max_rows": max_rows}
                    if replacement:
                        payload["dsn"] = replacement
                    if not name.strip() or not payload["allowed_tables"]:
                        st.error("Indica un nombre y al menos una tabla permitida.")
                    else:
                        api.patch(f"{base}/{item['id']}", payload)
                        saved("Conexión actualizada.")
            if st.button("Probar conexión", key=f"test_db_{item['id']}"):
                result = api.post(f"{base}/{item['id']}/test")
                st.json(result)
            confirmed = st.checkbox("Confirmar eliminación", key=f"confirm_db_{item['id']}")
            if st.button("Eliminar conexión", disabled=not confirmed, key=f"delete_db_{item['id']}"):
                api.delete(f"{base}/{item['id']}")
                saved("Conexión eliminada.")


def files(agent: dict) -> None:
    st.subheader("Archivos para usuarios")
    st.caption("Entregables que el agente puede adjuntar. Estos archivos no se incorporan automáticamente al conocimiento RAG.")
    base = f"/api/agents/{agent['id']}/files"
    uploaded = st.file_uploader("Subir archivo", key=f"deliverable_{agent['id']}")
    if st.button("Guardar archivo", type="primary", disabled=uploaded is None):
        api.upload(base, uploaded)
        saved("Archivo disponible para entrega y asociación a una FAQ.")
    items = rows(api.get(base), "files")
    if not items:
        empty("Aún no hay archivos entregables", "Sube catálogos, fichas, formularios u otros archivos para tus usuarios.")
    for item in items:
        with st.container(border=True):
            st.markdown(f"**{item.get('name', 'Archivo')}**")
            st.caption(f"ID: {item['id']} · Tipo: {item.get('kind', 'deliverable')}")
            download_file(agent["id"], item["id"], item.get("name", "archivo"), f"file_{item['id']}")
            confirmed = st.checkbox("Confirmar eliminación", key=f"confirm_file_{item['id']}")
            if st.button("Eliminar archivo", disabled=not confirmed, key=f"delete_file_{item['id']}"):
                api.delete(f"{base}/{item['id']}")
                saved("Archivo eliminado.")


def memory(agent: dict) -> None:
    st.subheader("Memoria conversacional")
    with st.form(f"memory_{agent['id']}"):
        enabled = st.checkbox("Usar historial en las respuestas", value=agent.get("memory_enabled", True))
        window = st.number_input("Mensajes de contexto", min_value=0, max_value=200, value=agent.get("memory_window", 20))
        history_limit = st.number_input("Máximo de caracteres del historial", min_value=1000, max_value=120000, value=agent.get("max_history_chars", 16000), step=1000,
            help="Envía los mensajes recientes completos que caben. Conserva todo el historial guardado y evita repetir la pregunta actual.")
        if st.form_submit_button("Guardar memoria", type="primary"):
            update(agent, {"memory_enabled": enabled, "memory_window": window, "max_history_chars": history_limit})
    st.caption("La memoria real se almacena en PostgreSQL mediante MemoryService. El estado de la interfaz solo conserva selecciones temporales.")
    items = rows(api.get(f"/api/agents/{agent['id']}/memory"), "conversations")
    st.metric("Conversaciones en memoria", len(items))
    if items:
        st.dataframe([{key: value for key, value in item.items() if key in ("id", "channel", "user_id", "status", "created_at", "interaction_count")} for item in items], hide_index=True, width="stretch")
        with st.expander("Borrar memoria"):
            st.caption("Esta acción elimina los mensajes de memoria del agente. No se puede deshacer.")
            confirmed = st.checkbox("Confirmo que deseo borrar la memoria de este agente", key=f"confirm_memory_{agent['id']}")
            if st.button("Borrar memoria", disabled=not confirmed):
                api.delete(f"/api/agents/{agent['id']}/memory")
                saved("Memoria eliminada.")


def tools(agent: dict) -> None:
    st.subheader("Herramientas")
    st.caption("Habilita las capacidades que el runtime puede seleccionar para este agente.")
    options = ["database", "file", "current_time"]
    current = agent.get("tools_enabled", [])
    options = list(dict.fromkeys(options + current))
    with st.form(f"tools_{agent['id']}"):
        selected = st.multiselect("Herramientas habilitadas", options, default=current, format_func=lambda value: {"database": "Base de datos · SQLGuard + consulta de solo lectura", "file": "Archivos · Entrega de archivos asociados", "current_time": "Fecha y hora actual"}.get(value, value))
        if st.form_submit_button("Guardar herramientas", type="primary"):
            update(agent, {"tools_enabled": selected})


def integration_form(agent: dict, channel: str, item: dict | None = None) -> None:
    item = item or {}
    config = item.get("config") or {}
    base = f"/api/agents/{agent['id']}/integrations"
    st.markdown(f"**{'Telegram' if channel == 'telegram' else 'WhatsApp'}**")
    if channel == "telegram":
        st.info("Telegram admite chats privados en esta versión. Los mensajes de grupos y canales no se procesan.")
    st.caption("Las credenciales se cifran en FastAPI y no se recuperan en esta interfaz. Deja vacíos los secretos para conservar los existentes.")
    with st.form(f"integration_{channel}_{agent['id']}", clear_on_submit=True):
        enabled = st.checkbox("Canal habilitado", value=item.get("enabled", False))
        fields = [("bot_token", "Bot token"), ("webhook_secret", "Secreto del webhook")] if channel == "telegram" else [("access_token", "Access token"), ("app_secret", "App secret"), ("verify_token", "Verify token")]
        secrets = {}
        for key, label in fields:
            value = st.text_input(label, type="password")
            if value:
                secrets[key] = value
        new_config = dict(config)
        new_config["webhook_url"] = st.text_input("URL pública del webhook", value=config.get("webhook_url", ""), placeholder="https://tu-dominio.example/webhooks/…", help="URL HTTPS de FastAPI registrada en la plataforma del canal.")
        if channel == "whatsapp":
            new_config["phone_number_id"] = st.text_input("Phone number ID", value=config.get("phone_number_id", ""))
        if st.form_submit_button("Guardar canal", type="primary"):
            if enabled and not item and len(secrets) != len(fields):
                st.error("Completa los secretos del canal antes de habilitarlo.")
            elif enabled and channel == "whatsapp" and not new_config.get("phone_number_id"):
                st.error("Indica el Phone number ID.")
            else:
                api.post(base, json={"channel": channel, "enabled": enabled, "config": new_config, "secrets": secrets})
                saved("Canal actualizado.")
    if item.get("id"):
        if st.button("Probar conexión", key=f"integration_test_{item['id']}"):
            result = api.post(f"{base}/{item['id']}/test")
            st.json(result)
    st.caption("Webhook gestionado exclusivamente por FastAPI. Consulta la URL pública y los pasos de registro en la documentación del proyecto.")


def channels(agent: dict) -> None:
    st.subheader("Canales")
    items = rows(api.get(f"/api/agents/{agent['id']}/integrations"), "integrations")
    channel = st.radio("Canal", ["telegram", "whatsapp"], format_func=lambda value: value.capitalize(), horizontal=True)
    integration_form(agent, channel, next((item for item in items if item["channel"] == channel), None))
    channel_jobs(agent)


def channel_jobs(agent: dict) -> None:
    st.divider()
    st.subheader("Trabajos pendientes de atención")
    st.caption("Revisa errores de procesamiento y envío antes de reintentar. La consulta y los reintentos se ejecutan en FastAPI.")
    if st.button("Actualizar trabajos", key=f"refresh_jobs_{agent['id']}", icon=":material/refresh:"):
        st.rerun()
    jobs = rows(api.get("/api/jobs", params={"agent_id": agent["id"]}), "jobs")
    attention = [job for job in jobs if job.get("status") in ("FAILED", "NEEDS_REVIEW")]
    if not attention:
        st.caption("No hay trabajos fallidos ni envíos que requieran revisión.")
        return
    for job in attention:
        review_required = job["status"] == "NEEDS_REVIEW"
        label = "Requiere revisión" if review_required else "Fallido"
        with st.expander(f"{label} · {job.get('kind', 'Trabajo')} · {job['id'][:8]}"):
            st.caption(f"Intentos: {job.get('attempts', 0)} · Fecha: {job.get('created_at', '—')}")
            if job.get("error"):
                st.error(job["error"])
            allow_resend = False
            if review_required:
                st.warning("El resultado del envío anterior es incierto. Revisa el canal; reintentar podría duplicar un mensaje.")
                allow_resend = st.checkbox("Revisé el envío y autorizo reenviarlo", key=f"allow_resend_{job['id']}")
            if st.button("Reintentar trabajo", disabled=review_required and not allow_resend, key=f"retry_job_{job['id']}"):
                api.post(f"/api/jobs/{job['id']}/retry", json={"allow_resend": allow_resend})
                saved("Reintento programado. Consulta de nuevo el estado del trabajo.")
