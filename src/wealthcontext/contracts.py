"""Version 1 wire contracts shared by REST, MCP and the answer verifier."""

from datetime import date, datetime
from typing import Annotated, Literal, Self

from pydantic import (
    AfterValidator,
    AwareDatetime,
    BaseModel,
    ConfigDict,
    Field,
    JsonValue,
    StrictInt,
    StringConstraints,
    model_validator,
)

from wealthcontext.clock import utc_timestamp


def _knowledge_cutoff(value: datetime) -> datetime:
    utc_timestamp(value)
    return value


KnowledgeCutoff = Annotated[AwareDatetime, AfterValidator(_knowledge_cutoff)]
Identifier = Annotated[str, StringConstraints(pattern=r"^[A-Za-z0-9][A-Za-z0-9_.:@-]{0,127}$")]
DecimalString = Annotated[str, StringConstraints(pattern=r"^-?(0|[1-9][0-9]*)(\.[0-9]+)?$")]
PolicyRevision = Annotated[str, StringConstraints(pattern=r"^sha256:[0-9a-f]{64}$")]
ErrorCode = Literal[
    "UNAUTHENTICATED", "NOT_AUTHORIZED", "NOT_FOUND", "INVALID_ARGUMENT",
    "AS_OF_OUT_OF_RANGE", "UNSUPPORTED_CURRENCY", "RESULT_TOO_LARGE",
    "POLICY_UNAVAILABLE", "INTERNAL",
]
QualityCode = Literal[
    "AMBIGUOUS", "AMBIGUOUS_ENTITY", "PRICE_STALE", "SOURCE_UNAVAILABLE", "FX_UNAVAILABLE",
    "HOUSEHOLD_MEMBERSHIP_CONFLICT", "REPORTING_HOUSEHOLD_CONFLICT", "OWNERSHIP_CONFLICT",
    "OWNERSHIP_UNAVAILABLE",
    "PENDING_SETTLEMENT", "QUANTITY_BREAK", "CASH_BREAK", "RECONCILIATION_UNAVAILABLE",
    "PRICE_SOURCE_MISMATCH", "CLOSED_ACCOUNT_WITH_BALANCE", "HELD_AWAY_EXCLUDED",
]


class Contract(BaseModel):
    model_config = ConfigDict(
        extra="forbid", validate_assignment=True, allow_inf_nan=False,
        json_schema_serialization_defaults_required=True,
    )


class Error(Contract):
    code: ErrorCode
    message: str = Field(min_length=1, max_length=500)
    retryable: bool
    details: dict[str, JsonValue] = Field(default_factory=dict)


class QualityFlag(Contract):
    code: QualityCode
    severity: Literal["info", "warning"]
    subject: Identifier
    detail: dict[str, JsonValue] = Field(default_factory=dict)


ENVELOPE_STATUS_RULES: list[JsonValue] = [{
    "if": {"properties": {"status": {"const": "ERROR"}}},
    "then": {"properties": {"data": {"type": "null"}, "error": {"type": "object"}}},
    "else": {"properties": {
        "data": {"type": "object"}, "error": {"type": "null"},
        "entitlement_revision": {"type": "integer", "minimum": 0},
        "policy_revision": {"type": "string", "pattern": "^sha256:[0-9a-f]{64}$"},
    }},
}]


class Envelope[Data](Contract):
    model_config = ConfigDict(json_schema_extra={"allOf": ENVELOPE_STATUS_RULES})
    contract_version: Literal["1.0"] = "1.0"
    trace_id: Identifier
    result_id: Identifier
    status: Literal["COMPLETE", "INCOMPLETE", "ERROR"]
    as_of: date | None
    valuation_date: date | None
    known_as_of: AwareDatetime | None
    calculated_at: AwareDatetime
    entitlement_revision: int | None = Field(ge=0)
    policy_revision: PolicyRevision | None
    data: Data | None
    quality_flags: list[QualityFlag] = Field(default_factory=list)
    source_refs: list[Identifier] = Field(default_factory=list)
    metric_refs: list[Identifier] = Field(default_factory=list)
    error: Error | None = None

    @model_validator(mode="after")
    def consistent_status(self) -> Self:
        if self.status == "ERROR":
            if self.error is None or self.data is not None:
                raise ValueError("ERROR requires an error and no data")
        elif self.error is not None or self.data is None:
            raise ValueError("successful/partial results require data and no request error")
        elif self.entitlement_revision is None or self.policy_revision is None:
            raise ValueError("successful/partial results require verified revisions")
        return self


class EntityReference(Contract):
    type: Literal[
        "result_summary", "household", "account", "issuer", "security", "position",
        "lot", "transaction", "feed_run",
    ]
    id: Identifier


class Claim(Contract):
    id: Annotated[str, StringConstraints(pattern=r"^c[1-9][0-9]*$")]
    result_id: Identifier
    entity: EntityReference
    field: Identifier
    household_id: Identifier | None
    valuation_date: date
    unit: Literal["USD", "pct", "count", "shares", "fx_rate", "days"]
    format: Literal["value_only", "label_and_value"]


class PredicateReference(Contract):
    id: Annotated[str, StringConstraints(pattern=r"^p[1-9][0-9]*$")]
    result_id: Identifier
    entity: EntityReference


class RankPredicate(PredicateReference):
    kind: Literal["rank"]
    field: Literal["rank"]
    expect: StrictInt = Field(ge=1)


class ThresholdPredicate(PredicateReference):
    kind: Literal["threshold"]
    field: Literal["applied_threshold_pct"]
    expect: Literal["AT_OR_ABOVE", "BELOW"]


class CompletenessPredicate(PredicateReference):
    kind: Literal["completeness"]
    field: Literal["status"]
    expect: Literal["COMPLETE", "INCOMPLETE"]


class ReconciliationPredicate(PredicateReference):
    kind: Literal["reconciliation"]
    field: Literal["reconciliation_status"]
    expect: Literal["MATCHED", "PENDING_SETTLEMENT", "BREAK", "RECONCILIATION_UNAVAILABLE"]


Predicate = Annotated[
    RankPredicate | ThresholdPredicate | CompletenessPredicate | ReconciliationPredicate,
    Field(discriminator="kind"),
]


class Caveat(Contract):
    flag: QualityCode
    subject: Identifier


class Refusal(Contract):
    code: ErrorCode
    result_id: Identifier


class Answer(Contract):
    answer_markdown: str = Field(min_length=1, max_length=8000)
    claims: list[Claim] = Field(max_length=60)
    predicates: list[Predicate] = Field(max_length=60)
    caveats: list[Caveat] = Field(max_length=100)
    refusal: Refusal | None

    @model_validator(mode="after")
    def unique_ids(self) -> Self:
        ids = [item.id for item in self.claims] + [item.id for item in self.predicates]
        if len(ids) != len(set(ids)):
            raise ValueError("claim and predicate identifiers must be unique")
        if self.refusal and (self.claims or self.predicates):
            raise ValueError("a refusal cannot also make claims")
        return self
