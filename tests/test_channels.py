import hashlib
import hmac
import json
import logging

import httpx
import pytest

from app.channels import (ChannelError, TelegramChannel, WhatsAppChannel, extract_telegram_messages,
                          extract_whatsapp_messages, verify_telegram, verify_whatsapp)


def test_telegram_secret_fails_closed():
    assert verify_telegram("known-secret", "known-secret")
    assert not verify_telegram("known-secret", "wrong-secret")
    assert not verify_telegram("", "")
    assert not verify_telegram("known-secret", None)


def test_whatsapp_authenticates_exact_original_bytes():
    body = b'{"entry": []}'
    signature = "sha256=" + hmac.new(b"app-secret", body, hashlib.sha256).hexdigest()
    assert verify_whatsapp("app-secret", body, signature)
    assert not verify_whatsapp("app-secret", b'{"entry":[]}', signature)
    assert not verify_whatsapp("", body, signature)
    assert not verify_whatsapp("app-secret", body, "sha1=bad")
    assert not verify_whatsapp("app-secret", body, None)


def test_telegram_normalizes_user_and_recipient_separately():
    payload = {"update_id": 42, "message": {"message_id": 7, "from": {"id": 10, "is_bot": False}, "chat": {"id": -20}, "text": " Hola "}}
    assert extract_telegram_messages(payload) == [{"external_message_id": "42", "user_id": "10", "recipient": "-20", "text": "Hola", "channel": "telegram"}]
    assert extract_telegram_messages({"edited_message": payload["message"]}) == []
    payload["message"]["from"]["is_bot"] = True
    assert extract_telegram_messages(payload) == []


def test_whatsapp_parses_batched_text_and_ignores_statuses():
    value = {"metadata": {"phone_number_id": "100"}, "messages": [
        {"id": "wamid.1", "from": "52111", "type": "text", "text": {"body": "Hola"}},
        {"id": "wamid.2", "from": "52111", "type": "image", "image": {"id": "media"}},
        {"id": "wamid.3", "from": "52112", "type": "text", "text": {"body": "Otra"}}]}
    payload = {"object": "whatsapp_business_account", "entry": [{"changes": [{"field": "messages", "value": value}, {"field": "messages", "value": {"statuses": [{"id": "delivery"}]}}]}]}
    messages = extract_whatsapp_messages(payload)
    assert [message["external_message_id"] for message in messages] == ["wamid.1", "wamid.3"]
    assert all(message["phone_number_id"] == "100" for message in messages)


async def test_telegram_sends_actual_uploaded_bytes(tmp_path):
    path = tmp_path / "manual.pdf"
    path.write_bytes(b"%PDF-test-content")
    requests = []
    def handler(request):
        requests.append(request)
        return httpx.Response(200, json={"ok": True, "result": {"message_id": 99}})
    async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as client:
        result = await TelegramChannel("123:token", client=client).send_file("10", path, "../manual.pdf", "application/pdf")
    assert result["external_message_id"] == "99"
    assert requests[0].url.path.endswith("/sendDocument")
    assert b"%PDF-test-content" in requests[0].content
    assert b'filename="manual.pdf"' in requests[0].content
    assert b"../manual.pdf" not in requests[0].content


async def test_whatsapp_upload_precedes_send_and_uses_media_id(tmp_path):
    path = tmp_path / "manual.pdf"
    path.write_bytes(b"%PDF-real-file")
    requests = []
    def handler(request):
        requests.append(request)
        if request.url.path.endswith("/media"):
            return httpx.Response(200, json={"id": "media-123"})
        return httpx.Response(200, json={"messages": [{"id": "wamid.sent"}]})
    async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as client:
        result = await WhatsAppChannel("token-secret", "100", "v25.0", client=client).send_file("52111", path, "manual.pdf", "application/pdf")
    assert result["external_message_id"] == "wamid.sent"
    assert len(requests) == 2
    assert b"%PDF-real-file" in requests[0].content
    assert requests[0].headers["Authorization"] == "Bearer token-secret"
    assert json.loads(requests[1].content)["document"] == {"id": "media-123", "filename": "manual.pdf"}


async def test_channel_failure_does_not_leak_url_token_or_upstream_body():
    def handler(request):
        return httpx.Response(401, json={"error": "token-secret exposed"})
    async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as client:
        channel = TelegramChannel("123:token-secret", client=client)
        with pytest.raises(ChannelError) as error:
            await channel.send_text("10", "Hola")
        assert "token-secret" not in str(error.value)
        assert "api.telegram" not in str(error.value)
        assert not (await channel.test())["ok"]


async def test_long_unicode_messages_are_split_without_losing_text():
    texts = []
    def handler(request):
        texts.append(json.loads(request.content)["text"])
        return httpx.Response(200, json={"ok": True, "result": {"message_id": len(texts)}})
    original = "😀" * 3000
    async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as client:
        await TelegramChannel("123:token", client=client).send_text("10", original)
    assert len(texts) == 2
    assert "".join(texts) == original
    assert all(len(text.encode("utf-16-le")) // 2 <= 4096 for text in texts)


async def test_httpx_logs_redact_telegram_tokens(caplog):
    def handler(request):
        return httpx.Response(200, json={"ok": True, "result": {"message_id": 1}})
    with caplog.at_level(logging.INFO, logger="httpx"):
        async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as client:
            await TelegramChannel("123:super-secret", client=client).send_text("10", "Hola")
    assert "super-secret" not in caplog.text
    assert "[REDACTED]" in caplog.text
