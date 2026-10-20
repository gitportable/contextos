from datetime import datetime
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

class AssistantQuery(BaseModel):
    question: str = Field(min_length=1, max_length=5000)

class SlackReply(BaseModel):
    text: str = Field(min_length=1, max_length=4000)
    thread_ts: str | None = None
    approved: bool = False
