"""Brand assets and publication are controlled independently of runtime configuration."""
import asyncio
from io import BytesIO
from pathlib import Path
from urllib.parse import urlsplit
from uuid import uuid4
import warnings

from PIL import Image, ImageOps, UnidentifiedImageError

from app.config import get_settings
from app.errors import AppError
from app.models import Agent, Document, FAQ
from app.repositories import Repository
from app.services.agents import agent_view
from app.services.credentials import CredentialService

LOGO_MAX_BYTES = 2 * 1024 * 1024


def normalize_logo(content: bytes) -> bytes:
    if len(content) > LOGO_MAX_BYTES:
        raise AppError("El logo debe pesar como máximo 2 MB", 413)
    try:
        with warnings.catch_warnings():
            warnings.simplefilter("error", Image.DecompressionBombWarning)
            with Image.open(BytesIO(content)) as source:
                if source.format not in {"PNG", "JPEG", "WEBP"}:
                    raise AppError("Sube una imagen PNG, JPG o WebP válida", 422)
                if source.width * source.height > 16_000_000:
                    raise AppError("El logo debe tener como máximo 16 millones de píxeles", 422)
                source.load()
                picture = ImageOps.exif_transpose(source).convert("RGBA")
                picture.thumbnail((1024, 1024))
                output = BytesIO()
                # Re-encode pixels only: discard metadata and any appended active content.
                clean = Image.new("RGBA", picture.size)
                clean.paste(picture)
                clean.save(output, format="PNG")
                return output.getvalue()
    except (UnidentifiedImageError, OSError, ValueError, Image.DecompressionBombError, Image.DecompressionBombWarning):
        raise AppError("No se pudo leer la imagen. Usa un PNG, JPG o WebP válido", 422) from None


class PublicationService:
    def __init__(self, session):
        self.session = session
        self.repo = Repository(session, Agent)

    @staticmethod
    def logo_path(filename: str) -> Path:
        root = (get_settings().storage_dir / "logos").resolve()
        path = (root / filename).resolve()
        if path.parent != root or path.suffix != ".png":
            raise AppError("Logo no disponible", 404)
        return path

    async def logo(self, agent_id):
        agent = await self.repo.get(agent_id)
        if not agent.logo_filename:
            raise AppError("Este agente no tiene un logo personalizado", 404)
        path = self.logo_path(agent.logo_filename)
        if not path.is_file():
            raise AppError("Logo no disponible", 404)
        return path

    async def upload_logo(self, agent_id, content):
        agent = await self.repo.get(agent_id, lock=True)
        normalized = await asyncio.to_thread(normalize_logo, content)
        filename = f"{uuid4().hex}.png"
        path = self.logo_path(filename)
        path.parent.mkdir(parents=True, exist_ok=True)
        old = agent.logo_filename
        try:
            await asyncio.to_thread(path.write_bytes, normalized)
            agent.logo_filename = filename
            await self.session.commit()
        except Exception:
            path.unlink(missing_ok=True)
            raise
        if old:
            self.logo_path(old).unlink(missing_ok=True)
        return agent_view(agent)

    async def delete_logo(self, agent_id):
        agent = await self.repo.get(agent_id, lock=True)
        old, agent.logo_filename = agent.logo_filename, None
        await self.session.commit()
        if old:
            self.logo_path(old).unlink(missing_ok=True)
        return agent_view(agent)

    async def publish(self, agent_id):
        agent = await self.repo.get(agent_id, lock=True)
        if not agent.config.get("active"):
            raise AppError("Activa el agente antes de publicar su página", 409)
        if not agent.config.get("model", "").strip():
            raise AppError("Configura el modelo de conversación antes de publicar", 409)
        credentials = CredentialService(self.session)
        await credentials.get(f"provider:{agent.config['provider']}")
        has_knowledge = any([
            bool(await Repository(self.session, model).list(model.agent_id == agent_id, limit=1))
            for model in (Document, FAQ)
        ])
        if has_knowledge:
            if not agent.config.get("embedding_model", "").strip():
                raise AppError("Configura el modelo de embeddings para usar el conocimiento", 409)
            await credentials.get(f"provider:{agent.config['embedding_provider']}")
        url = urlsplit(get_settings().public_base_url)
        if url.scheme not in {"http", "https"} or not url.netloc or url.username or url.password or url.query or url.fragment or url.path not in {"", "/"}:
            raise AppError("PUBLIC_BASE_URL debe ser la dirección HTTP(S) de la API, sin rutas ni credenciales", 409)
        agent.published = True
        await self.session.commit()
        return agent_view(agent)

    async def unpublish(self, agent_id):
        agent = await self.repo.get(agent_id, lock=True)
        agent.published = False
        await self.session.commit()
        return agent_view(agent)

    async def public_agent(self, agent_id):
        agent = await self.repo.get(agent_id)
        if not agent.published or not agent.config.get("active"):
            raise AppError("Esta página no está disponible", 404)
        return agent
