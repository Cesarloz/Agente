import smtplib
from unittest.mock import MagicMock
from uuid import uuid4

import pytest
from sqlalchemy import select

from app import worker
from app.models import Conversation, Credential, Job
from app.repositories import Repository
from app.services import email


async def setup(client, database):
    agent = (await client.post("/api/agents", json={"name": "Correo"})).json()
    async with database() as session:
        row = await Repository(session, Conversation).add(agent_id=agent["id"], user_id="test", channel="web")
        await session.commit()
        return row.id


async def configure(client):
    return await client.put("/api/email/gmail", json={"sender": "owner@gmail.com", "sender_name": "ReflexIA", "app_password": "abcd efgh ijkl mnop"})


def body(**overrides):
    return {"recipient": "recipient@example.com", "subject": "Tu respuesta", "body": "Respuesta con más información https://outlook.office.com/bookwithme/", "request_id": str(uuid4()), **overrides}


async def test_gmail_credentials_are_encrypted_not_exposed_and_validation_blocks_headers(client, database):
    assert (await client.get("/api/email/gmail")).json()["configured"] is False
    response = await configure(client)
    assert response.status_code == 200, response.text
    assert response.json()["sender"] == "owner@gmail.com" and "app_password" not in response.text
    async with database() as session:
        row = await session.scalar(select(Credential).where(Credential.name == email.CREDENTIAL_NAME))
        assert "abcdefghijklmnop" not in row.ciphertext
    cid = await setup(client, database)
    for data in (body(recipient="person@example.com\r\nBcc: other@example.com"), body(subject="Hello\r\nBcc: other@example.com"), body(recipient="person@example.com,second@example.com")):
        assert (await client.post(f"/api/conversations/{cid}/email", json=data)).status_code == 422
    assert (await client.put("/api/email/gmail", headers={"Authorization": ""}, json={"sender": "owner@gmail.com", "app_password": "abcdefghijklmnop"})).status_code == 401
    await client.delete("/api/email/gmail")
    assert (await client.get("/api/email/gmail")).json()["configured"] is False


async def test_gmail_connection_test_authenticates_without_sending(client, monkeypatch):
    await configure(client)
    smtp = MagicMock()
    factory = MagicMock(return_value=smtp)
    monkeypatch.setattr(email.smtplib, "SMTP_SSL", factory)
    result = await client.post("/api/email/gmail/test")
    assert result.status_code == 200, result.text
    assert result.json()["ok"]
    assert factory.call_args.args == ("smtp.gmail.com", 465)
    assert factory.call_args.kwargs["context"].check_hostname
    smtp.login.assert_called_once_with("owner@gmail.com", "abcdefghijklmnop")
    smtp.send_message.assert_not_called()


async def test_email_enqueue_is_idempotent_and_worker_sends_once(client, database, monkeypatch):
    cid = await setup(client, database)
    data = body()
    assert (await client.post(f"/api/conversations/{cid}/email", json=data)).status_code == 409
    await configure(client)
    result = await client.post(f"/api/conversations/{cid}/email", json=data)
    assert result.status_code == 202, result.text
    job_id = result.json()["job_id"]
    repeated = await client.post(f"/api/conversations/{cid}/email", json=data)
    assert repeated.json()["job_id"] == job_id
    changed = await client.post(f"/api/conversations/{cid}/email", json={**data, "body": "Different"})
    assert changed.status_code == 409
    smtp = MagicMock()
    monkeypatch.setattr(email.smtplib, "SMTP_SSL", MagicMock(return_value=smtp))
    monkeypatch.setattr(worker, "Session", database)
    await worker.process(job_id)
    await worker.process(job_id)
    smtp.send_message.assert_called_once()
    message = smtp.send_message.call_args.args[0]
    assert message["To"] == data["recipient"]
    assert "https://outlook.office.com/bookwithme/" in message.get_content()
    async with database() as session:
        job = await session.get(Job, job_id)
        assert job.status == "DONE" and job.payload["email_sent"]
        assert "app_password" not in job.payload


async def test_uncertain_email_delivery_is_not_automatically_repeated(client, database, monkeypatch):
    cid = await setup(client, database)
    await configure(client)
    job_id = (await client.post(f"/api/conversations/{cid}/email", json=body())).json()["job_id"]
    smtp = MagicMock()
    smtp.send_message.side_effect = TimeoutError("unknown delivery status")
    monkeypatch.setattr(email.smtplib, "SMTP_SSL", MagicMock(return_value=smtp))
    monkeypatch.setattr(worker, "Session", database)
    await worker.process(job_id)
    async with database() as session:
        assert (await session.get(Job, job_id)).status == "NEEDS_REVIEW"
    await worker.process(job_id)
    smtp.send_message.assert_called_once()
    assert (await client.post(f"/api/jobs/{job_id}/retry", json={})).status_code == 409


async def test_rejected_gmail_auth_is_safe_to_retry_and_redacts_provider_errors(client, database, monkeypatch):
    cid = await setup(client, database)
    await configure(client)
    job_id = (await client.post(f"/api/conversations/{cid}/email", json=body())).json()["job_id"]
    smtp = MagicMock()
    smtp.login.side_effect = smtplib.SMTPAuthenticationError(535, b"abcdefghijklmnop")
    monkeypatch.setattr(email.smtplib, "SMTP_SSL", MagicMock(return_value=smtp))
    monkeypatch.setattr(worker, "Session", database)
    await worker.process(job_id)
    async with database() as session:
        job = await session.get(Job, job_id)
        assert not job.payload["send_in_progress"]
        assert job.status == "PENDING" and "abcdefghijklmnop" not in job.error
    smtp.send_message.assert_not_called()


@pytest.mark.parametrize("value", ["", "a\nb@gmail.com", "Name <a@gmail.com>", "a@gmail.com,b@gmail.com"])
def test_rejects_multiple_or_invalid_addresses(value):
    with pytest.raises(ValueError):
        email.checked_email(value)
