"""Thin REST adapters: validation and transport, with business work delegated to services."""
from typing import Annotated, Literal

from fastapi import APIRouter, Depends, File, Query, Request, UploadFile
from fastapi.responses import FileResponse, PlainTextResponse, Response
from sqlalchemy.ext.asyncio import AsyncSession

from app.config import get_settings
from app.auth import require_admin
from app.db import get_session
from app.errors import AppError
from app.schemas import AgentConfig, ChatInput, CredentialInput, DatabaseInput, FAQInput, IntegrationInput, PromptInput, ProviderName, QuestionFAQInput, URLInput
from app.services.agents import AgentService
from app.services.publication import LOGO_MAX_BYTES, PublicationService
from app.services.connections import DatabaseService, IntegrationService
from app.services.conversations import ConversationService, MemoryService
from app.services.credentials import CredentialService
from app.services.email import EmailInput, EmailService, GmailInput
from app.services.knowledge import DocumentService, FAQService, FileService
from app.services.questions import AnalyticsService, QuestionService
from app.services.webhooks import WebhookService
from app.services.jobs import JobService
from app.schemas import JobRetryInput


SessionDep = Annotated[AsyncSession, Depends(get_session)]
router = APIRouter(prefix="/api", dependencies=[Depends(require_admin)])
webhooks = APIRouter(prefix="/webhooks")


async def upload_bytes(file: UploadFile):
    result = await file.read(get_settings().upload_max_bytes + 1)
    if len(result) > get_settings().upload_max_bytes:
        raise AppError("El archivo supera el límite permitido", 413)
    return result


@router.get("/agents")
async def agents(session: SessionDep):
    return await AgentService(session).list()


@router.post("/agents", status_code=201)
async def create_agent(data: AgentConfig, session: SessionDep):
    return await AgentService(session).create(data)


@router.get("/agents/{agent_id}")
async def agent(agent_id: str, session: SessionDep):
    return await AgentService(session).get(agent_id)


@router.patch("/agents/{agent_id}")
async def update_agent(agent_id: str, data: dict, session: SessionDep):
    return await AgentService(session).update(agent_id, data)


@router.get("/agents/{agent_id}/prompts")
async def prompts(agent_id: str, session: SessionDep):
    return await AgentService(session).prompts(agent_id)


@router.post("/agents/{agent_id}/logo")
async def upload_logo(agent_id: str, session: SessionDep, file: UploadFile = File()):
    return await PublicationService(session).upload_logo(agent_id, await file.read(LOGO_MAX_BYTES + 1))


@router.get("/agents/{agent_id}/logo")
async def agent_logo(agent_id: str, session: SessionDep):
    return FileResponse(await PublicationService(session).logo(agent_id), media_type="image/png", headers={"Cache-Control": "no-store", "X-Content-Type-Options": "nosniff"})


@router.delete("/agents/{agent_id}/logo")
async def delete_logo(agent_id: str, session: SessionDep):
    return await PublicationService(session).delete_logo(agent_id)


@router.post("/agents/{agent_id}/publish")
async def publish_agent(agent_id: str, session: SessionDep):
    return await PublicationService(session).publish(agent_id)


@router.delete("/agents/{agent_id}/publish")
async def unpublish_agent(agent_id: str, session: SessionDep):
    return await PublicationService(session).unpublish(agent_id)


@router.post("/agents/{agent_id}/prompts")
async def save_prompt(agent_id: str, data: PromptInput, session: SessionDep):
    return await AgentService(session).save_prompt(agent_id, data.body)


@router.post("/agents/{agent_id}/prompts/{prompt_id}/restore")
async def restore_prompt(agent_id: str, prompt_id: str, session: SessionDep):
    return await AgentService(session).restore_prompt(agent_id, prompt_id)


@router.get("/agents/{agent_id}/faqs")
async def faqs(agent_id: str, session: SessionDep):
    return await FAQService(session).list(agent_id)


@router.post("/agents/{agent_id}/faqs")
async def create_faq(agent_id: str, data: FAQInput, session: SessionDep):
    return await FAQService(session).save(agent_id, data.model_dump())


@router.get("/agents/{agent_id}/faqs/export")
async def export_faqs(agent_id: str, session: SessionDep):
    content = await FAQService(session).export_csv(agent_id)
    return Response(content, media_type="text/csv", headers={"Content-Disposition": 'attachment; filename="faqs.csv"'})


@router.post("/agents/{agent_id}/faqs/import")
async def import_faqs(agent_id: str, session: SessionDep, file: UploadFile = File()):
    return await FAQService(session).import_csv(agent_id, await upload_bytes(file))


@router.patch("/agents/{agent_id}/faqs/{faq_id}")
async def update_faq(agent_id: str, faq_id: str, data: dict, session: SessionDep):
    return await FAQService(session).save(agent_id, data, faq_id)


@router.delete("/agents/{agent_id}/faqs/{faq_id}")
async def delete_faq(agent_id: str, faq_id: str, session: SessionDep):
    return await FAQService(session).delete(agent_id, faq_id)


@router.get("/agents/{agent_id}/documents")
async def documents(agent_id: str, session: SessionDep):
    return await DocumentService(session).list(agent_id)


@router.post("/agents/{agent_id}/documents", status_code=202)
async def upload_document(agent_id: str, session: SessionDep, file: UploadFile = File()):
    return await DocumentService(session).upload(agent_id, file.filename or "document.txt", await upload_bytes(file))


@router.post("/agents/{agent_id}/documents/url", status_code=202)
async def document_url(agent_id: str, data: URLInput, session: SessionDep):
    return await DocumentService(session).upload_url(agent_id, data.url)


@router.post("/agents/{agent_id}/documents/{document_id}/index", status_code=202)
async def index_document(agent_id: str, document_id: str, session: SessionDep):
    return await DocumentService(session).reindex(agent_id, document_id)


@router.delete("/agents/{agent_id}/documents/{document_id}")
async def delete_document(agent_id: str, document_id: str, session: SessionDep):
    return await DocumentService(session).delete(agent_id, document_id)


@router.get("/agents/{agent_id}/files")
async def files(agent_id: str, session: SessionDep):
    return await FileService(session).list(agent_id)


@router.post("/agents/{agent_id}/files", status_code=201)
async def upload_file(agent_id: str, session: SessionDep, file: UploadFile = File()):
    return await FileService(session).upload(agent_id, file.filename or "archivo", await upload_bytes(file))


@router.get("/agents/{agent_id}/files/{file_id}/download")
async def download_file(agent_id: str, file_id: str, session: SessionDep):
    row = await FileService(session).download(agent_id, file_id)
    return FileResponse(row.path, filename=row.name, media_type=row.mime_type, headers={"X-Content-Type-Options": "nosniff"})


@router.delete("/agents/{agent_id}/files/{file_id}")
async def delete_file(agent_id: str, file_id: str, session: SessionDep):
    return await FileService(session).delete(agent_id, file_id)


@router.get("/credentials")
async def credentials(session: SessionDep):
    return await CredentialService(session).status()


@router.get("/email/gmail")
async def gmail_status(session: SessionDep):
    return await EmailService(session).status()


@router.put("/email/gmail")
async def configure_gmail(data: GmailInput, session: SessionDep):
    return await EmailService(session).configure(data)


@router.delete("/email/gmail")
async def disconnect_gmail(session: SessionDep):
    return await EmailService(session).disconnect()


@router.post("/email/gmail/test")
async def test_gmail(session: SessionDep):
    return await EmailService(session).test()


@router.post("/conversations/{conversation_id}/email", status_code=202)
async def email_response(conversation_id: str, data: EmailInput, session: SessionDep):
    return await EmailService(session).enqueue(conversation_id, data)


@router.put("/credentials/{provider}")
async def credential(provider: ProviderName, data: CredentialInput, session: SessionDep):
    return await CredentialService(session).set_provider(provider, data.api_key.get_secret_value())


@router.post("/credentials/{provider}/test")
async def test_provider(provider: ProviderName, session: SessionDep):
    result = await CredentialService(session).models(provider)
    return {"ok": True, "model_count": len(result["models"])}


@router.get("/providers/{provider}/models")
async def provider_models(provider: ProviderName, session: SessionDep, purpose: Literal["chat", "embeddings"] = "chat"):
    return await CredentialService(session).models(provider, purpose=purpose)


@router.get("/agents/{agent_id}/databases")
async def databases(agent_id: str, session: SessionDep):
    return await DatabaseService(session).list(agent_id)


@router.post("/agents/{agent_id}/databases")
async def create_database(agent_id: str, data: DatabaseInput, session: SessionDep):
    return await DatabaseService(session).save(agent_id, data)


@router.patch("/agents/{agent_id}/databases/{database_id}")
async def update_database(agent_id: str, database_id: str, data: DatabaseInput, session: SessionDep):
    return await DatabaseService(session).save(agent_id, data, database_id)


@router.delete("/agents/{agent_id}/databases/{database_id}")
async def delete_database(agent_id: str, database_id: str, session: SessionDep):
    return await DatabaseService(session).delete(agent_id, database_id)


@router.post("/agents/{agent_id}/databases/{database_id}/test")
async def test_database(agent_id: str, database_id: str, session: SessionDep):
    return await DatabaseService(session).test(agent_id, database_id)


@router.get("/agents/{agent_id}/integrations")
async def integrations(agent_id: str, session: SessionDep):
    return await IntegrationService(session).list(agent_id)


@router.post("/agents/{agent_id}/integrations")
async def integration(agent_id: str, data: IntegrationInput, session: SessionDep):
    return await IntegrationService(session).save(agent_id, data)


@router.post("/agents/{agent_id}/integrations/{integration_id}/test")
async def test_integration(agent_id: str, integration_id: str, session: SessionDep):
    return await IntegrationService(session).test(agent_id, integration_id)


@router.post("/agents/{agent_id}/chat")
async def chat(agent_id: str, data: ChatInput, session: SessionDep):
    # ChatInput valida texto/canal; el router exige administración antes de aceptar el mensaje.
    # El servicio carga memoria y ejecuta el grafo; runtime_hooks.structured es quien envía al LLM.
    return await ConversationService(session).chat(agent_id, data)


@router.get("/conversations")
async def conversations(session: SessionDep, agent_id: str | None = None):
    return await ConversationService(session).list(agent_id)


@router.get("/conversations/{conversation_id}")
async def conversation(conversation_id: str, session: SessionDep):
    return await ConversationService(session).get(conversation_id)


@router.post("/conversations/{conversation_id}/close")
async def close_conversation(conversation_id: str, session: SessionDep):
    return await ConversationService(session).close(conversation_id)


@router.post("/conversations/{conversation_id}/files")
async def conversation_file(conversation_id: str, session: SessionDep, file: UploadFile = File()):
    conversation = await ConversationService(session).get(conversation_id)
    return await FileService(session).upload(conversation["agent_id"], file.filename or "archivo", await upload_bytes(file), "CONVERSATION", conversation_id)


@router.get("/agents/{agent_id}/memory")
async def memory(agent_id: str, session: SessionDep):
    return await ConversationService(session).list(agent_id)


@router.delete("/agents/{agent_id}/memory")
async def clear_memory(agent_id: str, session: SessionDep):
    return await MemoryService(session).clear(agent_id)


@router.get("/questions")
async def questions(session: SessionDep, agent_id: str | None = None, unanswered: bool = False, out_of_scope: bool = False, limit: int = Query(1000, ge=1, le=5000)):
    return await QuestionService(session).list(agent_id, unanswered, out_of_scope, limit)


@router.post("/questions/{question_id}/faq")
async def question_faq(question_id: str, data: QuestionFAQInput, session: SessionDep):
    return await QuestionService(session).to_faq(question_id, data)


@router.get("/analytics")
async def analytics(session: SessionDep, agent_id: str | None = None):
    return await AnalyticsService(session).summary(agent_id)


@router.get("/runtime/graph")
async def runtime_graph():
    from app.runtime import AgentRuntime
    return {"mermaid": AgentRuntime(None).mermaid()}


@router.get("/jobs")
async def jobs(session: SessionDep, agent_id: str | None = None):
    return await JobService(session).list(agent_id)


@router.post("/jobs/{job_id}/retry")
async def retry_job(job_id: str, data: JobRetryInput, session: SessionDep):
    return await JobService(session).retry(job_id, data.allow_resend)


@webhooks.get("/whatsapp/{agent_id}", response_class=PlainTextResponse)
async def verify_whatsapp(agent_id: str, request: Request, session: SessionDep):
    q = request.query_params
    return await WebhookService(session).verify(agent_id, q.get("hub.mode"), q.get("hub.verify_token"), q.get("hub.challenge"))


@webhooks.post("/{channel}/{agent_id}")
async def receive_webhook(channel: str, agent_id: str, request: Request, session: SessionDep):
    if channel not in {"telegram", "whatsapp"}:
        raise AppError("Canal no válido", 404)
    raw = await request.body()
    if len(raw) > 1024 * 1024:
        raise AppError("Webhook demasiado grande", 413)
    header = "X-Telegram-Bot-Api-Secret-Token" if channel == "telegram" else "X-Hub-Signature-256"
    return await WebhookService(session).receive(agent_id, channel, raw, request.headers.get(header, ""))
