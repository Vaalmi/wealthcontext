"""Public acceptance and evaluation evidence; never a source of runtime answers."""

from datetime import date
from typing import Annotated, Literal, Self

from pydantic import AwareDatetime, Field, JsonValue, StringConstraints, model_validator

from wealthcontext.contracts import Contract, ErrorCode, Identifier, QualityCode
from wealthcontext.identity import Role

Sha256 = Annotated[str, StringConstraints(pattern=r"^[0-9a-f]{64}$")]
ProviderName = Literal["anthropic", "openai", "google"]
EvaluationMode = Literal["live_scored", "live_calibration", "replay", "synthetic_test"]


class CasePrincipal(Contract):
    id: Identifier
    role: Role


class ReceivedRecord(Contract):
    source_system: str
    feed: str
    business_date: date
    recorded_at: AwareDatetime | None = None
    source_record_ids: list[str] = Field(default_factory=list)
    observation: str


class CaseRequest(Contract):
    alias: Identifier
    tool: Identifier
    arguments: dict[str, JsonValue]
    status: Literal["COMPLETE", "INCOMPLETE", "ERROR"]
    error_code: ErrorCode | None = None
    metric_refs: list[str] = Field(default_factory=list)


class RequiredFact(Contract):
    request: Identifier
    entity_type: str
    entity_id: str
    field: str
    value: str | int
    unit: Literal["USD", "pct", "count", "shares", "fx_rate", "days"]
    household_id: str | None
    valuation_date: date
    known_as_of: AwareDatetime


class RequiredPredicate(Contract):
    request: Identifier
    entity_type: str
    entity_id: str
    kind: Literal["rank", "threshold", "completeness", "reconciliation"]
    field: str
    expect: str | int


class RequiredCaveat(Contract):
    flag: QualityCode
    subject: str


class ResultCheck(Contract):
    request: Identifier
    path: str
    value: JsonValue
    comparison: Literal[
        "equals", "contains", "absent", "exact_values", "not_contains", "matches",
    ] = "equals"


class AcceptanceCase(Contract):
    version: Literal[1] = 1
    id: Annotated[str, StringConstraints(pattern=r"^(F(?:0[1-9]|1[0-9]|20)|Q(?:0[1-9]|10))$")]
    category: Literal["financial", "security_failure"]
    question: str
    scenario: str
    principal: CasePrincipal
    request_time: AwareDatetime
    valuation_date: date
    known_as_of: AwareDatetime
    household_ids: list[str]
    authoritative_sources: dict[str, str]
    received_records: list[ReceivedRecord]
    missing_records: list[str] = Field(default_factory=list)
    requests: list[CaseRequest]
    required_facts: list[RequiredFact] = Field(default_factory=list)
    required_predicates: list[RequiredPredicate] = Field(default_factory=list)
    required_caveats: list[RequiredCaveat] = Field(default_factory=list)
    result_checks: list[ResultCheck] = Field(default_factory=list)
    required_mentions: list[str] = Field(default_factory=list)
    forbidden_mentions: list[str] = Field(default_factory=list)
    expected_refusal: ErrorCode | None = None
    manual_checks: list[str] = Field(min_length=1)
    setup: list[str] = Field(default_factory=list)
    limitations: list[str] = Field(default_factory=list)
    required_source_disclosure: bool = False
    no_new_retrieval: bool = False
    enforce_household_scope: bool = False
    execution: Literal["model", "pre_provider_security"] = "model"
    pre_provider_error: Literal["UNAUTHENTICATED", "POLICY_UNAVAILABLE"] | None = None

    @model_validator(mode="after")
    def consistent_references(self) -> Self:
        aliases = [item.alias for item in self.requests]
        if len(set(aliases)) != len(aliases):
            raise ValueError("request aliases must be unique")
        if any(item.request not in aliases for item in (
            *self.required_facts, *self.required_predicates, *self.result_checks,
        )):
            raise ValueError("expected fact references an unknown request")
        if self.id.startswith("F") != (self.category == "financial"):
            raise ValueError("case identifier and category disagree")
        pre_provider = self.execution == "pre_provider_security"
        if pre_provider and (self.category != "security_failure" or self.pre_provider_error is None
                             or self.requests or self.required_facts or self.required_predicates
                             or self.required_caveats or self.result_checks or self.expected_refusal
                             or self.no_new_retrieval):
            raise ValueError("pre-provider security evidence cannot require model submissions")
        if not pre_provider and self.pre_provider_error is not None:
            raise ValueError("pre-provider error requires explicit pre-provider execution")
        if self.enforce_household_scope and not self.household_ids:
            raise ValueError("a disclosure scope requires explicit households")
        if not self.requests and not (
            self.category == "security_failure" and (self.no_new_retrieval or pre_provider)
        ):
            raise ValueError("only explicit non-retrieval security cases may omit requests")
        if self.no_new_retrieval and (self.requests or self.required_facts
                                      or self.required_predicates or self.expected_refusal):
            raise ValueError("no-new-retrieval case cannot require unavailable result references")
        return self


class GovernedResult(Contract):
    tool: str
    arguments: dict[str, JsonValue]
    envelope: dict[str, JsonValue]


class Grade(Contract):
    passed: bool
    diagnostics: list[str] = Field(default_factory=list)


class AttemptEvidence(Contract):
    attempt: int = Field(ge=1, le=2)
    candidate: dict[str, JsonValue] | None = None
    rendered_answer: dict[str, JsonValue] | None = None
    valid: bool
    diagnostics: list[str] = Field(default_factory=list)


class TrialRecord(Contract):
    run_id: Identifier
    case_id: str
    trial: int = Field(ge=1, le=3)
    provider: ProviderName
    model: str
    actual_model_ids: list[str] = Field(default_factory=list)
    settings: dict[str, JsonValue]
    mode: EvaluationMode
    case_set_sha256: Sha256
    prompt_sha256: Sha256
    schema_sha256: Sha256
    started_at: AwareDatetime
    completed_at: AwareDatetime
    attempts: list[AttemptEvidence] = Field(default_factory=list, max_length=2)
    governed_results: list[GovernedResult] = Field(default_factory=list)
    transcript: dict[str, JsonValue] = Field(default_factory=dict)
    first_pass_valid: bool
    final_valid: bool
    rendered_answer_valid: bool = False
    retries: int = Field(ge=0, le=1)
    fallback_used: bool
    provider_invoked: bool | None = True
    security_check_passed: bool | None = None
    security_evidence: dict[str, JsonValue] = Field(default_factory=dict)
    first_attempt_grade: Grade
    final_grade: Grade
    failure: str | None = None

    @model_validator(mode="after")
    def honest_attempts(self) -> Self:
        if self.completed_at < self.started_at:
            raise ValueError("trial completion precedes start")
        if self.provider_invoked is not True and (self.attempts or self.actual_model_ids):
            raise ValueError("an uninvoked provider has no model responses or submissions")
        if self.security_check_passed is not None and not self.security_evidence:
            raise ValueError("a security disposition requires actual separate evidence")
        if self.retries != max(0, len(self.attempts) - 1):
            raise ValueError("retry count must match actual submissions")
        if self.first_pass_valid != bool(self.attempts and self.attempts[0].valid):
            raise ValueError("first-pass validity must describe the first submission")
        if self.final_valid and not (self.attempts and self.attempts[-1].valid):
            raise ValueError("final model validity requires an accepted model submission")
        if self.first_attempt_grade.passed and not self.first_pass_valid:
            raise ValueError("an invalid first submission cannot pass acceptance")
        if self.final_grade.passed and (not self.final_valid or self.fallback_used or self.failure):
            raise ValueError("fallbacks or failed/invalid runs cannot pass model acceptance")
        if self.mode.startswith("live_") and not self.actual_model_ids and not self.failure:
            raise ValueError("a completed live run requires returned provider model evidence")
        return self


class EvaluationRun(Contract):
    id: Identifier
    provider: ProviderName
    model: str
    mode: EvaluationMode
    status: Literal["running", "completed", "failed", "deferred_not_run"]
    recorded_at: AwareDatetime
    completed_at: AwareDatetime | None = None
    case_set_sha256: Sha256
    prompt_sha256: Sha256
    schema_sha256: Sha256
    settings: dict[str, JsonValue]
    summary: dict[str, JsonValue] = Field(default_factory=dict)
