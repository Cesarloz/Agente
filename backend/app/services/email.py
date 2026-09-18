"""Administrator-triggered Gmail delivery. SMTP secrets never enter jobs or API responses."""
import asyncio
from email.headerregistry import Address
from email.errors import HeaderParseError
from email.message import EmailMessage
from email.utils import formataddr
import json
import smtplib
import ssl
from uuid import UUID

from pydantic import Field, SecretStr, field_validator
from sqlalchemy import select
from sqlalchemy.exc import IntegrityError

from app.errors import AppError
from app.models import Conversation, Credential, Job
from app.repositories import Repository
from app.schemas import Input
from app.services.credentials import CredentialService

CREDENTIAL_NAME = "email:gmail"


def checked_email(value):
    if not value or not value.isascii() or any(char.isspace() for char in value):
        raise ValueError("Usa una dirección de correo válida")
    try:
        address = Address(addr_spec=value)
    except (ValueError, IndexError, HeaderParseError):
        raise ValueError("Usa una dirección de correo válida") from None
    if not address.username or not address.domain or "." not in address.domain or address.addr_spec != value:
        raise ValueError("Usa una dirección de correo válida")
    return value


class GmailInput(Input):
    sender: str = Field(min_length=3, max_length=254)
    sender_name: str = Field(default="ReflexIA", max_length=100, pattern=r"^[^\r\n]*$")
    app_password: SecretStr = Field(min_length=1, max_length=128)

    _sender_email = field_validator("sender")(checked_email)


class EmailInput(Input):
    recipient: str = Field(min_length=3, max_length=254)
    subject: str = Field(min_length=1, max_length=200, pattern=r"^[^\r\n]+$")
    body: str = Field(min_length=1, max_length=100000)
    request_id: UUID

    _recipient_email = field_validator("recipient")(checked_email)


def smtp_connection(credentials):
    connection = smtplib.SMTP_SSL("smtp.gmail.com", 465, timeout=20, context=ssl.create_default_context())
    try:
        connection.login(credentials["sender"], credentials["app_password"])
    except Exception:
        connection.close()
        raise
    return connection


def test_gmail(credentials):
    try:
        connection = smtp_connection(credentials)
        connection.close()
    except (smtplib.SMTPException, OSError):
        raise AppError("No se pudo conectar con Gmail. Revisa la cuenta, contraseña de aplicación y conexión SMTP", 502) from None


def send_gmail(credentials, payload):
    message = EmailMessage()
    message["From"] = formataddr((credentials.get("sender_name", "ReflexIA"), credentials["sender"]))
    message["To"] = payload["recipient"]
    message["Subject"] = payload["subject"]
    message["Message-ID"] = f"<reflexia-{payload['request_id']}@{credentials['sender'].split('@')[1]}>"
    message.set_content(payload["body"])
    try:
        connection = smtp_connection(credentials)
        try:
            connection.send_message(message)
        finally:
            # QUIT failure after a successful DATA must not turn a confirmed send into an unknown one.
            connection.close()
    except (smtplib.SMTPAuthenticationError, smtplib.SMTPRecipientsRefused, smtplib.SMTPSenderRefused) as error:
        raise AppError("Gmail rechazó la cuenta o la dirección de correo. Revisa la configuración antes de reintentar", 422) from error


class EmailService:
    def __init__(self, session):
        self.session = session
        self.credentials = CredentialService(session)

    async def status(self):
        row = await self.session.scalar(select(Credential).where(Credential.name == CREDENTIAL_NAME))
        if row is None:
            return {"configured": False, "sender": "", "sender_name": ""}
        data = await self.credentials.get_json(CREDENTIAL_NAME)
        return {"configured": True, "sender": data["sender"], "sender_name": data.get("sender_name", "")}

    async def configure(self, data: GmailInput):
        password = data.app_password.get_secret_value().replace(" ", "")
        if len(password) != 16 or not password.isascii() or not password.isalnum():
            raise AppError("Introduce la contraseña de aplicación de 16 caracteres generada por Google", 422)
        await self.credentials.set(CREDENTIAL_NAME, json.dumps({"sender": data.sender, "sender_name": data.sender_name, "app_password": password}))
        await self.session.commit()
        return await self.status()

    async def disconnect(self):
        row = await self.session.scalar(select(Credential).where(Credential.name == CREDENTIAL_NAME))
        if row:
            await self.session.delete(row)
            await self.session.commit()
        return {"configured": False}

    async def test(self):
        credentials = await self.credentials.get_json(CREDENTIAL_NAME)
        await asyncio.to_thread(test_gmail, credentials)
        return {"ok": True, "sender": credentials["sender"], "message": "Conexión autenticada. No se envió ningún correo."}

    async def enqueue(self, conversation_id, data: EmailInput):
        conversation = await Repository(self.session, Conversation).get(conversation_id)
        account = await self.status()
        if not account["configured"]:
            raise AppError("Configura Gmail antes de enviar una respuesta", 409)
        payload = {**data.model_dump(mode="json"), "agent_id": conversation.agent_id,
                   "conversation_id": conversation.id, "sender": account["sender"]}
        key = f"email:{data.request_id}"
        existing = await self.session.scalar(select(Job).where(Job.dedup_key == key))
        if existing:
            if any(existing.payload.get(field) != value for field, value in payload.items()):
                raise AppError("La solicitud ya existe con otro contenido. Prepara un nuevo correo", 409)
            return {"job_id": existing.id, "status": existing.status}
        try:
            job = await Repository(self.session, Job).add(kind="email", payload=payload, dedup_key=key)
            await self.session.commit()
        except IntegrityError:
            await self.session.rollback()
            # Concurrent double-clicks resolve to the same durable request.
            return await self.enqueue(conversation_id, data)
        return {"job_id": job.id, "status": job.status}

    async def deliver(self, job):
        credentials = await self.credentials.get_json(CREDENTIAL_NAME)
        if credentials["sender"] != job.payload["sender"]:
            raise AppError("La cuenta Gmail cambió después de preparar este correo. Revisa el envío", 409)
        job.payload = {**job.payload, "send_in_progress": True}
        await self.session.commit()
        try:
            await asyncio.to_thread(send_gmail, credentials, job.payload)
        except AppError as error:
            if error.status == 422:
                job.payload = {**job.payload, "send_in_progress": False}
                await self.session.commit()
            raise
        job.payload = {**job.payload, "send_in_progress": False, "email_sent": True}
        await self.session.commit()
