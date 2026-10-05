from typing import Literal

from pydantic import BaseModel, ConfigDict, Field, field_validator


class StrictModel(BaseModel):
    model_config = ConfigDict(extra="forbid", str_strip_whitespace=True)


class ConversationCreate(StrictModel):
    title: str = Field(default="New conversation", min_length=1, max_length=160)
    selected_company: str | None = Field(default=None, pattern=r"^[A-Za-z0-9_-]{1,12}$")


class ConversationUpdate(StrictModel):
    title: str | None = Field(default=None, min_length=1, max_length=160)
    archived: bool | None = None


class ChatRequest(StrictModel):
    conversation_id: str = Field(pattern=r"^[a-fA-F0-9-]{36}$")
    message: str = Field(min_length=1, max_length=12000)
    retry_message_id: str | None = Field(default=None, pattern=r"^[a-fA-F0-9-]{36}$")

    @field_validator("message")
    @classmethod
    def no_empty_message(cls, value):
        if not value.strip():
            raise ValueError("Please enter a message")
        return value


class ActionRequest(StrictModel):
    conversation_id: str = Field(pattern=r"^[a-fA-F0-9-]{36}$")


class FeedbackRequest(StrictModel):
    value: Literal[-1, 1]


class ExportRequest(StrictModel):
    format: Literal["json", "markdown"] = "markdown"
