"""Browser chat contracts contain current display data, never provider evidence."""

import json
from datetime import date
from typing import Literal, Self

from pydantic import Field, JsonValue, model_validator

from wealthcontext.contracts import Contract, Identifier, KnowledgeCutoff
from wealthcontext.provider_contracts import ProviderName


class ChatArguments(Contract):
    question: str = Field(min_length=1, max_length=12000, pattern=r"\S")
    provider: ProviderName = "openai"
    conversation_id: Identifier | None = None
    as_of: date | None = None
    known_as_of: KnowledgeCutoff | None = None

    def request_text(self) -> str:
        if self.as_of is None and self.known_as_of is None:
            return self.question
        return json.dumps(self.model_dump(
            mode="json", include={"question", "as_of", "known_as_of"}, exclude_none=True,
        ), sort_keys=True, ensure_ascii=False)

    @model_validator(mode="after")
    def bounded_message(self) -> Self:
        if len(self.request_text()) > 16000:
            raise ValueError("structured user request exceeds the conversation message limit")
        return self


class HistoryArguments(Contract):
    conversation_id: Identifier


class ChatAnswer(Contract):
    result_id: Identifier
    data: dict[str, JsonValue]


class ChatRunResponse(Contract):
    run_id: Identifier
    conversation_id: Identifier
    provider: ProviderName
    model: str
    mode: Literal["live_chat"] = "live_chat"
    first_pass_valid: bool
    final_valid: bool
    retries: int = Field(ge=0, le=1)
    fallback: bool
    failure: str | None
    final_result: ChatAnswer | None


class HistoryMessage(Contract):
    role: Literal["user", "assistant"]
    content: str
    redacted: bool = False
    result_id: Identifier | None = None
    data: dict[str, JsonValue] | None = None


class ChatHistory(Contract):
    conversation_id: Identifier
    messages: list[HistoryMessage]
