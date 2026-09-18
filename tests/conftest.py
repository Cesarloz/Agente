import os

from cryptography.fernet import Fernet
import httpx
import pytest
from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine

os.environ.setdefault("DATABASE_URL", "sqlite+aiosqlite://")
os.environ.setdefault("ADMIN_TOKEN", "test-admin-token-with-at-least-32-characters")
os.environ.setdefault("ENCRYPTION_KEY", Fernet.generate_key().decode())

from app.config import get_settings  # noqa: E402
from app.db import get_session  # noqa: E402
from app.main import app  # noqa: E402
from app.models import Base  # noqa: E402


@pytest.fixture
async def database(tmp_path, monkeypatch):
    engine = create_async_engine("sqlite+aiosqlite://")
    async with engine.begin() as connection:
        await connection.run_sync(Base.metadata.create_all)
    factory = async_sessionmaker(engine, expire_on_commit=False)
    monkeypatch.setattr(get_settings(), "storage_dir", tmp_path / "storage")
    yield factory
    await engine.dispose()


@pytest.fixture
async def session(database):
    async with database() as session:
        yield session


@pytest.fixture
async def client(database):
    async def dependency():
        async with database() as session:
            yield session
    app.dependency_overrides[get_session] = dependency
    async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="http://test", headers={"Authorization": f"Bearer {get_settings().admin_token}"}) as client:
        yield client
    app.dependency_overrides.clear()
