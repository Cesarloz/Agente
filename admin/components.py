from __future__ import annotations

from functools import wraps
from typing import Any, Callable

import streamlit as st

from admin.api import ApiError, api


def page(title: str, subtitle: str = "") -> Callable:
    """Render an API error at the page boundary without hiding failures."""
    def decorator(function: Callable) -> Callable:
        @wraps(function)
        def wrapped() -> None:
            st.title(title)
            if subtitle:
                st.caption(subtitle)
            flash = st.session_state.pop("ui_flash", None)
            if flash:
                st.success(flash)
            try:
                function()
            except ApiError as exc:
                st.error(str(exc))
                if st.button("Volver a intentar", key=f"retry_{function.__name__}"):
                    st.rerun()
        return wrapped
    return decorator


def saved(message: str = "Cambios guardados.") -> None:
    st.session_state["ui_flash"] = message
    st.rerun()


def rows(data: Any, key: str = "items") -> list[dict]:
    if isinstance(data, list):
        return data
    if isinstance(data, dict):
        return data.get(key, data.get("items", []))
    return []


def agents() -> list[dict]:
    return rows(api.get("/api/agents"), "agents")


def agent_selector(*, optional: bool = False, key: str = "ui_agent") -> dict | None:
    available = agents()
    if not available:
        st.info("Crea tu primer agente en Agentes para comenzar.")
        return None
    choices = {item["id"]: item for item in available}
    ids = ([None] if optional else []) + list(choices)
    selected = st.selectbox(
        "Agente", ids, key=key,
        format_func=lambda value: "Todos los agentes" if value is None else choices[value]["name"],
    )
    return choices.get(selected)


def filter_params(agent: dict | None) -> dict:
    return {"agent_id": agent["id"]} if agent else {}


def tags_from_text(value: str) -> list[str]:
    return list(dict.fromkeys(item.strip() for item in value.split(",") if item.strip()))


def empty(label: str, description: str) -> None:
    with st.container(border=True):
        st.markdown(f"**{label}**")
        st.caption(description)


def health_badge() -> None:
    try:
        data = api.get("/health")
        status = data.get("status", "ok") if isinstance(data, dict) else "ok"
        if status in ("ok", "healthy"):
            st.success("API conectada", icon=":material/check_circle:")
        else:
            st.warning(f"API: {status}")
    except ApiError:
        st.warning("API sin conexión", icon=":material/cloud_off:")


def download_file(agent_id: str, file_id: str, name: str, key: str) -> None:
    if st.button("Preparar descarga", key=f"prepare_{key}"):
        content = api.get(f"/api/agents/{agent_id}/files/{file_id}/download", raw=True)
        st.download_button("Descargar archivo", content, file_name=name, key=f"download_{key}")
