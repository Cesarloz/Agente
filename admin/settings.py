from __future__ import annotations

import streamlit as st

from admin.agents import integration_form
from admin.api import api
from admin.components import agent_selector, page, rows, saved


@page("Configuración", "Administra las conexiones de modelos y los canales de mensajería.")
def settings_page() -> None:
    selected = st.radio("Servicio", ["OpenAI", "Google Gemini", "Gmail", "WhatsApp", "Telegram"], horizontal=True, key="ui_settings_section")
    if selected in ("OpenAI", "Google Gemini"):
        provider_settings("openai" if selected == "OpenAI" else "gemini", selected)
    elif selected == "Gmail":
        gmail_settings()
    else:
        st.subheader(selected)
        st.caption("Las integraciones se asignan por agente para mantener separado el enrutamiento y las credenciales.")
        agent = agent_selector(key="ui_settings_channel_agent")
        if agent:
            channel = selected.lower()
            integrations = rows(api.get(f"/api/agents/{agent['id']}/integrations"), "integrations")
            item = next((item for item in integrations if item["channel"] == channel), None)
            integration_form(agent, channel, item)


def provider_settings(provider: str, label: str) -> None:
    st.subheader(label)
    credentials = rows(api.get("/api/credentials"), "credentials")
    configured = any(item.get("provider") == provider and item.get("configured") for item in credentials)
    if configured:
        st.success("API key configurada en el servidor", icon=":material/key:")
    else:
        st.info("Aún no hay una API key configurada para este proveedor.")
    st.caption("La API key se envía a CredentialService, se cifra en el servidor y no se devuelve a esta interfaz. El formulario se limpia al enviarlo.")
    with st.form(f"credentials_{provider}", clear_on_submit=True):
        api_key = st.text_input("API Key", type="password", placeholder="Introduce una nueva API key" if configured else "Introduce tu API key")
        if st.form_submit_button("Guardar API key", type="primary"):
            if not api_key.strip():
                st.error("Introduce una API key.")
            else:
                api.put(f"/api/credentials/{provider}", {"api_key": api_key.strip()})
                saved(f"Credencial de {label} guardada y cifrada.")
    left, right = st.columns(2)
    if left.button("Probar conexión", disabled=not configured, width="stretch"):
        with st.spinner(f"Probando conexión con {label}…"):
            result = api.post(f"/api/credentials/{provider}/test")
        st.json(result)
    if right.button("Consultar modelos disponibles", disabled=not configured, width="stretch"):
        with st.spinner("Consultando catálogo del proveedor…"):
            result = api.get(f"/api/providers/{provider}/models")
        models = result.get("models", []) if isinstance(result, dict) else result
        if models:
            st.dataframe({"Modelo": [item if isinstance(item, str) else item.get("id", "") for item in models]}, hide_index=True, width="stretch")
        else:
            st.info("El proveedor no devolvió modelos disponibles para esta credencial.")
    st.divider()
    st.markdown("**Siguiente paso**")
    st.caption("Abre Agentes → Modelo para elegir este proveedor y asignar un modelo de conversación y embeddings a cada agente.")


def gmail_settings() -> None:
    st.subheader("Correo saliente con Gmail")
    account = api.get("/api/email/gmail")
    if account.get("configured"):
        st.success(f"Cuenta configurada: {account['sender']}")
    st.caption("Conecta una cuenta Gmail o Google Workspace habilitada para contraseñas de aplicación. Se guarda cifrada en el servidor.")
    st.markdown("Activa la verificación en dos pasos y crea una [contraseña de aplicación de Google](https://myaccount.google.com/apppasswords). Usa esa clave de 16 caracteres en este formulario.")
    st.caption("Si tu organización bloquea las contraseñas de aplicación, esta conexión SMTP no estará disponible para esa cuenta.")
    with st.form("gmail_configuration", clear_on_submit=True):
        sender = st.text_input("Dirección Gmail", value=account.get("sender", ""), placeholder="tu-cuenta@gmail.com")
        name = st.text_input("Nombre del remitente", value=account.get("sender_name") or "ReflexIA")
        password = st.text_input("Contraseña de aplicación", type="password", help="No uses la contraseña habitual de tu cuenta.")
        if st.form_submit_button("Guardar Gmail", type="primary"):
            if not sender.strip() or not password.strip():
                st.error("Escribe el correo y la contraseña de aplicación.")
            else:
                api.put("/api/email/gmail", {"sender": sender.strip(), "sender_name": name, "app_password": password})
                saved("Cuenta Gmail guardada y cifrada.")
    left, right = st.columns(2)
    if left.button("Probar conexión Gmail", disabled=not account.get("configured")):
        with st.spinner("Verificando acceso SMTP…"):
            result = api.post("/api/email/gmail/test")
        st.success(result["message"])
    if right.button("Desconectar Gmail", disabled=not account.get("configured")):
        api.delete("/api/email/gmail")
        saved("Cuenta desconectada. Los correos pendientes no podrán enviarse con esta credencial.")
    st.info("Para enviar una respuesta, abre Conversaciones → Enviar respuesta por correo. Puedes revisar el contenido, agregar información y elegir el destinatario antes de enviarlo.")
