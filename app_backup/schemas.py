from datetime import datetime
from typing import Literal

from pydantic import BaseModel, Field


class ReminderCreate(BaseModel):
    title: str = Field(min_length=1, max_length=300)
    due_at: datetime
    message_id: int | None = None


class EventCreate(BaseModel):
    title: str = Field(min_length=1, max_length=300)
    proposed_start: datetime
    duration_minutes: int = Field(default=30, ge=5, le=1440)
    message_id: int | None = None
    source_text: str = ""


class AssistantHistoryTurn(BaseModel):
    role: Literal["user", "assistant"]
    content: str = Field(min_length=1, max_length=1500)


class AssistantQuery(BaseModel):
    question: str = Field(min_length=1, max_length=5000)
    history: list[AssistantHistoryTurn] = Field(default_factory=list, max_length=8)


class SlackReply(BaseModel):
    text: str = Field(min_length=1, max_length=4000)
    thread_ts: str | None = None
    approved: bool = False
    send_as: Literal["user", "bot"] = "user"
    client_request_id: str | None = Field(default=None, max_length=100)  # idempotency key from the composer


class SlackChannelPost(BaseModel):
    text: str = Field(min_length=1, max_length=4000)
    approved: bool = False
    client_request_id: str | None = Field(default=None, max_length=100)


class ProjectCreate(BaseModel):
    name: str = Field(min_length=2, max_length=100)
    keywords: list[str] = Field(default_factory=list, max_length=20)


class ProjectPatch(BaseModel):
    name: str | None = Field(default=None, min_length=2, max_length=100)
    keywords: list[str] | None = Field(default=None, max_length=20)
    status: Literal["confirmed", "archived"] | None = None


class ProjectMerge(BaseModel):
    into_id: int


class ConnectRequest(BaseModel):
    level: Literal["essential", "recommended", "with_replies"] = "recommended"


class RegisterIn(BaseModel):
    email: str = Field(min_length=3, max_length=320)
    password: str = Field(min_length=10, max_length=200)
    name: str = Field(default="", max_length=200)


class LoginIn(BaseModel):
    email: str = Field(min_length=3, max_length=320)
    password: str = Field(min_length=1, max_length=200)


class SyncRequest(BaseModel):
    connection_id: int | None = None
    mode: Literal["auto", "initial", "incremental", "reconcile"] = "auto"


class DisconnectRequest(BaseModel):
    connection_id: int
    purge: bool = False  # also delete imported messages
