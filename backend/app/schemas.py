from typing import Literal

from urllib.parse import urlsplit

from pydantic import BaseModel, ConfigDict, Field, SecretStr, field_validator, model_validator

ProviderName = Literal["openai", "gemini"]


class Input(BaseModel):
    model_config = ConfigDict(extra="forbid", str_strip_whitespace=True)


class ResponseLink(Input):
    label: str = Field(min_length=1, max_length=120)
    url: str = Field(min_length=8, max_length=2000)

    @field_validator("url")
    @classmethod
    def safe_url(cls, value):
        parsed = urlsplit(value)
        if parsed.scheme not in {"http", "https"} or not parsed.hostname or parsed.username or parsed.password or any(c.isspace() for c in value):
            raise ValueError("Usa una liga HTTP(S) válida, sin credenciales")
        return value


class AgentConfig(Input):
    name: str = Field(min_length=1, max_length=200)
    description: str = Field(default="", max_length=4000)
    avatar: str = Field(default="🤖", max_length=2000)
    welcome_message: str = "Hola, ¿en qué puedo ayudarte?"
    response_links: list[ResponseLink] = Field(default_factory=list, max_length=20)
    system_prompt: str = Field(default="Responde en español usando las fuentes del agente. Si no sabes, indícalo.", max_length=100000)
    provider: ProviderName = "openai"
    model: str = ""
    llm_timeout_seconds: int = Field(default=45, ge=5, le=120)
    llm_max_retries: int = Field(default=0, ge=0, le=2)
    llm_max_output_tokens: int | None = Field(default=None, ge=256, le=32000)
    llm_reasoning_effort: Literal["default", "low", "medium", "high"] = "default"
    exact_faq_enabled: bool = True
    embedding_provider: ProviderName = "openai"
    embedding_model: str = "text-embedding-3-small"
    scope_description: str = Field(default="", max_length=20000)
    out_of_scope_policy: Literal["CLOSE", "WARN", "CONTINUE"] = "WARN"
    out_of_scope_message: str = "Esta pregunta está fuera de mi alcance. Puedo ayudarte con los temas definidos para este agente."
    closing_message: str = "La conversación ha finalizado. Gracias por escribir."
    max_interactions: int = Field(default=10, ge=1, le=1000)
    memory_enabled: bool = True
    memory_window: int = Field(default=20, ge=0, le=200)
    # Acota el JSON del historial además del número de mensajes; no borra memoria persistida.
    max_history_chars: int = Field(default=16000, ge=1000, le=120000)
    # Presupuesto conjunto para FAQ, fragmentos RAG y resultados de herramientas.
    max_context_chars: int = Field(default=32000, ge=1000, le=120000)
    # Impide enviar inventarios SQL/archivos excesivos al planificador del modelo.
    max_tool_context_chars: int = Field(default=16000, ge=1000, le=120000)
    chunk_size: int = Field(default=1000, ge=100, le=8000)
    chunk_overlap: int = Field(default=150, ge=0, le=2000)
    retriever_k: int = Field(default=4, ge=1, le=20)
    retriever_type: Literal["similarity", "mmr"] = "similarity"
    tools_enabled: list[Literal["database", "file", "current_time"]] = Field(default_factory=list)
    active: bool = True

    @model_validator(mode="after")
    def validate_chunks(self):
        if self.chunk_overlap >= self.chunk_size:
            raise ValueError("chunk_overlap debe ser menor que chunk_size")
        return self


class FAQInput(Input):
    question: str = Field(min_length=1, max_length=10000)
    answer: str = Field(min_length=1, max_length=50000)
    tags: list[str] = Field(default_factory=list, max_length=50)
    priority: int = Field(default=0, ge=0, le=100)
    file_id: str | None = None
    active: bool = True


class PromptInput(Input):
    body: str = Field(min_length=1, max_length=100000)


class CredentialInput(Input):
    api_key: SecretStr


class ChatInput(Input):
    message: str = Field(min_length=1, max_length=20000)
    conversation_id: str | None = None
    user_id: str = Field(default="playground", min_length=1, max_length=200)
    channel: Literal["playground", "api", "web", "telegram", "whatsapp"] = "playground"


class DatabaseInput(Input):
    name: str = Field(min_length=1, max_length=200)
    dialect: Literal["postgresql", "mysql", "mssql", "sqlite"]
    dsn: SecretStr | None = None
    allowed_tables: list[str] = Field(min_length=1, max_length=100)
    max_rows: int = Field(default=100, ge=1, le=500)


class IntegrationInput(Input):
    channel: Literal["telegram", "whatsapp"]
    enabled: bool = False
    config: dict[str, str] = Field(default_factory=dict)
    secrets: dict[str, SecretStr] = Field(default_factory=dict)


class URLInput(Input):
    url: str = Field(min_length=8, max_length=2000)


class QuestionFAQInput(Input):
    answer: str | None = None
    tags: list[str] = Field(default_factory=list)
    priority: int = Field(default=0, ge=0, le=100)


class JobRetryInput(Input):
    allow_resend: bool = False
