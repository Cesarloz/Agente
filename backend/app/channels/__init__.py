"""Messaging adapters; webhooks are received and persisted by FastAPI."""

from .adapters import ChannelError, TelegramChannel, WhatsAppChannel
from .webhooks import extract_telegram_messages, extract_whatsapp_messages, verify_telegram, verify_whatsapp

__all__ = ["ChannelError", "TelegramChannel", "WhatsAppChannel", "verify_telegram", "verify_whatsapp", "extract_telegram_messages", "extract_whatsapp_messages"]
