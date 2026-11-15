"""Provider-neutral turns and bounded-run evidence; no credentials or opaque reasoning."""

from datetime import datetime
from typing import Literal, Protocol

from pydantic import Field, JsonValue

from wealthcontext.contracts import Contract, Identifier
from wealthcontext.identity import Role

ProviderName = Literal["anthropic", "openai", "google"]
RunMode = Literal["live_calibration", "live_scored", "live_chat", "synthetic_test", "replay"]


class ToolSpec(Contract):
    name: Identifier
    description: str
    input_schema: dict[str, JsonValue]


class ToolCall(Contract):
    id: Identifier
    name: Identifier
    arguments: dict[str, JsonValue]


class ProviderRequest(Contract):
    system_prompt: str
    messages: list[dict[str, JsonValue]]
    tools: list[ToolSpec]
    terminal_only: bool = False
    authorized_result_ids: list[Identifier] = Field(default_factory=list)


class ProviderTurn(Contract):
    calls: list[ToolCall] = Field(default_factory=list, max_length=16)
    # This is unverified provider text. The harness never renders it as an answer.
    text: str | None = None
    model: str
    request_id: str | None = None
    response_id: str | None = None
    usage: dict[str, JsonValue] = Field(default_factory=dict)


class Provider(Protocol):
    provider: str
    model: str
    settings: dict[str, JsonValue]
    # Optional: transport retries taken by the latest complete() call, updated before each
    # retry so a harness timeout still records them. An adapter without it records zero.
    # last_transport_retries: int

    async def complete(self, request: ProviderRequest) -> ProviderTurn: ...


class ProviderFailure(RuntimeError):
    """Sanitized provider failure. SDK exception bodies never enter recorded evidence."""

    def __init__(self, code: str):
        self.code = code
        super().__init__(code)


class HarnessLimits(Contract):
    max_data_calls: int = Field(default=8, ge=0, le=8)
    per_call_timeout_seconds: float = Field(default=30, gt=0, le=120)
    total_timeout_seconds: float = Field(default=180, gt=0, le=600)


MAX_TRANSPORT_RETRIES = 2


class TurnRecord(Contract):
    request: ProviderRequest
    response: ProviderTurn | None = None
    error: str | None = None
    transport_retries: int = Field(default=0, ge=0, le=MAX_TRANSPORT_RETRIES)
    envelopes: list[dict[str, JsonValue]] = Field(default_factory=list)


class HarnessRun(Contract):
    run_id: Identifier
    conversation_id: Identifier
    principal_id: Identifier | None = None
    principal_role: Role | None = None
    provider: str
    model: str
    settings: dict[str, JsonValue]
    prompt_sha256: str
    schema_sha256: str
    started_at: datetime
    completed_at: datetime
    authorization_started_at: datetime | None = None
    authorization_completed_at: datetime | None = None
    mode: RunMode
    turns: list[TurnRecord] = Field(default_factory=list)
    terminal_attempts: list[dict[str, JsonValue]] = Field(default_factory=list)
    first_pass_valid: bool = False
    final_valid: bool = False
    retries: int = Field(default=0, ge=0, le=1)
    # Provider transport retries (429, 5xx, connection) summed over this run's turns;
    # distinct from `retries`, the single structured answer retry.
    transport_retries: int = Field(default=0, ge=0)
    fallback: bool = False
    failure: str | None = None
    data_tool_calls: int = Field(default=0, ge=0, le=8)
    final_result: dict[str, JsonValue] | None = None
    rendered_context: dict[str, JsonValue] | None = None
