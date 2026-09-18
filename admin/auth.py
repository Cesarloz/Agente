"""Guard every Streamlit page before navigation and bind API access to one browser session."""

from __future__ import annotations

import time

import streamlit as st

from admin.api import ApiError, FrontendAdapter, api


def _clear_session() -> None:
    # Clear loaded data and password/provider form state as well as authentication.
    for key in list(st.session_state):
        del st.session_state[key]


def require_login() -> None:
    if st.session_state.get("superuser_token"):
        if st.session_state.get("superuser_expires_at", 0) <= time.time():
            _clear_session()
            st.warning("Tu sesión venció. Inicia sesión nuevamente.")
        else:
            try:
                api.get("/auth/session")
                return
            except ApiError as exc:
                if exc.status_code in (401, 403):
                    _clear_session()
                    st.warning("Tu sesión venció o fue cerrada. Inicia sesión nuevamente.")
                else:
                    st.error(str(exc))
                    st.stop()

    st.title("Acceso de superusuario")
    st.caption("Inicia sesión para configurar agentes, administrar fuentes y publicar tu asistente.")
    if st.session_state.pop("password_changed", False):
        st.success("Contraseña actualizada. Todas las sesiones se cerraron; ingresa con la nueva contraseña.")
    with st.form("superuser_login", clear_on_submit=True):
        password = st.text_input("Contraseña de superusuario", type="password")
        submitted = st.form_submit_button("Iniciar sesión", type="primary", use_container_width=True)
    if submitted:
        if not password:
            st.error("Escribe la contraseña de superusuario.")
        else:
            try:
                result = FrontendAdapter().post("/auth/login", json={"password": password})
                st.session_state["superuser_token"] = result["token"]
                st.session_state["superuser_expires_at"] = result["expires_at"]
                st.rerun()
            except ApiError as exc:
                st.error(str(exc))
    st.caption("La contraseña inicial se obtiene en el servidor; consulta la guía de instalación de ReflexIA.")
    st.stop()


def account_controls() -> None:
    st.caption("Sesión de superusuario · duración máxima de 8 horas")
    with st.expander("Cambiar contraseña"):
        with st.form("change_superuser_password", clear_on_submit=True):
            current = st.text_input("Contraseña actual", type="password")
            new = st.text_input("Nueva contraseña", type="password", help="Usa al menos 12 caracteres.")
            confirm = st.text_input("Repetir nueva contraseña", type="password")
            submitted = st.form_submit_button("Guardar contraseña")
        if submitted:
            if len(new) < 12:
                st.error("La nueva contraseña debe tener al menos 12 caracteres.")
            elif new != confirm:
                st.error("Las contraseñas nuevas no coinciden.")
            elif not current:
                st.error("Escribe la contraseña actual.")
            else:
                try:
                    api.post("/auth/password", json={"current_password": current, "new_password": new})
                    _clear_session()
                    st.session_state["password_changed"] = True
                    st.rerun()
                except ApiError as exc:
                    st.error(str(exc))
    if st.button("Cerrar sesión", use_container_width=True):
        try:
            api.post("/auth/logout")
            _clear_session()
            st.rerun()
        except ApiError as exc:
            st.error(f"No se pudo cerrar la sesión en el servidor. Vuelve a intentarlo. {exc}")
