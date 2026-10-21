from datetime import datetime
from typing import Literal

from pydantic import BaseModel, Field


class ReminderCreate(BaseModel):
    title: str = Field(min_length=1, max_length=300)
    due_at: datetime
    message_id: int | None = None
    assignees: list[str] = Field(default_factory=list, max_length=20)


class EventCreate(BaseModel):
    title: str = Field(min_length=1, max_length=300)
    proposed_start: datetime
    duration_minutes: int = Field(default=30, ge=5, le=1440)
    message_id: int | None = None
    source_text: str = ""
    assignees: list[str] = Field(default_factory=list, max_length=20)


class AssigneesUpdate(BaseModel):
    assignees: list[str] = Field(max_length=20)


class ProfileUpdate(BaseModel):
    name: str = Field(min_length=1, max_length=200)


class PasswordChange(BaseModel):
    current_password: str = Field(min_length=1, max_length=200)
    new_password: str = Field(min_length=10, max_length=200)


class AssistantQuery(BaseModel):
    question: str = Field(min_length=1, max_length=5000)


class SlackReply(BaseModel):
    text: str = Field(min_length=1, max_length=4000)
    thread_ts: str | None = None
    approved: bool = False
    send_as: Literal["user", "bot"] = "user"
    client_request_id: str | None = Field(default=None, max_length=100)  # idempotency key from the composer


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
