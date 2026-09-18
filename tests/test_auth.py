"""Authentication regressions: bootstrap, credential separation, session lifecycle and UI gating."""

import sqlite3
import time
from pathlib import Path
from unittest.mock import patch

import pytest
from streamlit.testing.v1 import AppTest

from admin.api import ApiError, FrontendAdapter, api
from app import auth
from app.config import get_settings


PASSWORD = "test-superuser-password-1234"
NEW_PASSWORD = "new-test-superuser-password-5678"


@pytest.fixture
def auth_storage(database, monkeypatch):
    settings = get_settings()
    monkeypatch.setattr(settings, "superuser_password_hash", auth.hash_password(PASSWORD))
    auth.initialize_auth(settings)
    return settings


async def login(client, password=PASSWORD):
    return await client.post("/auth/login", json={"password": password}, headers={"Authorization": ""})


async def test_login_password_is_independent_and_grants_protected_api_access(client, auth_storage):
    wrong = await login(client, auth_storage.admin_token)
    assert wrong.status_code == 401
    assert auth_storage.admin_token not in wrong.text
    response = await login(client)
    assert response.status_code == 200
    result = response.json()
    assert result["role"] == "superuser"
    assert result["expires_at"] > time.time()
    assert PASSWORD not in response.text
    headers = {"Authorization": f"Bearer {result['token']}"}
    assert (await client.get("/auth/session", headers=headers)).status_code == 200
    assert (await client.get("/api/agents", headers=headers)).status_code == 200
    assert (await client.get("/api/agents", headers={"Authorization": ""})).status_code == 401
    assert (await client.get("/api/agents", headers={"Authorization": "Bearer invalid-token"})).status_code == 401
    # Machine automation retains its independent preexisting credential.
    assert (await client.get("/api/agents")).status_code == 200
    assert (await client.get("/auth/session")).status_code == 401


async def test_logout_revokes_only_presented_session(client, auth_storage):
    first = (await login(client)).json()["token"]
    second = (await login(client)).json()["token"]
    headers = {"Authorization": f"Bearer {first}"}
    assert (await client.post("/auth/logout", headers=headers)).status_code == 204
    assert (await client.get("/api/agents", headers=headers)).status_code == 401
    assert (await client.post("/auth/logout", headers=headers)).status_code == 204
    assert (await client.get("/auth/session", headers={"Authorization": f"Bearer {second}"})).status_code == 200


async def test_session_expiry_and_hashed_storage(client, auth_storage, monkeypatch):
    result = (await login(client)).json()
    path = auth_storage.storage_dir / ".auth" / "auth.sqlite3"
    connection = sqlite3.connect(path)
    try:
        encoded = connection.execute("SELECT password_hash FROM superuser").fetchone()[0]
        stored_token = connection.execute("SELECT token_hash FROM sessions").fetchone()[0]
    finally:
        connection.close()
    assert encoded != PASSWORD and auth.verify_password(PASSWORD, encoded)
    assert stored_token != result["token"]
    monkeypatch.setattr(auth.time, "time", lambda: result["expires_at"] + 1)
    headers = {"Authorization": f"Bearer {result['token']}"}
    assert (await client.get("/auth/session", headers=headers)).status_code == 401
    assert (await client.get("/api/agents", headers=headers)).status_code == 401


async def test_attempt_limit_persists_and_recovers_after_lockout(client, auth_storage, monkeypatch):
    now = time.time()
    monkeypatch.setattr(auth.time, "time", lambda: now)
    for attempt in range(auth_storage.auth_max_attempts):
        response = await login(client, "wrong-test-password")
        assert response.status_code == (429 if attempt == auth_storage.auth_max_attempts - 1 else 401)
    assert (await login(client)).status_code == 429
    # A restart/bootstrap must preserve blocked attempts.
    auth.initialize_auth(auth_storage)
    assert (await login(client)).status_code == 429
    monkeypatch.setattr(auth.time, "time", lambda: now + auth_storage.auth_lockout_seconds + 1)
    assert (await login(client)).status_code == 200


async def test_change_password_revokes_all_sessions_and_survives_bootstrap(client, auth_storage):
    first = (await login(client)).json()["token"]
    second = (await login(client)).json()["token"]
    headers = {"Authorization": f"Bearer {first}"}
    invalid = await client.post("/auth/password", json={"current_password": "incorrect", "new_password": NEW_PASSWORD}, headers=headers)
    assert invalid.status_code == 401
    response = await client.post("/auth/password", json={"current_password": PASSWORD, "new_password": NEW_PASSWORD}, headers=headers)
    assert response.status_code == 204
    for token in (first, second):
        assert (await client.get("/api/agents", headers={"Authorization": f"Bearer {token}"})).status_code == 401
    auth.initialize_auth(auth_storage)
    assert (await login(client)).status_code == 401
    assert (await login(client, NEW_PASSWORD)).status_code == 200
    assert (await client.post("/auth/password", json={"current_password": NEW_PASSWORD, "new_password": PASSWORD})).status_code == 401


def test_native_bootstrap_is_private_and_does_not_rotate_existing_password(database, monkeypatch, capsys):
    settings = get_settings()
    monkeypatch.setattr(settings, "superuser_password_hash", "")
    auth.initialize_auth(settings)
    initial = settings.storage_dir / ".auth" / "initial_password"
    password = initial.read_text(encoding="utf-8")
    assert len(password) >= 24
    auth.initialize_auth(settings)
    assert initial.read_text(encoding="utf-8") == password
    assert password not in capsys.readouterr().out


def test_frontend_token_callback_is_resolved_per_request_and_never_uses_server_secret(monkeypatch):
    monkeypatch.setenv("ADMIN_TOKEN", "server-test-token-must-never-be-used")
    monkeypatch.setenv("ADMIN_TOKEN_FILE", "nonexistent-test-secret-file")
    assert FrontendAdapter().token == ""
    state = {"token": "first-browser-session"}
    adapter = FrontendAdapter(token_provider=lambda: state["token"])
    assert adapter.token == "first-browser-session"
    state["token"] = "second-browser-session"
    assert adapter.token == "second-browser-session"
    state["token"] = ""
    assert adapter.token == ""


def test_unauthenticated_app_never_reads_admin_data():
    source = Path(__file__).resolve().parents[1] / "admin" / "app.py"
    with patch.object(api, "get") as read:
        app = AppTest.from_file(str(source), default_timeout=20).run()
    assert not app.exception
    assert app.title[0].value == "Acceso de superusuario"
    assert not app.sidebar.button
    read.assert_not_called()


def test_revoked_ui_session_is_cleared_before_loading_admin_data():
    source = Path(__file__).resolve().parents[1] / "admin" / "app.py"
    app = AppTest.from_file(str(source), default_timeout=20)
    app.session_state["superuser_token"] = "revoked-test-session"
    app.session_state["superuser_expires_at"] = time.time() + 3600
    app.session_state["loaded_private_record"] = {"name": "private"}
    with patch.object(api, "get", side_effect=ApiError("Sesión cerrada", 401)) as read:
        app.run()
    assert not app.exception
    read.assert_called_once_with("/auth/session")
    assert "superuser_token" not in app.session_state
    assert "loaded_private_record" not in app.session_state
    assert app.title[0].value == "Acceso de superusuario"


def test_ui_login_errors_then_authenticates_and_logout_revokes_session():
    source = """
import streamlit as st
from admin.auth import require_login, account_controls
require_login()
st.title("Panel privado")
account_controls()
"""
    app = AppTest.from_string(source, default_timeout=20).run()
    expires_at = time.time() + 3600
    with patch.object(FrontendAdapter, "post", side_effect=ApiError("Contraseña incorrecta", 401)) as signin:
        app.text_input[0].input("wrong-ui-password")
        next(button for button in app.button if button.label == "Iniciar sesión").click().run()
    assert not app.exception
    assert app.error
    signin.assert_called_once_with("/auth/login", json={"password": "wrong-ui-password"})
    assert "superuser_token" not in app.session_state
    with patch.object(FrontendAdapter, "post", return_value={"token": "ui-test-session", "expires_at": expires_at}), patch.object(api, "get", return_value={"role": "superuser", "expires_at": expires_at}):
        app.text_input[0].input(PASSWORD)
        next(button for button in app.button if button.label == "Iniciar sesión").click().run()
    assert not app.exception
    assert app.title[0].value == "Panel privado"
    assert app.session_state["superuser_token"] == "ui-test-session"
    with patch.object(api, "post", return_value=None) as signout, patch.object(api, "get", return_value={"role": "superuser", "expires_at": expires_at}):
        next(button for button in app.button if button.label == "Cerrar sesión").click().run()
    assert not app.exception
    signout.assert_called_once_with("/auth/logout")
    assert "superuser_token" not in app.session_state
    assert app.title[0].value == "Acceso de superusuario"


def test_ui_password_change_clears_sensitive_state_and_returns_to_login():
    source = """
import streamlit as st
from admin.auth import require_login, account_controls
require_login()
account_controls()
"""
    expires_at = time.time() + 3600
    app = AppTest.from_string(source, default_timeout=20)
    app.session_state["superuser_token"] = "ui-test-session"
    app.session_state["superuser_expires_at"] = expires_at
    app.session_state["loaded_private_record"] = "sensitive-test-data"
    with patch.object(api, "get", return_value={"role": "superuser", "expires_at": expires_at}), patch.object(api, "post", return_value=None) as change:
        app.run()
        next(widget for widget in app.text_input if widget.label == "Contraseña actual").input(PASSWORD)
        next(widget for widget in app.text_input if widget.label == "Nueva contraseña").input(NEW_PASSWORD)
        next(widget for widget in app.text_input if widget.label == "Repetir nueva contraseña").input(NEW_PASSWORD)
        next(button for button in app.button if button.label == "Guardar contraseña").click().run()
    assert not app.exception
    change.assert_called_once_with("/auth/password", json={"current_password": PASSWORD, "new_password": NEW_PASSWORD})
    assert "superuser_token" not in app.session_state
    assert "loaded_private_record" not in app.session_state
    assert app.title[0].value == "Acceso de superusuario"
    assert app.success
