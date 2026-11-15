"""Closed request and response contracts for governed secondary surfaces."""

from datetime import date
from typing import Annotated, Literal, Self

from pydantic import AwareDatetime, Field, JsonValue, StringConstraints, model_validator

from wealthcontext.contracts import (
    Contract,
    DecimalString,
    Envelope,
    Identifier,
    KnowledgeCutoff,
    QualityFlag,
)
from wealthcontext.evaluation_contracts import EvaluationMode, ProviderName, Sha256
from wealthcontext.feeds import FeedName, RunStatus, SourceSystem

NameQuery = Annotated[str, StringConstraints(
    strict=True, strip_whitespace=True, min_length=1, max_length=120,
)]


class SearchArguments(Contract):
    name_query: NameQuery


class QualityArguments(Contract):
    as_of: date
    known_as_of: KnowledgeCutoff | None = None


class FeedRunsArguments(QualityArguments):
    pass


class ConflictArguments(QualityArguments):
    household_id: Identifier


class EmptyArguments(Contract):
    pass


class TraceArguments(Contract):
    trace_id: Identifier


class SurfaceList[Item](Contract):
    items: list[Item] = Field(max_length=20)
    item_count: int = Field(ge=0, le=20)
    total_item_count: int = Field(ge=0)
    ordering: str
    truncated: bool
    next_cursor: None = None

    @model_validator(mode="after")
    def counts_match(self) -> Self:
        if self.item_count != len(self.items) or self.total_item_count < self.item_count:
            raise ValueError("list counts must describe the complete evaluated list")
        if self.truncated != (self.total_item_count > self.item_count):
            raise ValueError("truncation must match the full count")
        return self


class SearchItem(Contract):
    household_id: Identifier
    household_name: str


class SearchData(SurfaceList[SearchItem]):
    evaluated_household_count: int = Field(ge=0)


class HouseholdListData(SurfaceList[SearchItem]):
    """Currently entitled households, ID and name, bounded with honest counts."""

    evaluated_household_count: int = Field(ge=0)


class HouseholdQuality(Contract):
    household_id: Identifier
    complete: bool
    flags: SurfaceList[QualityFlag]
    missing_component_count: int = Field(ge=0)
    warning_count: int = Field(ge=0)
    info_count: int = Field(ge=0)


class QualityData(SurfaceList[HouseholdQuality]):
    evaluated_household_count: int = Field(ge=0)
    households_with_issues_count: int = Field(ge=0)
    incomplete_household_count: int = Field(ge=0)
    quality_flag_count: int = Field(ge=0)


class FeedReject(Contract):
    id: Identifier
    reason: str
    raw_payload: Literal["[REDACTED]"] = "[REDACTED]"


class FeedRunItem(Contract):
    id: Identifier
    source_system: SourceSystem
    feed: FeedName
    business_date: date
    status: RunStatus
    upstream_status: RunStatus
    recorded_at: AwareDatetime
    imported_at: AwareDatetime
    accepted_records: int = Field(ge=0)
    rejected_records: int = Field(ge=0)
    content_hash: str | None
    rejects: SurfaceList[FeedReject]


class ConflictItem(Contract):
    code: str
    subject: Identifier
    current_household_id: Identifier
    detail: dict[str, JsonValue]


class DecisionItem(Contract):
    id: Identifier
    principal_id: Identifier | None
    action: str
    resources: list[Identifier]
    allowed: bool
    reason: str
    policy_revision: str | None
    entitlement_revision: int | None
    trace_id: Identifier
    recorded_at: AwareDatetime


class EvalReportItem(Contract):
    id: Identifier
    provider: ProviderName
    model: str
    actual_model_ids: list[str]
    mode: EvaluationMode
    status: Literal["running", "completed", "failed", "deferred_not_run"]
    recorded_at: AwareDatetime
    completed_at: AwareDatetime | None
    case_set_sha256: Sha256
    case_snapshot_sha256: Sha256
    prompt_sha256: Sha256
    schema_sha256: Sha256
    question_count: int = Field(ge=0)
    financial_question_count: int = Field(ge=0)
    security_question_count: int = Field(ge=0)
    expected_trial_count: int = Field(ge=0)
    recorded_trial_count: int = Field(ge=0)
    missing_trial_count: int = Field(ge=0)
    predeclared_trial_denominator: int = Field(ge=0)
    model_expected_trial_count: int = Field(ge=0)
    pre_provider_expected_trial_count: int = Field(ge=0)
    pre_provider_recorded_trial_count: int = Field(ge=0)
    pre_provider_missing_trial_count: int = Field(ge=0)
    pre_provider_check_pass_count: int = Field(ge=0)
    pre_provider_check_fail_count: int = Field(ge=0)
    pre_provider_security_checks_passed: bool
    first_attempt_pass_count: int = Field(ge=0)
    first_attempt_denominator: int = Field(ge=0)
    first_pass_valid_count: int = Field(ge=0)
    final_valid_count: int = Field(ge=0)
    rendered_answer_valid_count: int = Field(ge=0)
    final_pass_count: int = Field(ge=0)
    retry_count: int = Field(ge=0)
    fallback_count: int = Field(ge=0)
    failure_count: int = Field(ge=0)
    raw_failure_count: int = Field(ge=0)
    provider_invoked_count: int = Field(ge=0)
    provider_not_invoked_count: int = Field(ge=0)
    provider_invocation_unknown_count: int = Field(ge=0)
    security_check_pass_count: int = Field(ge=0)
    security_check_fail_count: int = Field(ge=0)
    pass_all_three: dict[str, bool]
    financial_pass_all_three_count: int = Field(ge=0)
    security_pass_all_three_count: int = Field(ge=0)
    scored: bool
    primary_financial_gate_passed: bool
    security_model_cases_passed: bool
    deterministic_security_suite: Literal["requires_separate_test_evidence"]


class TraceResultItem(Contract):
    result_id: Identifier
    status: Literal["COMPLETE", "INCOMPLETE", "ERROR"]
    payload_hash: str
    recorded_at: AwareDatetime


class TraceData(Contract):
    trace_id: Identifier
    principal_id: Identifier | None
    results: SurfaceList[TraceResultItem]
    decisions: SurfaceList[DecisionItem]


# --- ADR 0009 sections 5 to 7: the compliance contracts (C1, C2, C3) ----------------------

DecisionCursor = Annotated[str, StringConstraints(
    strict=True, min_length=1, max_length=1024, pattern=r"^[A-Za-z0-9_-]+\.[A-Za-z0-9_-]+$",
)]


class DecisionLogPageArguments(Contract):
    """Optional filters and an opaque cursor; the cursor is decoded only after an allow."""

    principal_id: Identifier | None = None
    action: Identifier | None = None
    outcome: Literal["allow", "deny"] | None = None
    # UTC-representable instants, as every knowledge cutoff is: an offset that moves an
    # instant outside years 1 to 9999 is refused here, before it reaches a filter.
    recorded_from: KnowledgeCutoff | None = None
    recorded_before: KnowledgeCutoff | None = None
    household_id: Identifier | None = None
    cursor: DecisionCursor | None = None

    @model_validator(mode="after")
    def ordered_range(self) -> Self:
        if (self.recorded_from is not None and self.recorded_before is not None
                and self.recorded_from >= self.recorded_before):
            raise ValueError("recorded_from must precede recorded_before")
        return self


class DecisionPageItem(DecisionItem):
    """The stored decision fields, and for an allow its authorized resources."""

    authorized_resources: list[Identifier] | None


class EntitlementFactItem(Contract):
    kind: str
    subject: Identifier
    object: Identifier
    valid_from: date
    valid_to: date


class RevisionFacts(Contract):
    """The grants stored for a revision, not the full input the policy saw (decision 6)."""

    revision: int = Field(ge=0)
    recorded_at: AwareDatetime
    label: Literal["FACTS_AT_STORED_REVISION"] = "FACTS_AT_STORED_REVISION"
    facts: list[EntitlementFactItem] = Field(max_length=100)
    item_count: int = Field(ge=0, le=100)
    total_item_count: int = Field(ge=0)
    ordering: str
    truncated: bool

    @model_validator(mode="after")
    def counts_match(self) -> Self:
        if self.item_count != len(self.facts) or self.total_item_count < self.item_count:
            raise ValueError("fact counts must describe the stored revision")
        if self.truncated != (self.total_item_count > self.item_count):
            raise ValueError("truncation must match the full count")
        return self


class DecisionLogPageData(Contract):
    """One page of a browse fixed at a commit-position watermark (ADR 0009 section 5)."""

    items: list[DecisionPageItem] = Field(max_length=20)
    item_count: int = Field(ge=0, le=20)
    total_item_count: int = Field(ge=0)
    ordering: str
    truncated: bool
    next_cursor: DecisionCursor | None
    browse_started_at: AwareDatetime
    revisions: list[RevisionFacts] = Field(max_length=20)
    revision_count: int = Field(ge=0, le=20)

    @model_validator(mode="after")
    def counts_match(self) -> Self:
        if self.item_count != len(self.items) or self.total_item_count < self.item_count:
            raise ValueError("list counts must describe the complete matching set")
        if self.truncated != (self.total_item_count > self.item_count):
            raise ValueError("truncation must match the full count")
        if self.revision_count != len(self.revisions):
            raise ValueError("revision count must describe the revisions returned")
        return self


class HouseholdReceiptsArguments(Contract):
    household_id: Identifier
    as_of: date
    known_as_of: KnowledgeCutoff | None = None


class ReceiptItem(Contract):
    """One feed run that first delivered records the household snapshot cites."""

    run_id: Identifier
    source_system: SourceSystem
    feed: FeedName
    business_date: date
    recorded_at: AwareDatetime
    record_count: int = Field(ge=1)


class HouseholdReceiptsData(SurfaceList[ReceiptItem]):
    household_id: Identifier
    cited_record_count: int = Field(ge=0)
    records_without_run_count: int = Field(ge=0)


SpanName = Literal[
    "agent", "tool", "authz", "query", "calc", "provider_context", "answer_render",
    "browser_history", "authenticate", "harness_http_failure", "other",
]
SpanId = Annotated[str, StringConstraints(pattern=r"^[0-9a-f]{16}$")]
TraceHex = Annotated[str, StringConstraints(pattern=r"^[0-9a-f]{32}$")]


class SpanItem(Contract):
    """Allowlisted span fields only: no attribute, event, link or resource field."""

    span_id: SpanId
    parent_span_id: SpanId | None
    parent_omitted: bool
    name: SpanName
    start_time: AwareDatetime
    duration_ms: DecimalString
    status_code: Literal["UNSET", "OK", "ERROR"]


class TraceSpansData(Contract):
    trace_id: TraceHex
    items: list[SpanItem] = Field(max_length=200)
    item_count: int = Field(ge=0, le=200)
    total_item_count: int = Field(ge=0)
    ordering: str
    truncated: bool
    span_source: Literal["EXPORT", "UNAVAILABLE"]
    scan_truncated: bool

    @model_validator(mode="after")
    def counts_match(self) -> Self:
        if self.item_count != len(self.items) or self.total_item_count < self.item_count:
            raise ValueError("list counts must describe the complete matching set")
        if self.truncated != (self.total_item_count > self.item_count):
            raise ValueError("truncation must match the full count")
        if self.span_source == "UNAVAILABLE" and (self.total_item_count or self.scan_truncated):
            raise ValueError("an unavailable export returns no spans")
        return self


# --- ADR 0009 sections 3 and 4: the operations contracts (O1, O2) ------------------------

# The closed table of expected feed cells (ADR 0009 section 3). Every source-and-feed pair
# that `FEED_SOURCES` allows is expected on every business day: a property of the synthetic
# dataset, stated here beside the feed contracts and never inferred from what arrived. Ordered
# by source system, then feed.
EXPECTED_FEED_PAIRS: tuple[tuple[SourceSystem, FeedName], ...] = (
    ("CRM", "crm"),
    ("CUSTODY-A", "custody_accounts"), ("CUSTODY-A", "custody_snapshots"),
    ("CUSTODY-A", "prices"),
    ("CUSTODY-B", "custody_accounts"), ("CUSTODY-B", "custody_snapshots"),
    ("CUSTODY-B", "prices"),
    ("FX", "fx"),
    ("PAS-A", "lots"), ("PAS-A", "pas_snapshots"), ("PAS-A", "transactions"),
    ("PAS-B", "lots"), ("PAS-B", "pas_snapshots"), ("PAS-B", "transactions"),
    ("SECMASTER", "securities"),
)
WINDOW_BUSINESS_DAYS = 25
CellState = Literal["SUCCESS", "PARTIAL", "FAILED", "TIMEOUT", "NOT_RECEIVED"]
CELL_STATES: tuple[CellState, ...] = ("SUCCESS", "PARTIAL", "FAILED", "TIMEOUT", "NOT_RECEIVED")


class FeedHealthArguments(Contract):
    """Business dates, inclusive; each resolves as `as_of` does and must lie in the window."""

    date_from: date
    date_to: date
    known_as_of: KnowledgeCutoff | None = None

    @model_validator(mode="after")
    def ordered_range(self) -> Self:
        if self.date_from > self.date_to:
            raise ValueError("date_from must not follow date_to")
        return self


class FeedPair(Contract):
    source_system: SourceSystem
    feed: FeedName


class FeedHealthCell(Contract):
    """One expected pair on one business day; the latest run at the cutoff, or none."""

    source_system: SourceSystem
    feed: FeedName
    business_date: date
    state: CellState
    run_id: Identifier | None
    upstream_status: RunStatus | None
    accepted_records: int | None = Field(ge=0)
    rejected_records: int | None = Field(ge=0)
    recorded_at: AwareDatetime | None
    run_count: int = Field(ge=0)

    @model_validator(mode="after")
    def received_fields(self) -> Self:
        run = (self.run_id, self.upstream_status, self.accepted_records, self.rejected_records,
               self.recorded_at)
        if self.state == "NOT_RECEIVED":
            if self.run_count or any(value is not None for value in run):
                raise ValueError("a never-received cell carries no run")
        elif not self.run_count or any(value is None for value in run):
            raise ValueError("a received cell carries its latest run")
        return self


class FeedStateCount(Contract):
    state: CellState
    cell_count: int = Field(ge=0)


class FeedHealthData(Contract):
    """Every expected cell of the range, at most 15 pairs by 25 business days."""

    date_from: date
    date_to: date
    business_dates: list[date] = Field(min_length=1, max_length=WINDOW_BUSINESS_DAYS)
    business_day_count: int = Field(ge=1, le=WINDOW_BUSINESS_DAYS)
    pairs: list[FeedPair] = Field(max_length=len(EXPECTED_FEED_PAIRS))
    items: list[FeedHealthCell] = Field(
        max_length=len(EXPECTED_FEED_PAIRS) * WINDOW_BUSINESS_DAYS)
    item_count: int = Field(ge=0, le=len(EXPECTED_FEED_PAIRS) * WINDOW_BUSINESS_DAYS)
    total_item_count: int = Field(ge=0)
    ordering: str
    truncated: bool
    state_counts: list[FeedStateCount] = Field(
        min_length=len(CELL_STATES), max_length=len(CELL_STATES))

    @model_validator(mode="after")
    def counts_match(self) -> Self:
        if self.item_count != len(self.items) or self.total_item_count < self.item_count:
            raise ValueError("list counts must describe every expected cell")
        if self.truncated != (self.total_item_count > self.item_count):
            raise ValueError("truncation must match the full count")
        if self.business_day_count != len(self.business_dates):
            raise ValueError("the day count must describe the business dates returned")
        if [item.state for item in self.state_counts] != list(CELL_STATES):
            raise ValueError("one count per state, in the closed order")
        if sum(item.cell_count for item in self.state_counts) != self.total_item_count:
            raise ValueError("the state counts must describe every expected cell")
        return self


class FeedRunImpactArguments(Contract):
    run_id: Identifier
    known_as_of: KnowledgeCutoff | None = None


ImpactReason = Literal[
    "ACCOUNT_MANIFEST_MISMATCH", "SNAPSHOT_VALIDITY_MISMATCH", "DUPLICATE_ACCOUNT_COMPONENT",
    "INVALID_ACCOUNT_ROW", "EXPECTED_ACCOUNT_MISSING_FROM_MANIFEST", "OTHER",
]
HouseholdAttribution = Literal[
    "RESOLVED", "CROSSWALK_UNAVAILABLE", "HOUSEHOLD_UNAVAILABLE", "AMBIGUOUS",
]


class AffectedAccount(Contract):
    """An expected account whose publication from this run is unavailable; IDs only.

    `account_id` is the canonical account the external account resolves to through the
    CRM crosswalk at the run's business date and the cutoff; `manifest_account_id` is the
    label the manifest used (null when the manifest omitted the account).
    """

    account_id: Identifier | None
    manifest_account_id: Identifier | None
    reason: ImpactReason
    household_id: Identifier | None
    household_attribution: HouseholdAttribution

    @model_validator(mode="after")
    def attribution(self) -> Self:
        if (self.household_attribution == "RESOLVED") != (self.household_id is not None):
            raise ValueError("only a resolved account names its household")
        if (self.household_attribution == "CROSSWALK_UNAVAILABLE") != (self.account_id is None):
            raise ValueError("only an unresolved crosswalk leaves the account unnamed")
        if ((self.reason == "EXPECTED_ACCOUNT_MISSING_FROM_MANIFEST")
                != (self.manifest_account_id is None)):
            raise ValueError("only an account the manifest omitted has no manifest label")
        return self


class AffectedHousehold(Contract):
    household_id: Identifier


class FeedRunImpactData(Contract):
    """One run and, for a snapshot feed, the accounts and households it left unavailable."""

    run_id: Identifier
    source_system: SourceSystem
    feed: FeedName
    business_date: date
    status: RunStatus
    upstream_status: RunStatus
    recorded_at: AwareDatetime
    file_received: bool
    accepted_records: int = Field(ge=0)
    rejected_records: int = Field(ge=0)
    account_attribution: Literal["MANIFEST", "NOT_ATTRIBUTABLE"]
    expected_account_count: int | None = Field(ge=0)
    committed_account_count: int | None = Field(ge=0)
    affected_account_count: int | None = Field(ge=0)
    affected_household_count: int | None = Field(ge=0)
    unattributed_account_count: int | None = Field(ge=0)
    affected_accounts: SurfaceList[AffectedAccount] | None
    affected_households: SurfaceList[AffectedHousehold] | None

    @model_validator(mode="after")
    def attribution(self) -> Self:
        counts = (self.expected_account_count, self.committed_account_count,
                  self.affected_account_count, self.affected_household_count,
                  self.unattributed_account_count)
        lists = (self.affected_accounts, self.affected_households)
        if self.account_attribution == "NOT_ATTRIBUTABLE":
            if any(value is not None for value in (*counts, *lists)):
                raise ValueError("a feed without per-account availability has null counts")
            return self
        if any(value is None for value in (*counts, *lists)):
            raise ValueError("a snapshot run carries every count and list")
        assert self.affected_accounts is not None and self.affected_households is not None
        assert self.expected_account_count is not None and self.committed_account_count is not None
        assert self.affected_account_count is not None
        if (self.affected_accounts.total_item_count != self.affected_account_count
                or self.affected_households.total_item_count != self.affected_household_count
                or self.expected_account_count
                != self.committed_account_count + self.affected_account_count):
            raise ValueError("counts must describe the full sets before any cap")
        return self


SearchEnvelope = Envelope[SearchData]
ListHouseholdsEnvelope = Envelope[HouseholdListData]
QualityEnvelope = Envelope[QualityData]
FeedRunsEnvelope = Envelope[SurfaceList[FeedRunItem]]
ConflictEnvelope = Envelope[SurfaceList[ConflictItem]]
DecisionLogEnvelope = Envelope[SurfaceList[DecisionItem]]
EvalReportsEnvelope = Envelope[SurfaceList[EvalReportItem]]
TraceEnvelope = Envelope[TraceData]
DecisionLogPageEnvelope = Envelope[DecisionLogPageData]
HouseholdReceiptsEnvelope = Envelope[HouseholdReceiptsData]
TraceSpansEnvelope = Envelope[TraceSpansData]
FeedHealthEnvelope = Envelope[FeedHealthData]
FeedRunImpactEnvelope = Envelope[FeedRunImpactData]

SURFACE_OUTPUT_MODELS: dict[str, type[Contract]] = {
    "search_households": SearchEnvelope,
    "list_households": ListHouseholdsEnvelope,
    "get_data_quality_report": QualityEnvelope,
    "feed_runs": FeedRunsEnvelope,
    "conflicts": ConflictEnvelope,
    "decision_log": DecisionLogEnvelope,
    "eval_reports": EvalReportsEnvelope,
    "trace": TraceEnvelope,
    "decision_log_page": DecisionLogPageEnvelope,
    "household_receipts": HouseholdReceiptsEnvelope,
    "trace_spans": TraceSpansEnvelope,
    "feed_health": FeedHealthEnvelope,
    "feed_run_impact": FeedRunImpactEnvelope,
}

# The evaluation runner normalises recorded case arguments with this map. It names the
# surfaces an accepted case can call, so the workbench-only contracts of ADR 0009
# (`list_households`, `decision_log_page`, `household_receipts`, `trace_spans`,
# `feed_health`, `feed_run_impact`: no MCP tool, no case) are deliberately absent. Their
# argument models are named at their routes.
SURFACE_ARGUMENT_MODELS: dict[str, type[Contract]] = {
    "search_households": SearchArguments,
    "get_data_quality_report": QualityArguments,
    "feed_runs": FeedRunsArguments,
    "conflicts": ConflictArguments,
    "decision_log": EmptyArguments,
    "eval_reports": EmptyArguments,
    "trace": TraceArguments,
}
