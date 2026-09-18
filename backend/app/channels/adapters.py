"""Real HTTP adapters. Credentials and upstream bodies never enter public errors."""

import logging
import re
from contextlib import asynccontextmanager
from pathlib import Path
from typing import Any

import httpx


class ChannelError(RuntimeError):
    """Sanitized transport error safe for the API."""


class _TelegramURLRedactor(logging.Filter):
    """HTTPX normally logs the complete request URL, including Telegram tokens."""

    def filter(self, record: logging.LogRecord) -> bool:
        record.msg = re.sub(r"(api\.telegram\.org/(?:file/)?bot)[^/\s]+", r"\1[REDACTED]", record.getMessage())
        record.args = ()
        return True


_telegram_redactor = _TelegramURLRedactor()
logging.getLogger("httpx").addFilter(_telegram_redactor)


class _HTTPChannel:
    def __init__(self, client: httpx.AsyncClient | None = None):
        self._http_client = client

    @asynccontextmanager
    async def _client(self):
        if self._http_client is not None:
            yield self._http_client
        else:
            async with httpx.AsyncClient(timeout=httpx.Timeout(30.0, connect=10.0), follow_redirects=False) as client:
                yield client

    @staticmethod
    async def _request(client: httpx.AsyncClient, method: str, url: str, **kwargs: Any) -> dict:
        try:
            response = await client.request(method, url, **kwargs)
            response.raise_for_status()
            data = response.json()
            if not isinstance(data, dict) or data.get("ok") is False or data.get("error"):
                raise ChannelError("El proveedor rechazó la operación del canal.")
            return data
        except ChannelError:
            raise
        except (httpx.HTTPError, ValueError, TypeError):
            raise ChannelError("No fue posible completar la operación del canal; revisa conexión y credenciales.") from None

    @staticmethod
    def _file(path: str | Path, filename: str, limit: int) -> tuple[Path, str]:
        try:
            file_path = Path(path).resolve(strict=True)
            safe_name = Path(filename.replace("\\", "/")).name
            if not file_path.is_file() or file_path.stat().st_size > limit or not safe_name:
                raise ValueError
            return file_path, safe_name
        except (OSError, ValueError):
            raise ChannelError("El archivo no está disponible o supera el límite del canal.") from None

    @staticmethod
    def _chunks(text: str, limit: int = 4096) -> list[str]:
        if not isinstance(text, str) or not text.strip():
            raise ChannelError("El mensaje no puede estar vacío.")
        # Telegram counts UTF-16 code units for message limits/entities.
        chunks, current, units = [], [], 0
        for char in text:
            size = 2 if ord(char) > 0xFFFF else 1
            if units + size > limit:
                chunks.append("".join(current))
                current, units = [], 0
            current.append(char)
            units += size
        if current:
            chunks.append("".join(current))
        return chunks


class TelegramChannel(_HTTPChannel):
    def __init__(self, token: str, client: httpx.AsyncClient | None = None):
        super().__init__(client)
        if not re.fullmatch(r"[0-9]+:[A-Za-z0-9_-]+", token or ""):
            raise ChannelError("El token de Telegram no tiene un formato válido.")
        self._base_url = f"https://api.telegram.org/bot{token}"

    async def send_text(self, recipient: str, text: str) -> dict[str, Any]:
        ids = []
        async with self._client() as client:
            for chunk in self._chunks(text):
                data = await self._request(client, "POST", self._base_url + "/sendMessage", json={"chat_id": recipient, "text": chunk})
                result = data.get("result", {})
                if not isinstance(result, dict) or result.get("message_id") is None:
                    raise ChannelError("El canal no confirmó la entrega del mensaje.")
                ids.append(str(result["message_id"]))
        return {"ok": True, "channel": "telegram", "message_ids": ids, "external_message_id": ids[-1]}

    async def send_file(self, recipient: str, path: str | Path, filename: str, mime_type: str) -> dict[str, Any]:
        file_path, safe_name = self._file(path, filename, 50 * 1024 * 1024)
        try:
            async with self._client() as client:
                with file_path.open("rb") as handle:
                    data = await self._request(client, "POST", self._base_url + "/sendDocument", data={"chat_id": recipient},
                                               files={"document": (safe_name, handle, mime_type)})
            result = data.get("result", {})
            if not isinstance(result, dict) or result.get("message_id") is None:
                raise ChannelError("El canal no confirmó la entrega del archivo.")
            return {"ok": True, "channel": "telegram", "external_message_id": str(result["message_id"])}
        except OSError:
            raise ChannelError("No se pudo leer el archivo para enviarlo.") from None

    async def test(self) -> dict[str, Any]:
        try:
            async with self._client() as client:
                data = await self._request(client, "GET", self._base_url + "/getMe")
            result = data.get("result", {})
            if not isinstance(result, dict) or not result.get("is_bot"):
                raise ChannelError("La respuesta de validación de Telegram no es válida.")
            return {"ok": True, "channel": "telegram", "username": str(result.get("username", ""))}
        except ChannelError as error:
            return {"ok": False, "channel": "telegram", "error": str(error)}


class WhatsAppChannel(_HTTPChannel):
    DOCUMENT_MIMES = {"text/plain", "application/pdf", "application/msword", "application/vnd.ms-excel", "application/vnd.ms-powerpoint",
                      "application/vnd.openxmlformats-officedocument.wordprocessingml.document", "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet",
                      "application/vnd.openxmlformats-officedocument.presentationml.presentation"}
    MEDIA_MIMES = {"image/jpeg": ("image", 5), "image/png": ("image", 5), "audio/aac": ("audio", 16), "audio/mp4": ("audio", 16),
                   "audio/mpeg": ("audio", 16), "audio/amr": ("audio", 16), "audio/ogg": ("audio", 16), "video/mp4": ("video", 16), "video/3gpp": ("video", 16)}

    def __init__(self, access_token: str, phone_number_id: str, api_version: str, client: httpx.AsyncClient | None = None):
        super().__init__(client)
        if not access_token or not re.fullmatch(r"[0-9]+", str(phone_number_id)) or not re.fullmatch(r"v[0-9]+\.0", api_version or ""):
            raise ChannelError("Configura token, identificador de teléfono y versión de API de WhatsApp.")
        self._base_url = f"https://graph.facebook.com/{api_version}/{phone_number_id}"
        self._headers = {"Authorization": f"Bearer {access_token}"}

    async def _send(self, client: httpx.AsyncClient, recipient: str, kind: str, payload: dict) -> str:
        data = await self._request(client, "POST", self._base_url + "/messages", headers=self._headers,
                                   json={"messaging_product": "whatsapp", "recipient_type": "individual", "to": recipient, "type": kind, kind: payload})
        messages = data.get("messages", [])
        if not isinstance(messages, list) or not messages or not isinstance(messages[0], dict) or not messages[0].get("id"):
            raise ChannelError("El canal no confirmó la entrega del mensaje.")
        return str(messages[0]["id"])

    async def send_text(self, recipient: str, text: str) -> dict[str, Any]:
        ids = []
        async with self._client() as client:
            for chunk in self._chunks(text):
                ids.append(await self._send(client, recipient, "text", {"preview_url": False, "body": chunk}))
        return {"ok": True, "channel": "whatsapp", "message_ids": ids, "external_message_id": ids[-1]}

    async def send_file(self, recipient: str, path: str | Path, filename: str, mime_type: str) -> dict[str, Any]:
        if mime_type in self.DOCUMENT_MIMES:
            kind, size_mb = "document", 100
        elif mime_type in self.MEDIA_MIMES:
            kind, size_mb = self.MEDIA_MIMES[mime_type]
        else:
            raise ChannelError("WhatsApp no admite este tipo de archivo.")
        file_path, safe_name = self._file(path, filename, size_mb * 1024 * 1024)
        try:
            async with self._client() as client:
                with file_path.open("rb") as handle:
                    data = await self._request(client, "POST", self._base_url + "/media", headers=self._headers,
                                               data={"messaging_product": "whatsapp", "type": mime_type}, files={"file": (safe_name, handle, mime_type)})
                media_id = data.get("id")
                if not media_id:
                    raise ChannelError("El canal no confirmó la carga del archivo.")
                payload = {"id": str(media_id)}
                if kind == "document":
                    payload["filename"] = safe_name
                message_id = await self._send(client, recipient, kind, payload)
            return {"ok": True, "channel": "whatsapp", "external_message_id": message_id, "media_id": str(media_id)}
        except OSError:
            raise ChannelError("No se pudo leer el archivo para enviarlo.") from None

    async def test(self) -> dict[str, Any]:
        try:
            async with self._client() as client:
                data = await self._request(client, "GET", self._base_url, headers=self._headers, params={"fields": "id,display_phone_number,verified_name"})
            if not data.get("id"):
                raise ChannelError("La respuesta de validación de WhatsApp no es válida.")
            return {"ok": True, "channel": "whatsapp", "display_phone_number": str(data.get("display_phone_number", "")), "verified_name": str(data.get("verified_name", ""))}
        except ChannelError as error:
            return {"ok": False, "channel": "whatsapp", "error": str(error)}
