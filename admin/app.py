"""Run with: streamlit run admin/app.py"""

from __future__ import annotations

import sys
from base64 import b64encode
from pathlib import Path

# Keep `streamlit run admin/app.py` working from the repository and Docker.
sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

import streamlit as st

from admin.agents import agent_page
from admin.auth import account_controls, require_login
from admin.components import health_badge
from admin.overview import analytics_page, conversations_page, dashboard_page, playground_page, questions_page
from admin.settings import settings_page

st.set_page_config(page_title="ReflexIA · Administración", page_icon="◈", layout="wide", initial_sidebar_state="expanded")
require_login()
st.markdown(
    """<style>
    .block-container {max-width:1320px;padding-top:2.1rem;padding-bottom:3rem}
    h1 {font-size:2rem!important;letter-spacing:-.035em;font-weight:700!important}
    h2 {font-size:1.35rem!important;letter-spacing:-.015em}
    h3 {font-size:1.08rem!important}
    [data-testid="stSidebar"] {border-right:1px solid #dce6ed}
    [data-testid="stMetric"] {background:#fff;border:1px solid #dce6ed;border-radius:12px;padding:1rem 1.1rem}
    [data-testid="stMetricValue"] {font-size:1.8rem;letter-spacing:-.04em;color:#102d42}
    [data-testid="stForm"] {border-color:#dce6ed;border-radius:12px}
    .brand {background:#10121a;border-radius:12px;padding:1.1rem .8rem;margin-bottom:1.2rem}
    .brand img {display:block;width:100%;height:auto}
    .brand-note {font-size:.65rem;color:#c1cbd7;letter-spacing:.08em;text-align:center;margin-top:.85rem}
    </style>""",
    unsafe_allow_html=True,
)

with st.sidebar:
    logo_data = b64encode((Path(__file__).parent / "assets" / "reflexia-logo.png").read_bytes()).decode("ascii")
    st.markdown(
        f'<div class="brand"><img src="data:image/png;base64,{logo_data}" alt="ReflexIA">'
        '<div class="brand-note">CENTRO DE ADMINISTRACIÓN</div></div>',
        unsafe_allow_html=True,
    )

navigation = st.navigation(
    {
        "Espacio de trabajo": [
            st.Page(dashboard_page, title="Dashboard", icon=":material/space_dashboard:", default=True),
            st.Page(agent_page, title="Agentes", icon=":material/smart_toy:"),
            st.Page(playground_page, title="Playground", icon=":material/forum:"),
        ],
        "Actividad": [
            st.Page(conversations_page, title="Conversaciones", icon=":material/chat_bubble:"),
            st.Page(questions_page, title="Preguntas de usuarios", icon=":material/help:"),
            st.Page(analytics_page, title="Analítica", icon=":material/monitoring:"),
        ],
        "Administración": [
            st.Page(settings_page, title="Configuración", icon=":material/settings:"),
        ],
    }
)
with st.sidebar:
    st.divider()
    account_controls()
    st.divider()
    health_badge()
    st.caption("Memoria y configuración guardadas en el backend. Esta interfaz consume exclusivamente su API REST.")
navigation.run()
