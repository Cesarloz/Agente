import hmac
import json

from sqlalchemy import select
from sqlalchemy.exc import IntegrityError

from app.errors import AppError
from app.models import Integration, Job
from app.repositories import Repository
from app.services.credentials import CredentialService


class WebhookService:
    def __init__(self, session):
        self.session = session

    async def integration(self, agent_id, channel):
        row = await self.session.scalar(select(Integration).where(Integration.agent_id == agent_id, Integration.channel == channel, Integration.enabled.is_(True)))
        if not row:
            raise AppError("Canal no disponible", 404)
        return row, await CredentialService(self.session).get_json(row.credential_name)

    async def verify(self, agent_id, mode, token, challenge):
        _, secrets = await self.integration(agent_id, "whatsapp")
        if mode != "subscribe" or not hmac.compare_digest((token or "").encode(), secrets.get("verify_token", "").encode()):
            raise AppError("Verificación no válida", 403)
        return challenge or ""

    async def receive(self, agent_id, channel, raw, signature):
        from app.channels import extract_telegram_messages, extract_whatsapp_messages, verify_telegram, verify_whatsapp
        row, secrets = await self.integration(agent_id, channel)
        valid = verify_telegram(secrets["webhook_secret"], signature) if channel == "telegram" else verify_whatsapp(secrets["app_secret"], raw, signature)
        if not valid:
            raise AppError("Firma de webhook no válida", 403)
        try:
            payload = json.loads(raw)
            if not isinstance(payload, dict):
                raise ValueError()
        except (ValueError, UnicodeError):
            raise AppError("Payload no válido") from None
        messages = extract_telegram_messages(payload) if channel == "telegram" else extract_whatsapp_messages(payload)
        # v1 supports private Telegram chats; group identities require a separate chat key.
        if channel == "telegram" and payload.get("message", {}).get("chat", {}).get("type") != "private":
            return {"ok": True, "accepted": 0}
        accepted = 0
        for message in messages:
            if channel == "whatsapp" and message.get("phone_number_id") != row.config.get("phone_number_id"):
                continue
            if len(message["text"]) > 20000:
                continue
            key = f"{row.id}:{message['external_message_id']}"
            if await self.session.scalar(select(Job.id).where(Job.dedup_key == key)):
                continue
            try:
                async with self.session.begin_nested():
                    await Repository(self.session, Job).add(kind="channel", payload={"agent_id": agent_id, "integration_id": row.id, **message}, dedup_key=key)
                accepted += 1
            except IntegrityError:
                pass
        await self.session.commit()
        return {"ok": True, "accepted": accepted}
