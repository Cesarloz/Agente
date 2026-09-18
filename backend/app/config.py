from functools import lru_cache
from pathlib import Path

from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    model_config = SettingsConfigDict(env_file=".env", extra="ignore")
    database_url: str = "postgresql+psycopg://agente:agente@localhost:5432/agente"
    admin_token: str = ""
    encryption_key: str = ""
    admin_token_file: str = ""
    encryption_key_file: str = ""
    superuser_password_hash: str = ""
    superuser_password_hash_file: str = ""
    auth_session_seconds: int = 8 * 60 * 60
    auth_max_attempts: int = 5
    auth_lockout_seconds: int = 15 * 60
    storage_dir: Path = Path("storage")
    chroma_host: str = "localhost"
    chroma_port: int = 8001
    vector_provider: str = "chroma"
    upload_max_bytes: int = 25 * 1024 * 1024
    public_base_url: str = "http://localhost:8000"
    url_allowed_hosts: str = ""
    worker_poll_seconds: float = 2.0
    worker_max_attempts: int = 3
    graph_api_version: str = "v23.0"


@lru_cache
def get_settings() -> Settings:
    settings = Settings()
    if settings.admin_token_file:
        settings.admin_token = Path(settings.admin_token_file).read_text().strip()
    if settings.encryption_key_file:
        settings.encryption_key = Path(settings.encryption_key_file).read_text().strip()
    if settings.superuser_password_hash_file:
        settings.superuser_password_hash = Path(settings.superuser_password_hash_file).read_text().strip()
    return settings
