"""Password login and revocable, expiring sessions, separate from the automation API token."""

from __future__ import annotations

import hashlib
import hmac
import os
from contextlib import closing, contextmanager
from pathlib import Path
import secrets
import sqlite3
import time
from typing import Annotated

from fastapi import APIRouter, Depends, Header
from pydantic import BaseModel, Field, SecretStr

from app.config import Settings, get_settings
from app.errors import AppError


router = APIRouter(prefix="/auth", tags=["superusuario"])
PASSWORD_MIN_LENGTH = 12


def hash_password(password: str) -> str:
    salt = secrets.token_bytes(16)
    digest = hashlib.scrypt(password.encode("utf-8"), salt=salt, n=16384, r=8, p=1, dklen=32)
    return f"scrypt$16384$8$1${salt.hex()}${digest.hex()}"


def verify_password(password: str, encoded: str) -> bool:
    try:
        algorithm, n, r, p, salt, expected = encoded.split("$")
        # Restrict work factors to our format; malformed configuration cannot trigger unbounded work.
        if (algorithm, n, r, p) != ("scrypt", "16384", "8", "1"):
            return False
        if len(salt) != 32 or len(expected) != 64:
            return False
        digest = hashlib.scrypt(password.encode("utf-8"), salt=bytes.fromhex(salt), n=16384, r=8, p=1, dklen=32)
        return hmac.compare_digest(digest, bytes.fromhex(expected))
    except (ValueError, TypeError):
        return False


def _path(settings: Settings) -> Path:
    return settings.storage_dir / ".auth" / "auth.sqlite3"


def initialize_auth(settings: Settings | None = None) -> None:
    """Create private auth storage and bootstrap exactly once; never print credentials."""
    settings = settings or get_settings()
    if settings.auth_session_seconds < 60 or settings.auth_max_attempts < 1 or settings.auth_lockout_seconds < 1:
        raise RuntimeError("La duración de sesión y los límites de acceso deben ser positivos")
    path = _path(settings)
    path.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
    path.parent.chmod(0o700)
    with closing(sqlite3.connect(path, timeout=10)) as connection, connection:
        path.chmod(0o600)
        connection.executescript("""
            CREATE TABLE IF NOT EXISTS superuser (id INTEGER PRIMARY KEY CHECK (id = 1), password_hash TEXT NOT NULL);
            CREATE TABLE IF NOT EXISTS sessions (token_hash TEXT PRIMARY KEY, expires_at REAL NOT NULL);
            CREATE TABLE IF NOT EXISTS attempts (id INTEGER PRIMARY KEY CHECK (id = 1), failures INTEGER NOT NULL,
                window_start REAL NOT NULL, blocked_until REAL NOT NULL);
        """)
        connection.execute("BEGIN IMMEDIATE")
        if connection.execute("SELECT 1 FROM superuser WHERE id = 1").fetchone():
            return
        encoded = settings.superuser_password_hash
        if not encoded:
            # Native development fallback. Docker supplies a hash from its persistent secrets volume.
            initial = path.parent / "initial_password"
            if initial.exists():
                password = initial.read_text(encoding="utf-8").strip()
            else:
                password = secrets.token_urlsafe(24)
                descriptor = os.open(initial, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
                with os.fdopen(descriptor, "w", encoding="utf-8") as stream:
                    stream.write(password)
                initial.chmod(0o600)
            encoded = hash_password(password)
        else:
            # Verification checks format without requiring the original password.
            parts = encoded.split("$")
            if len(parts) != 6 or parts[:4] != ["scrypt", "16384", "8", "1"]:
                raise RuntimeError("SUPERUSER_PASSWORD_HASH no tiene un formato válido")
            try:
                if len(bytes.fromhex(parts[4])) != 16 or len(bytes.fromhex(parts[5])) != 32:
                    raise ValueError
            except ValueError as exc:
                raise RuntimeError("SUPERUSER_PASSWORD_HASH no tiene un formato válido") from exc
        connection.execute("INSERT INTO superuser (id, password_hash) VALUES (1, ?)", (encoded,))


@contextmanager
def _connection():
    settings = get_settings()
    if not _path(settings).exists():
        initialize_auth(settings)
    with closing(sqlite3.connect(_path(settings), timeout=10)) as connection, connection:
        yield connection


def _bearer(authorization: str | None) -> str:
    return authorization[7:] if authorization and authorization.startswith("Bearer ") else ""


def _token_hash(token: str) -> str:
    return hashlib.sha256(token.encode("utf-8")).hexdigest()


def _session(token: str) -> dict:
    if not token or len(token) > 256:
        raise AppError("Inicia sesión con la contraseña de superusuario", 401)
    with _connection() as connection:
        row = connection.execute("SELECT expires_at FROM sessions WHERE token_hash = ?", (_token_hash(token),)).fetchone()
    if not row or row[0] <= time.time():
        raise AppError("La sesión venció o fue cerrada. Inicia sesión nuevamente", 401)
    return {"role": "superuser", "expires_at": row[0], "token_hash": _token_hash(token)}


def require_session(authorization: Annotated[str | None, Header()] = None) -> dict:
    return _session(_bearer(authorization))


def require_admin(authorization: Annotated[str | None, Header()] = None) -> dict:
    supplied = _bearer(authorization)
    expected = get_settings().admin_token
    if supplied and expected and secrets.compare_digest(supplied.encode("utf-8"), expected.encode("utf-8")):
        return {"role": "admin", "auth_type": "api_token"}
    return _session(supplied)


def _check_attempts(connection: sqlite3.Connection, now: float) -> None:
    row = connection.execute("SELECT blocked_until FROM attempts WHERE id = 1").fetchone()
    if row and row[0] > now:
        raise AppError("Demasiados intentos. Espera unos minutos antes de volver a intentar", 429)


def _failed_attempt(connection: sqlite3.Connection, now: float) -> None:
    settings = get_settings()
    row = connection.execute("SELECT failures, window_start FROM attempts WHERE id = 1").fetchone()
    failures, start = row if row and row[1] + settings.auth_lockout_seconds > now else (0, now)
    failures += 1
    blocked = now + settings.auth_lockout_seconds if failures >= settings.auth_max_attempts else 0
    connection.execute("INSERT OR REPLACE INTO attempts VALUES (1, ?, ?, ?)", (failures, start, blocked))
    # Commit before raising: failed authentication must retain the rate-limit record.
    connection.commit()
    if blocked:
        raise AppError("Demasiados intentos. Espera unos minutos antes de volver a intentar", 429)
    raise AppError("Contraseña incorrecta", 401)


class LoginInput(BaseModel):
    password: SecretStr = Field(min_length=1, max_length=1024)


class PasswordInput(BaseModel):
    current_password: SecretStr = Field(min_length=1, max_length=1024)
    new_password: SecretStr = Field(min_length=PASSWORD_MIN_LENGTH, max_length=1024)


@router.post("/login")
def login(data: LoginInput):
    now = time.time()
    with _connection() as connection:
        connection.execute("BEGIN IMMEDIATE")
        _check_attempts(connection, now)
        encoded = connection.execute("SELECT password_hash FROM superuser WHERE id = 1").fetchone()[0]
        if not verify_password(data.password.get_secret_value(), encoded):
            _failed_attempt(connection, now)
        token = secrets.token_urlsafe(48)
        expires_at = now + get_settings().auth_session_seconds
        connection.execute("DELETE FROM sessions WHERE expires_at <= ?", (now,))
        connection.execute("INSERT INTO sessions VALUES (?, ?)", (_token_hash(token), expires_at))
        connection.execute("DELETE FROM attempts")
    return {"token": token, "expires_at": expires_at, "role": "superuser"}


@router.get("/session")
def session_status(session: Annotated[dict, Depends(require_session)]):
    return {"role": session["role"], "expires_at": session["expires_at"]}


@router.post("/logout", status_code=204)
def logout(authorization: Annotated[str | None, Header()] = None):
    # Idempotent even when expired; only the caller's presented session is revoked.
    token = _bearer(authorization)
    if token and len(token) <= 256:
        with _connection() as connection:
            connection.execute("DELETE FROM sessions WHERE token_hash = ?", (_token_hash(token),))


@router.post("/password", status_code=204)
def change_password(data: PasswordInput, session: Annotated[dict, Depends(require_session)]):
    now = time.time()
    with _connection() as connection:
        connection.execute("BEGIN IMMEDIATE")
        # Recheck inside the write lock in case another password change revoked this session.
        active = connection.execute("SELECT expires_at FROM sessions WHERE token_hash = ?", (session["token_hash"],)).fetchone()
        if not active or active[0] <= now:
            raise AppError("La sesión venció o fue cerrada. Inicia sesión nuevamente", 401)
        _check_attempts(connection, now)
        encoded = connection.execute("SELECT password_hash FROM superuser WHERE id = 1").fetchone()[0]
        if not verify_password(data.current_password.get_secret_value(), encoded):
            _failed_attempt(connection, now)
        connection.execute("UPDATE superuser SET password_hash = ? WHERE id = 1", (hash_password(data.new_password.get_secret_value()),))
        connection.execute("DELETE FROM sessions")
        connection.execute("DELETE FROM attempts")
