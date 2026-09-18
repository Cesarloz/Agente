"""Stateless verification and parsing; authenticate the original bytes first."""

import hashlib
import hmac
import re
from typing import Any


def verify_telegram(secret: str, header: str | None) -> bool:
    if not secret or not header:
        return False
    return hmac.compare_digest(secret.encode("utf-8"), header.encode("utf-8"))


def verify_whatsapp(app_secret: str, raw_body: bytes, signature: str | None) -> bool:
    if not app_secret or not signature or not re.fullmatch(r"sha256=[0-9a-fA-F]{64}", signature):
        return False
    expected = hmac.new(app_secret.encode("utf-8"), raw_body, hashlib.sha256).hexdigest()
    return hmac.compare_digest(expected, signature[7:].lower())


def extract_telegram_messages(payload: dict[str, Any]) -> list[dict[str, str]]:
    """Only new user text messages. Delivery/edit/service events are ignored."""
    message = payload.get("message")
    if not isinstance(message, dict) or not isinstance(message.get("text"), str):
        return []
    sender, chat = message.get("from", {}), message.get("chat", {})
    if not isinstance(sender, dict) or not isinstance(chat, dict) or sender.get("is_bot"):
        return []
    if sender.get("id") is None or chat.get("id") is None or message.get("message_id") is None:
        return []
    text = message["text"].strip()
    if not text:
        return []
    return [{"external_message_id": str(payload.get("update_id", f'{chat["id"]}:{message["message_id"]}')),
             "user_id": str(sender["id"]), "recipient": str(chat["id"]), "text": text, "channel": "telegram"}]


def extract_whatsapp_messages(payload: dict[str, Any]) -> list[dict[str, str]]:
    """Handle all batched entries, changes, and text messages; ignore statuses."""
    if payload.get("object") != "whatsapp_business_account":
        return []
    messages = []
    entries = payload.get("entry", [])
    if not isinstance(entries, list):
        return []
    for entry in entries:
        if not isinstance(entry, dict) or not isinstance(entry.get("changes"), list):
            continue
        for change in entry["changes"]:
            if not isinstance(change, dict) or change.get("field") != "messages":
                continue
            value = change.get("value", {})
            if not isinstance(value, dict) or not isinstance(value.get("messages"), list):
                continue
            metadata = value.get("metadata", {})
            phone_number_id = str(metadata.get("phone_number_id", "")) if isinstance(metadata, dict) else ""
            for message in value["messages"]:
                if not isinstance(message, dict) or message.get("type") != "text" or not message.get("id") or not message.get("from"):
                    continue
                text_obj = message.get("text", {})
                text = text_obj.get("body") if isinstance(text_obj, dict) else None
                if not isinstance(text, str) or not text.strip():
                    continue
                messages.append({"external_message_id": str(message["id"]), "user_id": str(message["from"]),
                                 "recipient": str(message["from"]), "text": text.strip(), "channel": "whatsapp",
                                 "phone_number_id": phone_number_id})
    return messages
