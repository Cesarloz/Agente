import json

from cryptography.fernet import Fernet, InvalidToken
from sqlalchemy import select

from app.config import get_settings
from app.errors import AppError
from app.models import Credential, Provider
from app.repositories import Repository


class CredentialService:
    def __init__(self, session):
        self.session = session

    def cipher(self):
        try:
            return Fernet(get_settings().encryption_key.encode())
        except (ValueError, TypeError):
            raise AppError("Configura ENCRYPTION_KEY en el servidor", 503) from None

    async def set(self, name: str, value: str):
        if not value.strip():
            raise AppError("La credencial no puede estar vacía")
        row = await self.session.scalar(select(Credential).where(Credential.name == name))
        encrypted = self.cipher().encrypt(value.encode()).decode()
        if row:
            row.ciphertext = encrypted
        else:
            await Repository(self.session, Credential).add(name=name, ciphertext=encrypted)

    async def get(self, name: str) -> str:
        row = await self.session.scalar(select(Credential).where(Credential.name == name))
        if not row:
            raise AppError("Falta configurar la credencial requerida", 409)
        try:
            return self.cipher().decrypt(row.ciphertext.encode()).decode()
        except InvalidToken:
            raise AppError("No se pudo descifrar la credencial con la clave del servidor", 503) from None

    async def status(self):
        names = set(await self.session.scalars(select(Credential.name)))
        return [{"provider": p, "configured": f"provider:{p}" in names} for p in ("openai", "gemini")]

    async def set_provider(self, provider, value):
        if provider not in {"openai", "gemini"}:
            raise AppError("Proveedor no válido")
        await self.set(f"provider:{provider}", value)
        if not await self.session.scalar(select(Provider).where(Provider.name == provider)):
            await Repository(self.session, Provider).add(name=provider)
        await self.session.commit()
        return {"provider": provider, "configured": True}

    async def models(self, provider, *, purpose="chat"):
        from app.providers import list_models
        key = await self.get(f"provider:{provider}")
        try:
            return {"models": await list_models(provider, key, purpose=purpose)}
        except Exception:
            raise AppError("El proveedor no pudo listar modelos. Revisa la clave y la conectividad.", 502) from None

    async def get_json(self, name):
        return json.loads(await self.get(name))
