from typing import Literal

from pydantic import BaseModel, ConfigDict, Field, model_validator

Category = Literal["healthcare", "invoices", "agreements", "guidelines", "general"]


class StrictModel(BaseModel):
    model_config = ConfigDict(extra="forbid", strict=True)


class TextRequest(StrictModel):
    category: Category = "general"
    text: str | None = Field(None, min_length=1, max_length=32000)
    data: dict | list | None = None

    @model_validator(mode="after")
    def exactly_one_input(self):
        if (self.text is None) == (self.data is None):
            raise ValueError("exactly_one_input_required")
        return self


class Message(StrictModel):
    role: Literal["system", "user", "assistant"]
    content: str = Field(min_length=1, max_length=32000)


class ChatRequest(StrictModel):
    category: Category = "general"
    messages: list[Message] = Field(min_length=1, max_length=32)
    max_tokens: int = Field(512, ge=1, le=2048)
    # From /preview: refuse to send unless the first message masks to exactly what was reviewed.
    approved_preview_sha256: str | None = Field(None, pattern=r"^[0-9a-f]{64}$")


class Entity(StrictModel):
    start: int
    end: int
    entity_type: str
    confidence: float
    source: str
