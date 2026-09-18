"""PostgreSQL is the source of truth; vector indexes are rebuildable projections."""
from datetime import datetime, timezone
from uuid import uuid4

from sqlalchemy import JSON, Boolean, DateTime, ForeignKey, Integer, String, Text, UniqueConstraint
from sqlalchemy.orm import DeclarativeBase, Mapped, mapped_column


def utcnow():
    return datetime.now(timezone.utc)


class Base(DeclarativeBase):
    pass


class Record:
    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=lambda: str(uuid4()))
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow, index=True)
    updated_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow, onupdate=utcnow)


class Agent(Record, Base):
    __tablename__ = "agents"
    name: Mapped[str] = mapped_column(String(200))
    config: Mapped[dict] = mapped_column(JSON, default=dict)
    logo_filename: Mapped[str | None] = mapped_column(String(100), nullable=True)
    published: Mapped[bool] = mapped_column(Boolean, default=False, server_default="0")


class Provider(Record, Base):
    __tablename__ = "providers"
    name: Mapped[str] = mapped_column(String(40), unique=True)
    config: Mapped[dict] = mapped_column(JSON, default=dict)


class Credential(Record, Base):
    __tablename__ = "credentials"
    name: Mapped[str] = mapped_column(String(200), unique=True)
    ciphertext: Mapped[str] = mapped_column(Text)


class AgentRecord(Record):
    agent_id: Mapped[str] = mapped_column(ForeignKey("agents.id", ondelete="CASCADE"), index=True)


class Prompt(AgentRecord, Base):
    __tablename__ = "prompts"
    body: Mapped[str] = mapped_column(Text)


class FAQ(AgentRecord, Base):
    __tablename__ = "faqs"
    question: Mapped[str] = mapped_column(Text)
    answer: Mapped[str] = mapped_column(Text)
    tags: Mapped[list] = mapped_column(JSON, default=list)
    priority: Mapped[int] = mapped_column(Integer, default=0)
    file_id: Mapped[str | None] = mapped_column(String(36), nullable=True)
    active: Mapped[bool] = mapped_column(Boolean, default=True)
    status: Mapped[str] = mapped_column(String(32), default="PENDING")
    index_profile: Mapped[str | None] = mapped_column(Text, nullable=True)
    error: Mapped[str | None] = mapped_column(Text, nullable=True)


class Document(AgentRecord, Base):
    __tablename__ = "documents"
    name: Mapped[str] = mapped_column(String(255))
    path: Mapped[str] = mapped_column(Text)
    source_url: Mapped[str | None] = mapped_column(Text, nullable=True)
    status: Mapped[str] = mapped_column(String(32), default="RECEIVED")
    delete_requested: Mapped[bool] = mapped_column(Boolean, default=False)
    chunk_count: Mapped[int] = mapped_column(Integer, default=0)
    chunk_ids: Mapped[list] = mapped_column(JSON, default=list)
    index_profile: Mapped[str | None] = mapped_column(Text, nullable=True)
    error: Mapped[str | None] = mapped_column(Text, nullable=True)


class File(AgentRecord, Base):
    __tablename__ = "files"
    name: Mapped[str] = mapped_column(String(255))
    path: Mapped[str] = mapped_column(Text)
    mime_type: Mapped[str] = mapped_column(String(150))
    kind: Mapped[str] = mapped_column(String(32), default="DELIVERABLE")
    conversation_id: Mapped[str | None] = mapped_column(String(36), nullable=True)


class User(Record, Base):
    __tablename__ = "users"
    external_id: Mapped[str] = mapped_column(String(255), unique=True)
    role: Mapped[str] = mapped_column(String(24), default="user")


class Conversation(AgentRecord, Base):
    __tablename__ = "conversations"
    user_id: Mapped[str] = mapped_column(String(255), index=True)
    channel: Mapped[str] = mapped_column(String(32))
    status: Mapped[str] = mapped_column(String(24), default="ACTIVE")
    interaction_count: Mapped[int] = mapped_column(Integer, default=0)


class Message(Record, Base):
    __tablename__ = "messages"
    conversation_id: Mapped[str] = mapped_column(ForeignKey("conversations.id", ondelete="CASCADE"), index=True)
    role: Mapped[str] = mapped_column(String(24))
    content: Mapped[str] = mapped_column(Text)
    attachments: Mapped[list] = mapped_column(JSON, default=list)


class Question(AgentRecord, Base):
    __tablename__ = "questions"
    conversation_id: Mapped[str] = mapped_column(ForeignKey("conversations.id", ondelete="CASCADE"), index=True)
    user_id: Mapped[str] = mapped_column(String(255))
    channel: Mapped[str] = mapped_column(String(32))
    question: Mapped[str] = mapped_column(Text)
    scope_status: Mapped[str] = mapped_column(String(32), default="UNCERTAIN")
    faq_used: Mapped[bool] = mapped_column(Boolean, default=False)
    rag_used: Mapped[bool] = mapped_column(Boolean, default=False)
    database_used: Mapped[bool] = mapped_column(Boolean, default=False)
    answered: Mapped[bool] = mapped_column(Boolean, default=False)
    response: Mapped[str] = mapped_column(Text, default="")
    provider: Mapped[str] = mapped_column(String(32), default="")
    model: Mapped[str] = mapped_column(String(200), default="")
    tokens: Mapped[int] = mapped_column(Integer, default=0)
    latency: Mapped[int] = mapped_column(Integer, default=0)
    faq_ids: Mapped[list] = mapped_column(JSON, default=list)
    document_ids: Mapped[list] = mapped_column(JSON, default=list)
    trace: Mapped[list] = mapped_column(JSON, default=list)
    error: Mapped[str | None] = mapped_column(Text, nullable=True)


class DatabaseConnection(AgentRecord, Base):
    __tablename__ = "database_connections"
    name: Mapped[str] = mapped_column(String(200))
    dialect: Mapped[str] = mapped_column(String(32))
    credential_name: Mapped[str] = mapped_column(String(200))
    allowed_tables: Mapped[list] = mapped_column(JSON, default=list)
    max_rows: Mapped[int] = mapped_column(Integer, default=100)


class Integration(AgentRecord, Base):
    __tablename__ = "integrations"
    __table_args__ = (UniqueConstraint("agent_id", "channel"),)
    channel: Mapped[str] = mapped_column(String(32))
    enabled: Mapped[bool] = mapped_column(Boolean, default=False)
    config: Mapped[dict] = mapped_column(JSON, default=dict)
    credential_name: Mapped[str] = mapped_column(String(200))


class Job(Record, Base):
    __tablename__ = "jobs"
    kind: Mapped[str] = mapped_column(String(40))
    payload: Mapped[dict] = mapped_column(JSON)
    status: Mapped[str] = mapped_column(String(24), default="PENDING", index=True)
    attempts: Mapped[int] = mapped_column(Integer, default=0)
    locked_until: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    dedup_key: Mapped[str | None] = mapped_column(String(255), unique=True, nullable=True)
    error: Mapped[str | None] = mapped_column(Text, nullable=True)


class Log(Record, Base):
    __tablename__ = "logs"
    event: Mapped[str] = mapped_column(String(100))
    details: Mapped[dict] = mapped_column(JSON, default=dict)


class Analytics(Record, Base):
    __tablename__ = "analytics"
    metric: Mapped[str] = mapped_column(String(100))
    data: Mapped[dict] = mapped_column(JSON, default=dict)
