"""Replaceable HTTP frontend adapter; no application services are imported here."""

from __future__ import annotations

from collections.abc import Callable
from typing import Any

import httpx
from pydantic_settings import BaseSettings, SettingsConfigDict


class ApiError(Exception):
    """Safe, user-facing API failure."""

    def __init__(self, message: str, status_code: int | None = None):
        super().__init__(message)
        self.status_code = status_code


class FrontendSettings(BaseSettings):
    """Read local configuration without adding secrets to the process environment."""

    model_config = SettingsConfigDict(env_file=".env", env_file_encoding="utf-8", extra="ignore")
    api_base_url: str = "http://localhost:8000"


class FrontendAdapter:
    def __init__(self, base_url: str | None = None, token: str | None = None,
                 token_provider: Callable[[], str] | None = None):
        settings = FrontendSettings()
        self.base_url = (base_url or settings.api_base_url).rstrip("/")
        self._token = token or ""
        self._token_provider = token_provider if token is None else None

    @property
    def token(self) -> str:
        # The module-level adapter is shared; credentials must come from the current UI session.
        return self._token_provider() if self._token_provider else self._token

    def request(self, method: str, path: str, *, raw: bool = False, **kwargs: Any) -> Any:
        token = self.token
        headers = {"Authorization": f"Bearer {token}"} if token else {}
        try:
            with httpx.Client(timeout=httpx.Timeout(180.0, connect=10.0)) as client:
                response = client.request(method, f"{self.base_url}{path}", headers=headers, **kwargs)
        except httpx.TimeoutException as exc:
            raise ApiError("La API tardó demasiado en responder. Comprueba el estado antes de repetir la operación.") from exc
        except httpx.HTTPError as exc:
            raise ApiError("No fue posible conectar con la API. Comprueba que FastAPI esté iniciado y API_BASE_URL sea correcto.") from exc
        if response.is_error:
            # Never surface a request body: it may contain a provider key or DSN.
            if response.status_code in (401, 403):
                raise ApiError("Acceso denegado. Comprueba tu contraseña o inicia sesión nuevamente.", response.status_code)
            if response.status_code == 422:
                raise ApiError("La API rechazó los datos. Revisa los campos obligatorios y sus valores.", 422)
            try:
                detail = response.json().get("detail", "")
            except (ValueError, AttributeError):
                detail = ""
            if not isinstance(detail, str):
                detail = ""
            # Redact credential values if a third-party exception is echoed by the API.
            secrets = []
            body = kwargs.get("json") or {}
            if isinstance(body, dict):
                secrets.extend(str(body[key]) for key in ("api_key", "dsn", "password", "app_password", "current_password", "new_password") if body.get(key))
                secrets.extend(str(value) for value in (body.get("secrets") or {}).values() if value)
            for secret in secrets:
                detail = detail.replace(secret, "[protegido]")
            raise ApiError(detail[:500] or f"La API devolvió el error {response.status_code}. Revisa los registros del backend.", response.status_code)
        if raw:
            return response.content
        if response.status_code == 204 or not response.content:
            return None
        try:
            return response.json()
        except ValueError as exc:
            raise ApiError("La API devolvió una respuesta con un formato inesperado.") from exc

    def get(self, path: str, **kwargs: Any) -> Any:
        return self.request("GET", path, **kwargs)

    def post(self, path: str, **kwargs: Any) -> Any:
        return self.request("POST", path, **kwargs)

    def patch(self, path: str, payload: dict[str, Any]) -> Any:
        return self.request("PATCH", path, json=payload)

    def put(self, path: str, payload: dict[str, Any]) -> Any:
        return self.request("PUT", path, json=payload)

    def delete(self, path: str) -> Any:
        return self.request("DELETE", path)

    def upload(self, path: str, file: Any) -> Any:
        return self.post(path, files={"file": (file.name, file.getvalue(), file.type or "application/octet-stream")})


def _current_session_token() -> str:
    import streamlit as st

    return st.session_state.get("superuser_token", "")


api = FrontendAdapter(token_provider=_current_session_token)
