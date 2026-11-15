"""Small loopback-demo session boundary; bearer APIs never authenticate from cookies."""

import secrets
from collections import OrderedDict
from collections.abc import Callable, Iterable
from dataclasses import dataclass, field
from datetime import UTC, date, datetime, timedelta
from pathlib import Path
from typing import Annotated, Literal, Self
from urllib.parse import urlsplit

import yaml
from fastapi import Request
from pydantic import Field, StringConstraints, field_validator, model_validator

from wealthcontext.contracts import Contract, Identifier, KnowledgeCutoff
from wealthcontext.feeds import FeedName, SourceSystem
from wealthcontext.identity import PERSONAS, Identity, Principal, Unauthenticated
from wealthcontext.provider_contracts import ProviderName

COOKIE = "wc_demo_session"
_MATRIX = Path(__file__).resolve().parents[2] / "policies/matrix.yaml"
EVIDENCE_PAIR_LIMIT = 256


@dataclass(frozen=True)
class MenuItem:
    path: str
    label: str
    group: str
    roles: frozenset[str]


def load_matrix(path: Path = _MATRIX) -> tuple[frozenset[str], frozenset[str],
                                               tuple[MenuItem, ...]]:
    """Derive the workbench path sets and role menu from the permission matrix.

    The matrix is the one list of routes. A malformed menu row stops the app at import
    rather than widening a menu: the menu never grants access, policy does.
    """
    spec = yaml.safe_load(path.read_text())
    rows = [row for row in spec["routes"] if str(row["path"]).startswith("/workbench")]
    public = frozenset(row["path"] for row in rows if row["authentication"] == "public")
    required = frozenset(row["path"] for row in rows if row["authentication"] == "required")
    if public & required or len(public) + len(required) != len({row["path"] for row in rows}):
        raise ValueError("each workbench path needs exactly one authentication mode")
    menu = []
    for row in rows:
        entry = row.get("menu")
        if entry is None:
            continue
        roles = frozenset(entry["roles"])
        allowed = set(spec["actions"].get(row["action"], {}).get("roles", {}))
        if (row["method"] != "GET" or row["authentication"] != "required" or not roles
                or set(entry) != {"label", "group", "roles"} or not roles <= allowed):
            raise ValueError(f"invalid menu row for {row['path']}")
        menu.append(MenuItem(row["path"], str(entry["label"]), str(entry["group"]), roles))
    return public, required, tuple(menu)


PUBLIC_WORKBENCH_PATHS, AUTH_WORKBENCH_PATHS, MENU = load_matrix()


def action_roles(action: str, path: Path = _MATRIX) -> frozenset[str]:
    """The roles the matrix allows an action. A page may offer a link only to them; the
    link never grants access, policy does."""
    spec = yaml.safe_load(path.read_text())
    return frozenset(spec["actions"][action]["roles"])


def menu_for(role: str | None) -> list[MenuItem]:
    return [item for item in MENU if role in item.roles]


CsrfToken = Annotated[str, StringConstraints(min_length=32, max_length=128,
                                            pattern=r"^[A-Za-z0-9_-]+$")]


class WorkbenchContext(Contract):
    persona: Identifier = "ADV-01"
    as_of: date = date(2026, 9, 30)
    known_as_of: KnowledgeCutoff = datetime(2026, 10, 31, 16, tzinfo=UTC)
    mode: Literal["replay", "live"] = "replay"
    provider: ProviderName = "openai"
    household_id: Identifier = "WC-HH-0001"
    preset: Literal["clean", "reconciliation", "missing", "access", "late_trade",
                    "failed_feed"] = "clean"

    @field_validator("persona")
    @classmethod
    def known_persona(cls, value: str) -> str:
        if value not in PERSONAS:
            raise ValueError("known synthetic persona required")
        return value


class SessionForm(WorkbenchContext):
    csrf: CsrfToken
    apply_preset: bool = False


class CsrfForm(Contract):
    csrf: CsrfToken


class QuestionForm(Contract):
    csrf: CsrfToken
    question: str = Field(min_length=1, max_length=12000, pattern=r"\S")


class ClaimSelection(Contract):
    answer: Identifier
    claim: Identifier
    reference: Identifier | None = None


class EvidenceSelection(Contract):
    result: Identifier
    source: Identifier


class HouseholdSelection(Contract):
    """A household page's query: its shape is checked, nothing is looked up. Without an
    ID the page shows the session context's household."""

    household_id: Identifier | None = None


ThresholdQuery = Annotated[str, StringConstraints(max_length=6,
                                                  pattern=r"^[0-9]{1,3}(\.[0-9]{1,2})?$")]


class BookQuery(Contract):
    """The book's optional concentration threshold; the platform validates its range."""

    threshold: ThresholdQuery | None = None


MetricId = Annotated[str, StringConstraints(max_length=64,
                                            pattern=r"^[A-Z][A-Z0-9_]*@[0-9]{1,4}$")]
SchemaId = Annotated[str, StringConstraints(max_length=64, pattern=r"^[a-z][a-z0-9-]*$")]


class MetricQuery(Contract):
    """E1's optional metric: its shape is checked, nothing is looked up. The catalog's own
    ID does not fit the shape, so the page asks for the catalog only through its registry."""

    metric_id: MetricId | None = None


class SchemaQuery(Contract):
    """E2's optional schema, by its file stem: shape only, looked up after the policy allows."""

    schema_id: SchemaId | None = None


def _blank(value: object) -> object:
    """An empty field of a GET form is an absent filter."""
    return None if value == "" else value


def _utc(value: object) -> object:
    """A filter instant from a datetime-local field is read as UTC, as the form says."""
    if isinstance(value, str) and value:
        try:
            parsed = datetime.fromisoformat(value)
        except ValueError:
            return value
        return parsed.replace(tzinfo=UTC) if parsed.tzinfo is None else parsed
    return value


DecisionCursorQuery = Annotated[str, StringConstraints(
    max_length=1024, pattern=r"^[A-Za-z0-9_-]+\.[A-Za-z0-9_-]+$")]


class DecisionQuery(Contract):
    """C1's filters and cursor: shape only. The cursor is decoded by the platform, after
    the policy allows; an empty field is no filter."""

    principal_id: Identifier | None = None
    action: Identifier | None = None
    outcome: Literal["allow", "deny"] | None = None
    recorded_from: KnowledgeCutoff | None = None
    recorded_before: KnowledgeCutoff | None = None
    household_id: Identifier | None = None
    cursor: DecisionCursorQuery | None = None

    @field_validator("*", mode="before")
    @classmethod
    def blank_is_absent(cls, value: object) -> object:
        return _blank(value)

    @field_validator("recorded_from", "recorded_before", mode="before")
    @classmethod
    def instant_in_utc(cls, value: object) -> object:
        return _utc(value)

    def filters(self) -> dict[str, str]:
        """The filters as the page's own query, without the cursor."""
        return {key: value.isoformat().replace("+00:00", "Z") if isinstance(value, datetime)
                else str(value)
                for key, value in self.model_dump(exclude={"cursor"}).items()
                if value is not None}


class PointInTimeQuery(Contract):
    """C2's household and its comparison cutoff: a page parameter, never session context
    (ADR 0009 section 6). Shape only; the platform bounds the cutoff."""

    household_id: Identifier | None = None
    compare_known_as_of: KnowledgeCutoff | None = None

    @field_validator("*", mode="before")
    @classmethod
    def blank_is_absent(cls, value: object) -> object:
        return _utc(_blank(value))


class TraceQuery(Contract):
    """C3's trace: shape only. The platform normalises it after the policy allows."""

    trace_id: Identifier | None = None

    @field_validator("trace_id", mode="before")
    @classmethod
    def blank_is_absent(cls, value: object) -> object:
        return _blank(value)


class DataHealthQuery(Contract):
    """O1's range and view: shape only. The platform resolves and bounds the dates."""

    date_from: date | None = None
    date_to: date | None = None
    view: Literal["grid", "table"] = "grid"

    @field_validator("date_from", "date_to", mode="before")
    @classmethod
    def blank_is_absent(cls, value: object) -> object:
        return _blank(value)


class FeedRunQuery(Contract):
    """O2's run, or the O1 cell that names it (ADR 0009 "Presets": the operations preset
    names a cell, and the server takes the run ID from the authorized `feed_health` cell).
    Shape only; nothing here is looked up."""

    run_id: Identifier | None = None
    source_system: SourceSystem | None = None
    feed: FeedName | None = None
    business_date: date | None = None

    @field_validator("*", mode="before")
    @classmethod
    def blank_is_absent(cls, value: object) -> object:
        return _blank(value)

    @model_validator(mode="after")
    def run_or_cell(self) -> Self:
        cell = (self.source_system, self.feed, self.business_date)
        if any(value is not None for value in cell) and (
                self.run_id is not None or any(value is None for value in cell)):
            raise ValueError("name a run, or a whole cell, not both")
        return self

    def cell(self) -> dict[str, str] | None:
        if self.business_date is None:
            return None
        return {"source_system": str(self.source_system), "feed": str(self.feed),
                "business_date": self.business_date.isoformat()}


@dataclass
class DemoSession:
    id: str
    csrf: str
    expires: datetime
    context: WorkbenchContext = field(default_factory=WorkbenchContext)
    token: str | None = None
    conversation_id: str | None = None
    run_metadata: dict = field(default_factory=dict)
    busy: bool = False
    # (result ID, provenance handle) pairs issued to data pages in this session, each
    # with the path of the page that showed it: identifiers only, never a payload
    # (ADR 0009 section 2).
    evidence: OrderedDict[tuple[str, str], str] = field(default_factory=OrderedDict)

    def issue(self, result_id: str, handles: Iterable[str], page: str) -> None:
        for handle in handles:
            self.evidence.pop((result_id, handle), None)
            self.evidence[(result_id, handle)] = page
        while len(self.evidence) > EVIDENCE_PAIR_LIMIT:
            self.evidence.popitem(last=False)

    def holds(self, result_id: str, handle: str) -> bool:
        return (result_id, handle) in self.evidence


class DemoSessions:
    """Only identity/context/result IDs are retained; financial payloads are never cached.

    `on_drop` runs for every session that leaves the table: replaced, expired or evicted.
    The A7 page uses it to delete that session's seeded store.
    """

    def __init__(self, on_drop: Callable[[DemoSession], None] | None = None):
        self._sessions: dict[str, DemoSession] = {}
        self._on_drop = on_drop

    def _drop(self, session_id: str) -> None:
        session = self._sessions.pop(session_id, None)
        if session is not None and self._on_drop is not None:
            self._on_drop(session)

    def get(self, session_id: str | None, now: datetime) -> DemoSession | None:
        for key in list(self._sessions):
            if self._sessions[key].expires <= now:
                self._drop(key)
        return self._sessions.get(session_id or "")

    def create(self, now: datetime, context: WorkbenchContext | None = None,
               token: str | None = None) -> DemoSession:
        self.get(None, now)
        while len(self._sessions) >= 1024:
            self._drop(next(iter(self._sessions)))
        session = DemoSession(secrets.token_urlsafe(32), secrets.token_urlsafe(32),
                              now + timedelta(hours=1), context or WorkbenchContext(), token)
        self._sessions[session.id] = session
        return session

    def remove(self, session: DemoSession) -> None:
        if self._sessions.get(session.id) is session:
            self._drop(session.id)

    def authenticate(self, request: Request, identity: Identity, now: datetime) -> tuple[
        DemoSession, Principal,
    ]:
        if not local_request(request):
            raise Unauthenticated("Local demo origin required")
        session = self.get(request.cookies.get(COOKIE), now)
        if session is None or session.token is None:
            raise Unauthenticated("Select a local demo persona")
        return session, identity.authenticate("Bearer " + session.token, now)


def local_request(request: Request) -> bool:
    """Browser loopback boundary; Compose additionally binds the published port to 127.0.0.1."""
    host = request.headers.get("host", "")
    # Validate the actual authority, not Request.url's normalized server fallback.
    if not host or any(ord(character) <= 32 or ord(character) >= 127 for character in host):
        return False
    try:
        parsed = urlsplit("//" + host)
        port = parsed.port  # Access validates numeric/range constraints.
        return (parsed.netloc == host and not parsed.path and not parsed.query
                and not parsed.fragment and parsed.username is None and parsed.password is None
                and parsed.hostname in {"localhost", "127.0.0.1", "::1"}
                and not host.endswith(":") and (port is None or port > 0))
    except ValueError:
        return False


def same_origin(request: Request) -> bool:
    if not local_request(request) or request.headers.get("sec-fetch-site") == "cross-site":
        return False
    try:
        origin = urlsplit(request.headers.get("origin", ""))
    except ValueError:
        return False
    return (bool(origin.netloc) and origin.scheme == request.url.scheme
            and origin.netloc.lower() == request.headers.get("host", "").lower()
            and not origin.path and not origin.query and not origin.fragment)


def valid_csrf(request: Request, session: DemoSession, token: str) -> bool:
    return token.isascii() and same_origin(request) and secrets.compare_digest(session.csrf, token)
