"""Governed search, quality, inspector, compliance and operations reads in the read snapshot."""

import base64
import binascii
import hashlib
import hmac
import json
import sqlite3
from dataclasses import dataclass
from datetime import UTC, date, datetime, timedelta
from decimal import Decimal
from pathlib import Path
from typing import TYPE_CHECKING, Literal, cast, get_args
from zoneinfo import ZoneInfo

from wealthcontext.clock import utc_timestamp
from wealthcontext.contracts import Contract, QualityFlag
from wealthcontext.evaluation_store import evaluation_summaries
from wealthcontext.facts import FactView, _crosswalk, active_household_ids, fact_view
from wealthcontext.feeds import SNAPSHOT_FEEDS
from wealthcontext.households import HouseholdContext, household_context, household_views
from wealthcontext.projections import Projection, project_no_pii
from wealthcontext.snapshot import CompleteSnapshot, calculate_snapshot, valuation_date
from wealthcontext.source_projection import OriginAccount
from wealthcontext.storage import canonical, digest
from wealthcontext.surface_contracts import (
    CELL_STATES,
    EXPECTED_FEED_PAIRS,
    AffectedAccount,
    AffectedHousehold,
    ConflictArguments,
    ConflictItem,
    DecisionItem,
    DecisionLogPageArguments,
    DecisionLogPageData,
    DecisionPageItem,
    EmptyArguments,
    EntitlementFactItem,
    FeedHealthArguments,
    FeedHealthCell,
    FeedHealthData,
    FeedPair,
    FeedReject,
    FeedRunImpactArguments,
    FeedRunImpactData,
    FeedRunItem,
    FeedRunsArguments,
    FeedStateCount,
    HouseholdAttribution,
    HouseholdListData,
    HouseholdQuality,
    HouseholdReceiptsArguments,
    HouseholdReceiptsData,
    ImpactReason,
    QualityArguments,
    QualityData,
    ReceiptItem,
    RevisionFacts,
    SearchArguments,
    SearchData,
    SearchItem,
    SpanItem,
    SpanName,
    SurfaceList,
    TraceArguments,
    TraceData,
    TraceResultItem,
    TraceSpansData,
)
from wealthcontext.valuation import resolve_received_valuation

if TYPE_CHECKING:
    from wealthcontext.context_service import ContextService, Operation


def _listed[Item](items: list[Item], ordering: str) -> SurfaceList[Item]:
    return SurfaceList(items=items[:20], item_count=min(20, len(items)),
                       total_item_count=len(items), ordering=ordering, truncated=len(items) > 20)


def _ids(connection: sqlite3.Connection, day: date, known: datetime) -> list[str]:
    """Identifier-only inventory, called only after a global/preflight allow."""
    return active_household_ids(connection, day, known)


def _scope(operation: "Operation", household_id: str, context: HouseholdContext) -> None:
    assert operation.account_scope is not None
    accounts = [item.account for item in context.accounts] + context.excluded_accounts
    operation.account_scope.extend(OriginAccount(
        household_id=household_id, account_id=item["id"], custodian=item["custodian"],
        account_number=item["account_number"],
    ) for item in accounts)


def _flags(flags: list[QualityFlag], projection: Projection = "full") -> list[QualityFlag]:
    if projection == "no_pii":
        flags = [QualityFlag.model_validate({**flag.model_dump(mode="json"),
                                            "detail": project_no_pii(flag.detail)})
                 for flag in flags]
    unique = {canonical(flag.model_dump(mode="json")): flag for flag in flags}
    return sorted(unique.values(),
                  key=lambda item: (item.subject, item.code, canonical(item.detail)))


def _response(
    data: Contract, flags: list[QualityFlag] | None = None, status: str = "COMPLETE",
) -> tuple[dict, list[QualityFlag], list[str], str]:
    return data.model_dump(mode="json"), flags or [], [], status


async def _search(
    service: "ContextService", operation: "Operation", connection: sqlite3.Connection,
    arguments: dict | None,
) -> tuple[dict, list[QualityFlag], list[str], str]:
    args = SearchArguments.model_validate(arguments)
    items, flags = await _entitled_households(service, operation, connection, args.name_query)
    listed = _listed(items, "household_name.casefold ASC, household_id ASC")
    data = SearchData(**listed.model_dump(), evaluated_household_count=len(operation.scopes))
    return _response(data, flags, "INCOMPLETE" if flags else "COMPLETE")


async def _list_households(
    service: "ContextService", operation: "Operation", connection: sqlite3.Connection,
    arguments: dict | None,
) -> tuple[dict, list[QualityFlag], list[str], str]:
    """ADR 0009 section 1: search without the name predicate, for the workbench only."""
    EmptyArguments.model_validate(arguments)
    items, flags = await _entitled_households(service, operation, connection, None)
    listed = _listed(items, "household_name.casefold ASC, household_id ASC")
    data = HouseholdListData(**listed.model_dump(),
                             evaluated_household_count=len(operation.scopes))
    return _response(data, flags, "INCOMPLETE" if flags else "COMPLETE")


async def _entitled_households(
    service: "ContextService", operation: "Operation", connection: sqlite3.Connection,
    name_query: str | None,
) -> tuple[list[SearchItem], list[QualityFlag]]:
    """Preflight, then filter the identifier-only inventory; names for allowed IDs only.

    Records every authorized household as the scope (not only the returned 20), an empty
    account scope and the household records read, under the `search_households` action.
    """
    await service.authorize(operation, connection, "search_preflight", [])
    day = operation.now.astimezone(ZoneInfo("America/New_York")).date()
    candidates = _ids(connection, day, operation.now)
    operation.scopes = await service.authorize(
        operation, connection, "search_households", candidates, filtered=True,
    )
    operation.origin_action = "search_households"
    operation.account_scope = []
    # Search reads current membership: its counts carry that business day and the request
    # instant, so summary claims can cite them (SOL-P5-R1-SERVICE-001).
    operation.day, operation.known = day, operation.now
    # Only the authorized IDs can become names, matches, counts or provenance.
    households = fact_view(connection, "household", day, operation.now,
                           entity_ids=operation.scopes)
    items: list[SearchItem] = []
    flags: list[QualityFlag] = []
    sources: set[str] = set()
    for household_id in operation.scopes:
        household = households.values.get(household_id)
        if household is not None:
            sources.add(household["source_id"])
            if name_query is None or name_query.casefold() in household["name"].casefold():
                items.append(SearchItem(household_id=household_id,
                                        household_name=household["name"]))
        elif household_id in households.ambiguous:
            flags.append(QualityFlag(code="AMBIGUOUS_ENTITY", severity="warning",
                                     subject=household_id, detail={"entity_type": "household"}))
            sources.update(row["source_id"] for row in households.ambiguous[household_id])
    operation.sources = sorted(sources)
    items.sort(key=lambda item: (item.household_name.casefold(), item.household_id))
    return items, flags


async def _quality(
    service: "ContextService", operation: "Operation", connection: sqlite3.Connection,
    arguments: dict | None,
) -> tuple[dict, list[QualityFlag], list[str], str]:
    args = QualityArguments.model_validate(arguments)
    await service.authorize(operation, connection, "quality_preflight", [])
    service.resolve_cutoffs(operation, args.as_of, args.known_as_of)
    assert operation.day is not None and operation.known is not None
    scopes = _ids(connection, operation.day, operation.known)
    await service.authorize(operation, connection, "view_quality", scopes)
    operation.scopes = scopes
    operation.origin_action = "view_quality"
    operation.account_scope = []
    items, all_flags, sources = [], [], set()
    incomplete = issues = 0
    for household_id in operation.scopes:
        received = resolve_received_valuation(connection, household_id, operation.day,
                                              operation.known, absent_as_incomplete=True)
        _scope(operation, household_id, received.context)
        flags = _flags(list(received.quality_flags), operation.projection)
        all_flags.extend(flags)
        sources.update(received.valuation.source_ids)
        incomplete += not received.valuation.complete
        issues += bool(flags)
        items.append(HouseholdQuality(
            household_id=household_id, complete=received.valuation.complete,
            flags=_listed(flags, "subject ASC, code ASC, detail ASC"),
            missing_component_count=len(received.valuation.missing_components),
            warning_count=sum(flag.severity == "warning" for flag in flags),
            info_count=sum(flag.severity == "info" for flag in flags),
        ))
    operation.sources = sorted(sources)
    data = QualityData(**_listed(items, "household_id ASC").model_dump(),
                       evaluated_household_count=len(operation.scopes),
                       households_with_issues_count=issues, incomplete_household_count=incomplete,
                       quality_flag_count=len(all_flags))
    return _response(data, _flags(all_flags), "INCOMPLETE" if incomplete else "COMPLETE")


async def _feeds(
    service: "ContextService", operation: "Operation", connection: sqlite3.Connection,
    arguments: dict | None,
) -> tuple[dict, list[QualityFlag], list[str], str]:
    args = FeedRunsArguments.model_validate(arguments)
    await service.authorize(operation, connection, "feed_runs", [])
    operation.origin_action = "feed_runs"
    service.resolve_cutoffs(operation, args.as_of, args.known_as_of)
    assert operation.day is not None and operation.known is not None
    scopes = _ids(connection, operation.day, operation.known)
    await service.authorize(operation, connection, "feed_runs", scopes)
    operation.scopes = scopes
    operation.account_scope = []
    for household_id in operation.scopes:
        _scope(operation, household_id, household_context(
            connection, household_id, operation.day, operation.known,
        ))
    rows = connection.execute(
        "SELECT id,source_system,feed,business_date,status,upstream_status,recorded_at,imported_at,"
        "accepted_records,rejected_records,content_hash,receipt_source_id FROM feed_runs "
        "WHERE business_date=? AND recorded_at<=? ORDER BY recorded_at DESC,id ASC",
        (operation.day.isoformat(), utc_timestamp(operation.known)),
    ).fetchall()
    operation.sources = sorted({row["receipt_source_id"] for row in rows})
    items = []
    for row in rows:
        # Received reject bytes may be malformed or contain names in arbitrary keys.
        # No role gets the raw value, nor an upstream account label masquerading as scope.
        rejects = [FeedReject(id=item["id"], reason=item["reason"]) for item in connection.execute(
            "SELECT id,reason FROM quarantine WHERE run_id=? ORDER BY id", (row["id"],),
        )]
        values = {key: row[key] for key in row.keys() if key != "receipt_source_id"}
        items.append(FeedRunItem(**values, rejects=_listed(rejects, "id ASC")))
    return _response(_listed(items, "recorded_at DESC, id ASC"))


async def _conflicts(
    service: "ContextService", operation: "Operation", connection: sqlite3.Connection,
    arguments: dict | None,
) -> tuple[dict, list[QualityFlag], list[str], str]:
    args = ConflictArguments.model_validate(arguments)
    await service.authorize(operation, connection, "view_conflicts", [args.household_id])
    operation.origin_action = "view_conflicts"
    operation.scopes = [args.household_id]
    service.resolve_cutoffs(operation, args.as_of, args.known_as_of)
    assert operation.day is not None and operation.known is not None
    context = household_context(connection, args.household_id, operation.day, operation.known)
    if context.household is None and not context.missing_components:
        raise LookupError("Household unavailable at these cutoffs")
    operation.account_scope = []
    _scope(operation, args.household_id, context)
    operation.sources = context.source_ids
    # Canonical conflict derivation already redacts unentitled counterpart IDs.
    items = [ConflictItem(**{key: item[key] for key in (
        "code", "subject", "current_household_id", "detail",
    )}) for item in context.conflicts]
    if operation.projection == "no_pii":
        items = [ConflictItem.model_validate({**item.model_dump(mode="json"),
                                             "detail": project_no_pii(item.detail)})
                 for item in items]
    items = list({canonical(item.model_dump(mode="json")): item for item in items}.values())
    items.sort(key=lambda item: (item.subject, item.code, canonical(item.detail)))
    return _response(_listed(items, "subject ASC, code ASC, detail ASC"),
                     _flags(context.quality_flags, operation.projection),
                     "INCOMPLETE" if context.missing_components else "COMPLETE")


def _decision(row: sqlite3.Row) -> DecisionItem:
    values = dict(row)
    values["resources"] = json.loads(row["resources"])
    values["allowed"] = bool(row["allowed"])
    return DecisionItem.model_validate(values)


async def _decisions(
    service: "ContextService", operation: "Operation", connection: sqlite3.Connection,
    arguments: dict | None,
) -> tuple[dict, list[QualityFlag], list[str], str]:
    EmptyArguments.model_validate(arguments)
    await service.authorize(operation, connection, "view_decision_log", [])
    operation.origin_action = "view_decision_log"
    rows = connection.execute("SELECT * FROM decisions WHERE recorded_at<=? "
                              "ORDER BY recorded_at DESC,id ASC", (utc_timestamp(operation.now),))
    items = [_decision(row) for row in rows]
    operation.scopes = sorted({resource for item in items for resource in item.resources})
    operation.account_scope = []
    return _response(_listed(items, "recorded_at DESC, id ASC"))


async def _trace(
    service: "ContextService", operation: "Operation", connection: sqlite3.Connection,
    arguments: dict | None,
) -> tuple[dict, list[QualityFlag], list[str], str]:
    args = TraceArguments.model_validate(arguments)
    await service.authorize(operation, connection, "trace_preflight", [])
    owners = {row[0] for row in connection.execute(
        "SELECT principal_id FROM results WHERE trace_id=? AND recorded_at<=? UNION "
        "SELECT principal_id FROM decisions WHERE trace_id=? AND recorded_at<=?",
        (args.trace_id, utc_timestamp(operation.now), args.trace_id, utc_timestamp(operation.now)),
    )}
    if not owners or (operation.principal.role != "compliance"
                      and owners != {operation.principal.id}):
        service.deny_locally(operation, "TRACE_OWNER_MISMATCH")
    result_ids = [row[0] for row in connection.execute(
        "SELECT id FROM results WHERE trace_id=? AND recorded_at<=? ORDER BY id",
        (args.trace_id, utc_timestamp(operation.now)),
    )]
    if not result_ids:
        raise sqlite3.DatabaseError("Trace result association missing")
    scopes = sorted({household for result_id in result_ids
                     for household in service.result_scopes(connection, result_id)})
    await service.authorize(operation, connection, "trace_scope", scopes)
    operation.scopes = scopes
    operation.origin_action = "trace_scope"
    operation.account_scope = []
    results = []
    for row in connection.execute(
        "SELECT id,payload,payload_hash,recorded_at FROM results WHERE trace_id=? "
        "AND recorded_at<=? ORDER BY recorded_at DESC,id ASC",
        (args.trace_id, utc_timestamp(operation.now)),
    ):
        if digest(row["payload"]) != row["payload_hash"]:
            raise sqlite3.DatabaseError("Trace result hash mismatch")
        envelope = json.loads(row["payload"])
        if envelope.get("trace_id") != args.trace_id or envelope.get("result_id") != row["id"]:
            raise sqlite3.DatabaseError("Trace result association mismatch")
        results.append(TraceResultItem(result_id=row["id"], status=envelope["status"],
                                       payload_hash=row["payload_hash"],
                                       recorded_at=row["recorded_at"]))
    decisions = [_decision(row) for row in connection.execute(
        "SELECT * FROM decisions WHERE trace_id=? AND recorded_at<=? "
        "ORDER BY recorded_at DESC,id ASC", (args.trace_id, utc_timestamp(operation.now)),
    )]
    # A denied decision can mention an attempted resource outside the stored result
    # scope. A trace grant never makes that attempted resource an authorized one.
    if operation.principal.role != "compliance":
        for decision in decisions:
            decision.resources = [item for item in decision.resources if item in operation.scopes]
    return _response(TraceData(
        trace_id=args.trace_id, principal_id=next(iter(owners)) if len(owners) == 1 else None,
        results=_listed(results, "recorded_at DESC, result_id ASC"),
        decisions=_listed(decisions, "recorded_at DESC, id ASC"),
    ))

# --- ADR 0009 sections 5 to 7: the compliance contracts (C1, C2, C3) ------------------------
#
# Each handler takes its preflight decision on the empty scope before it decodes a cursor,
# resolves an identifier or opens the span export, and records its result as the surfaces
# above record theirs.

Response = tuple[dict, list[QualityFlag], list[str], str]

PAGE_SIZE = 20
FACT_LIMIT = 100
SPAN_LIMIT = 200
SCAN_BYTES = 32 * 1024 * 1024
# A filtered allow lists every candidate in `decisions.resources`; its authorized
# households are in `decision_authorized_resources` (ADR 0009 section 5).
FILTERED_ACTIONS = ("concentration_book", "search_households")
SPAN_NAMES = frozenset(name for name in get_args(SpanName) if name != "other")
STATUS_CODES = frozenset({"UNSET", "OK", "ERROR"})


def _invalid(message: str) -> Exception:
    from wealthcontext.context_service import RequestFailure

    return RequestFailure("INVALID_ARGUMENT", message)


# --- C1: the decision log, paginated (ADR 0009 section 5) --------------------------------


def _b64(data: bytes) -> str:
    return base64.urlsafe_b64encode(data).rstrip(b"=").decode()


def _unb64(text: str) -> bytes:
    return base64.urlsafe_b64decode(text + "=" * (-len(text) % 4))


@dataclass(frozen=True)
class _Position:
    principal_id: str
    watermark: int
    filters: str
    started_at: str
    recorded_at: str
    decision_id: str


def seal_cursor(key: bytes, position: _Position) -> str:
    body = _b64(canonical({
        "v": 1, "p": position.principal_id, "w": position.watermark, "f": position.filters,
        "s": position.started_at, "t": position.recorded_at, "i": position.decision_id,
    }).encode())
    return body + "." + _b64(hmac.new(key, body.encode(), hashlib.sha256).digest())


def open_cursor(key: bytes, token: str) -> _Position | None:
    """A forged, malformed or other-key cursor is None; nothing in it is trusted before."""
    body, _, tag = token.partition(".")
    try:
        expected = hmac.new(key, body.encode(), hashlib.sha256).digest()
        if not hmac.compare_digest(expected, _unb64(tag)):
            return None
        values = json.loads(_unb64(body))
    except (binascii.Error, ValueError, UnicodeError):
        return None
    if not isinstance(values, dict) or values.get("v") != 1:
        return None
    fields = ("p", "w", "f", "s", "t", "i")
    if not all(name in values for name in fields) or not isinstance(values["w"], int):
        return None
    if not all(isinstance(values[name], str) for name in ("p", "f", "s", "t", "i")):
        return None
    return _Position(values["p"], values["w"], values["f"], values["s"], values["t"],
                     values["i"])


def _filters(args: DecisionLogPageArguments) -> dict:
    return {
        "principal_id": args.principal_id, "action": args.action, "outcome": args.outcome,
        "recorded_from": utc_timestamp(args.recorded_from) if args.recorded_from else None,
        "recorded_before": utc_timestamp(args.recorded_before) if args.recorded_before else None,
        "household_id": args.household_id,
    }


def _where(filters: dict, watermark: int) -> tuple[str, list]:
    clauses, params = ["d.rowid<=?"], [watermark]
    if filters["principal_id"] is not None:
        clauses.append("d.principal_id=?")
        params.append(filters["principal_id"])
    if filters["action"] is not None:
        clauses.append("d.action=?")
        params.append(filters["action"])
    if filters["outcome"] is not None:
        clauses.append("d.allowed=?")
        params.append(int(filters["outcome"] == "allow"))
    if filters["recorded_from"] is not None:
        clauses.append("d.recorded_at>=?")
        params.append(filters["recorded_from"])
    if filters["recorded_before"] is not None:
        clauses.append("d.recorded_at<?")
        params.append(filters["recorded_before"])
    if filters["household_id"] is not None:
        filtered_allow = f"(d.allowed=1 AND d.action IN {FILTERED_ACTIONS!r})"
        clauses.append(
            f"(({filtered_allow} AND EXISTS (SELECT 1 FROM decision_authorized_resources a "
            "WHERE a.decision_id=d.id AND a.household_id=?)) OR "
            f"(NOT {filtered_allow} AND EXISTS (SELECT 1 FROM json_each(d.resources) r "
            "WHERE r.value=?)))"
        )
        params.extend([filters["household_id"], filters["household_id"]])
    return " AND ".join(clauses), params


def _revision(connection: sqlite3.Connection, revision: int) -> RevisionFacts:
    stored = connection.execute(
        "SELECT recorded_at FROM entitlement_revisions WHERE revision=?", (revision,),
    ).fetchone()
    if stored is None:
        raise sqlite3.DatabaseError("Entitlement revision missing")
    rows = connection.execute(
        "SELECT kind,subject,object,valid_from,valid_to FROM entitlement_facts WHERE revision=? "
        "ORDER BY kind,subject,object,valid_from", (revision,),
    ).fetchall()
    facts = [EntitlementFactItem.model_validate(dict(row)) for row in rows[:FACT_LIMIT]]
    return RevisionFacts(
        revision=revision, recorded_at=stored["recorded_at"], facts=facts,
        item_count=len(facts), total_item_count=len(rows),
        ordering="kind ASC, subject ASC, object ASC, valid_from ASC",
        truncated=len(rows) > len(facts),
    )


async def decision_log_page(
    service: "ContextService", operation: "Operation", connection: sqlite3.Connection,
    arguments: dict | None,
) -> Response:
    args = DecisionLogPageArguments.model_validate(arguments)
    await service.authorize(operation, connection, "view_decision_log", [])
    operation.origin_action = "view_decision_log"
    operation.account_scope = []
    filters = _filters(args)
    filter_digest = digest(canonical(filters))
    after: tuple[str, str] | None = None
    if args.cursor is None:
        # The commit position, read inside this read snapshot: rows are only appended, so
        # a decision committed later, this browse's own included, has a higher rowid.
        watermark = connection.execute(
            "SELECT COALESCE(MAX(rowid),0) FROM decisions").fetchone()[0]
        started_at = utc_timestamp(operation.now)
    else:
        position = open_cursor(service.cursor_key, args.cursor)
        if (position is None or position.principal_id != operation.principal.id
                or position.filters != filter_digest):
            raise _invalid("Invalid decision log cursor")
        watermark, started_at = position.watermark, position.started_at
        after = (position.recorded_at, position.decision_id)
    where, params = _where(filters, watermark)
    matching = connection.execute(
        f"SELECT d.resources FROM decisions d WHERE {where}", params).fetchall()
    # Every household any matching decision names was evaluated for the count, so all of
    # them are the recorded scope, as `decision_log` records its own (ADR 0007).
    operation.scopes = sorted({resource for row in matching
                               for resource in json.loads(row["resources"])})
    page_where, page_params = where, list(params)
    if after is not None:
        page_where += " AND (d.recorded_at<? OR (d.recorded_at=? AND d.id>?))"
        page_params.extend([after[0], after[0], after[1]])
    rows = connection.execute(
        "SELECT d.id,d.principal_id,d.action,d.resources,d.allowed,d.reason,d.policy_revision,"
        f"d.entitlement_revision,d.trace_id,d.recorded_at FROM decisions d WHERE {page_where} "
        f"ORDER BY d.recorded_at DESC,d.id ASC LIMIT {PAGE_SIZE + 1}", page_params,
    ).fetchall()
    items = []
    for row in rows[:PAGE_SIZE]:
        values = dict(row)
        values["resources"] = json.loads(row["resources"])
        values["allowed"] = bool(row["allowed"])
        values["authorized_resources"] = [item[0] for item in connection.execute(
            "SELECT household_id FROM decision_authorized_resources WHERE decision_id=? "
            "ORDER BY household_id", (row["id"],),
        )] if row["allowed"] else None
        items.append(DecisionPageItem.model_validate(values))
    next_cursor = None
    if len(rows) > PAGE_SIZE:
        last = rows[PAGE_SIZE - 1]
        next_cursor = seal_cursor(service.cursor_key, _Position(
            operation.principal.id, watermark, filter_digest, started_at,
            last["recorded_at"], last["id"],
        ))
    revisions = sorted({item.entitlement_revision for item in items
                        if item.entitlement_revision is not None}, reverse=True)
    data = DecisionLogPageData(
        items=items, item_count=len(items), total_item_count=len(matching),
        ordering="recorded_at DESC, id ASC", truncated=len(matching) > len(items),
        next_cursor=next_cursor, browse_started_at=datetime.fromisoformat(started_at),
        revisions=[_revision(connection, revision) for revision in revisions],
        revision_count=len(revisions),
    )
    return data.model_dump(mode="json"), [], [], "COMPLETE"


# --- C2: household receipts (ADR 0009 section 6) -----------------------------------------


async def household_receipts(
    service: "ContextService", operation: "Operation", connection: sqlite3.Connection,
    arguments: dict | None,
) -> Response:
    args = HouseholdReceiptsArguments.model_validate(arguments)
    await service.authorize(operation, connection, "household_receipts_preflight", [])
    await service.authorize(operation, connection, "household_receipts", [args.household_id])
    operation.origin_action = "household_receipts"
    operation.scopes = [args.household_id]
    service.resolve_cutoffs(operation, args.as_of, args.known_as_of)
    assert operation.day is not None and operation.known is not None
    # The same resolution the snapshot uses; its figures are discarded, its flags,
    # status, accounts and cited records are what the receipts list is defined over.
    with service.tracer.start_as_current_span("query"):
        result, flags, sources = calculate_snapshot(
            connection, args.household_id, operation.day, operation.known,
        )
    operation.account_scope = [OriginAccount(
        household_id=args.household_id, account_id=account.id, custodian=account.custodian,
        account_number=account.account_number,
    ) for account in [*result.accounts, *result.excluded_accounts]]
    operation.sources = list(sources)
    placeholders = ",".join("?" * len(sources))
    rows = connection.execute(
        "SELECT s.id,r.id AS run_id,r.source_system,r.feed,r.business_date,r.recorded_at "
        f"FROM source_records s LEFT JOIN feed_runs r ON r.id=s.feed_run_id "
        f"WHERE s.id IN ({placeholders})", list(sources),
    ).fetchall() if sources else []
    if len(rows) != len(set(sources)):
        raise sqlite3.DatabaseError("Cited source record missing")
    runs: dict[str, dict] = {}
    without_run = 0
    for row in rows:
        if row["run_id"] is None:
            without_run += 1
            continue
        receipt = runs.setdefault(row["run_id"], {
            key: row[key] for key in ("run_id", "source_system", "feed", "business_date",
                                      "recorded_at")
        } | {"record_count": 0})
        receipt["record_count"] += 1
    receipts = sorted((ReceiptItem.model_validate(item) for item in runs.values()),
                      key=lambda item: item.run_id)
    receipts.sort(key=lambda item: item.recorded_at, reverse=True)
    data = HouseholdReceiptsData(
        items=receipts[:PAGE_SIZE], item_count=min(PAGE_SIZE, len(receipts)),
        total_item_count=len(receipts), ordering="recorded_at DESC, run_id ASC",
        truncated=len(receipts) > PAGE_SIZE, household_id=args.household_id,
        cited_record_count=len(set(sources)), records_without_run_count=without_run,
    )
    status = "COMPLETE" if isinstance(result, CompleteSnapshot) else "INCOMPLETE"
    return data.model_dump(mode="json"), list(flags), [], status


# --- C3: the authorized span tree (ADR 0009 section 7) -----------------------------------


def normalise_trace_id(value: str) -> str | None:
    """Lower case, an optional 0x prefix removed, exactly 32 hexadecimal digits."""
    text = value.lower()
    text = text.removeprefix("0x")
    if len(text) == 32 and all(character in "0123456789abcdef" for character in text):
        return text
    return None


def _span_hex(value: object) -> str | None:
    if not isinstance(value, str):
        return None
    text = value.lower().removeprefix("0x")
    if len(text) == 16 and all(character in "0123456789abcdef" for character in text):
        return text
    return None


def _instant(value: object) -> datetime | None:
    if not isinstance(value, str):
        return None
    try:
        parsed = datetime.fromisoformat(value)
    except ValueError:
        return None
    return parsed.astimezone(UTC) if parsed.tzinfo is not None else None


@dataclass(frozen=True)
class SpanRead:
    spans: list[dict]
    source: Literal["EXPORT", "UNAVAILABLE"]
    scan_truncated: bool


def read_spans(path: Path | None, trace_id: str, now: datetime) -> SpanRead:
    """Scan at most the newest 32 MiB; skip lines that do not parse; allowlisted fields."""
    if path is None:
        return SpanRead([], "UNAVAILABLE", False)
    try:
        with path.open("rb") as stream:
            size = stream.seek(0, 2)
            start = max(0, size - SCAN_BYTES)
            # One byte more tells whether the scan begins on a line boundary.
            stream.seek(max(0, start - 1))
            data = stream.read(size - max(0, start - 1))
    except OSError:
        return SpanRead([], "UNAVAILABLE", False)
    if start > 0:
        data = data[data.find(b"\n") + 1:] if b"\n" in data else b""
    needle = trace_id.encode()
    spans: dict[str, dict] = {}
    for line in data.split(b"\n"):
        if needle not in line:
            continue
        try:
            raw = json.loads(line)
        except (ValueError, UnicodeError):
            continue
        span = _span(raw, trace_id, now)
        if span is not None and span["span_id"] not in spans:
            spans[span["span_id"]] = span
    ordered = sorted(spans.values(), key=lambda item: (item["start_time"], item["span_id"]))
    return SpanRead(ordered, "EXPORT", start > 0)


def _span(raw: object, trace_id: str, now: datetime) -> dict | None:
    if not isinstance(raw, dict):
        return None
    raw = cast(dict, raw)
    context = raw.get("context")
    status = raw.get("status")
    if not isinstance(context, dict) or not isinstance(status, dict):
        return None
    if (not isinstance(context.get("trace_id"), str)
            or normalise_trace_id(context["trace_id"]) != trace_id):
        return None
    span_id = _span_hex(context.get("span_id"))
    parent = raw.get("parent_id")
    parent_id = None if parent is None else _span_hex(parent)
    start, end = _instant(raw.get("start_time")), _instant(raw.get("end_time"))
    name, code = raw.get("name"), status.get("status_code")
    if (span_id is None or (parent is not None and parent_id is None) or start is None
            or end is None or end < start or not isinstance(name, str)
            or code not in STATUS_CODES):
        return None
    if end > now:
        return None  # a span that ended after the request time is left out
    microseconds = (end - start) // timedelta(microseconds=1)
    return {
        "span_id": span_id, "parent_span_id": parent_id,
        "name": name if name in SPAN_NAMES else "other", "start_time": start,
        "duration_ms": format(Decimal(microseconds).scaleb(-3).quantize(Decimal("0.001")), "f"),
        "status_code": code,
    }


async def trace_spans(
    service: "ContextService", operation: "Operation", connection: sqlite3.Connection,
    arguments: dict | None,
) -> Response:
    args = TraceArguments.model_validate(arguments)
    await service.authorize(operation, connection, "trace_spans_preflight", [])
    operation.origin_action = "trace_spans"
    trace_id = normalise_trace_id(args.trace_id)
    cutoff = utc_timestamp(operation.now)
    owners = set() if trace_id is None else {row[0] for row in connection.execute(
        "SELECT principal_id FROM results WHERE trace_id=? AND recorded_at<=? UNION "
        "SELECT principal_id FROM decisions WHERE trace_id=? AND recorded_at<=?",
        (trace_id, cutoff, trace_id, cutoff),
    )}
    if not owners or (operation.principal.role != "compliance"
                      and owners != {operation.principal.id}):
        service.deny_locally(operation, "TRACE_OWNER_MISMATCH")
    assert trace_id is not None
    rows = connection.execute(
        "SELECT id,payload,payload_hash FROM results WHERE trace_id=? AND recorded_at<=? "
        "ORDER BY id", (trace_id, cutoff),
    ).fetchall()
    if not rows:
        raise sqlite3.DatabaseError("Trace result association missing")
    # Validated exactly as `trace` validates its evidence (ADR 0007), before any household
    # is authorized and before the export is opened.
    households: set[str] = set()
    for row in rows:
        households.update(service.result_scopes(connection, row["id"]))
        if digest(row["payload"]) != row["payload_hash"]:
            raise sqlite3.DatabaseError("Trace result hash mismatch")
        envelope = json.loads(row["payload"])
        if envelope.get("trace_id") != trace_id or envelope.get("result_id") != row["id"]:
            raise sqlite3.DatabaseError("Trace result association mismatch")
    scopes = sorted(households)
    await service.authorize(operation, connection, "trace_spans", scopes)
    operation.scopes = scopes
    operation.account_scope = []
    read = read_spans(service.span_export, trace_id, operation.now)
    returned = read.spans[:SPAN_LIMIT]
    ids = {span["span_id"] for span in returned}
    items = []
    for span in returned:
        omitted = span["parent_span_id"] is not None and span["parent_span_id"] not in ids
        items.append(SpanItem.model_validate({
            **span, "parent_span_id": None if omitted else span["parent_span_id"],
            "parent_omitted": omitted,
        }))
    data = TraceSpansData(
        trace_id=trace_id, items=items, item_count=len(items),
        total_item_count=len(read.spans), ordering="start_time ASC, span_id ASC",
        truncated=len(read.spans) > len(items), span_source=read.source,
        scan_truncated=read.scan_truncated,
    )
    return data.model_dump(mode="json"), [], [], "COMPLETE"


# --- ADR 0009 sections 3 and 4: the operations contracts (O1, O2) --------------------------
#
# Both take `feed_runs` on the empty scope before any date is resolved or run looked up, then
# authorize `feed_runs` exactly over the households they evaluated, as `feed_runs` does. They
# return identifiers, closed codes, counts and timestamps only, for every role.

IMPACT_REASONS = frozenset(get_args(ImpactReason)) - {"OTHER"}


def _out_of_range() -> Exception:
    from wealthcontext.context_service import RequestFailure

    return RequestFailure("AS_OF_OUT_OF_RANGE", "Date is outside the data window")


def _business_days(first: date, last: date) -> list[date]:
    days, day = [], first
    while day <= last:
        if day.weekday() < 5:
            days.append(day)
        day += timedelta(days=1)
    return days


async def feed_health(
    service: "ContextService", operation: "Operation", connection: sqlite3.Connection,
    arguments: dict | None,
) -> Response:
    args = FeedHealthArguments.model_validate(arguments)
    await service.authorize(operation, connection, "feed_runs", [])
    operation.origin_action = "feed_runs"
    # The envelope's as_of and valuation_date carry date_to; date_from resolves the same way.
    service.resolve_cutoffs(operation, args.date_to, args.known_as_of)
    assert operation.day is not None and operation.known is not None
    try:
        first = valuation_date(args.date_from)
    except ValueError:
        raise _out_of_range() from None
    days = _business_days(first, operation.day)
    # Every household active on any day of the range at the cutoff.
    scopes = sorted({household for day in days
                     for household in _ids(connection, day, operation.known)})
    await service.authorize(operation, connection, "feed_runs", scopes)
    operation.scopes = scopes
    operation.account_scope = []
    operation.sources = []  # run metadata only; a cell's receipt is reached through O2
    latest: dict[tuple[str, str, str], sqlite3.Row] = {}
    runs: dict[tuple[str, str, str], int] = {}
    for row in connection.execute(
        "SELECT id,source_system,feed,business_date,status,upstream_status,recorded_at,"
        "accepted_records,rejected_records FROM feed_runs WHERE business_date>=? "
        "AND business_date<=? AND recorded_at<=? ORDER BY recorded_at DESC,id ASC",
        (first.isoformat(), operation.day.isoformat(), utc_timestamp(operation.known)),
    ):
        key = (row["business_date"], row["source_system"], row["feed"])
        latest.setdefault(key, row)
        runs[key] = runs.get(key, 0) + 1
    cells = []
    for day in days:
        for system, feed in EXPECTED_FEED_PAIRS:
            key = (day.isoformat(), system, feed)
            run = latest.get(key)
            cells.append(FeedHealthCell(
                source_system=system, feed=feed, business_date=day,
                state="NOT_RECEIVED" if run is None else run["status"],
                run_id=None if run is None else run["id"],
                upstream_status=None if run is None else run["upstream_status"],
                accepted_records=None if run is None else run["accepted_records"],
                rejected_records=None if run is None else run["rejected_records"],
                recorded_at=None if run is None else run["recorded_at"],
                run_count=runs.get(key, 0),
            ))
    data = FeedHealthData(
        date_from=first, date_to=operation.day, business_dates=days,
        business_day_count=len(days),
        pairs=[FeedPair(source_system=system, feed=feed) for system, feed in EXPECTED_FEED_PAIRS],
        items=cells, item_count=len(cells), total_item_count=len(cells),
        ordering="business_date ASC, source_system ASC, feed ASC", truncated=False,
        state_counts=[FeedStateCount(state=state,
                                     cell_count=sum(cell.state == state for cell in cells))
                      for state in CELL_STATES],
    )
    return data.model_dump(mode="json"), [], [], "COMPLETE"


def _attribute(
    key: tuple[str, str], mapping: dict[tuple[str, str], str | None],
    views: dict[str, FactView], scopes: set[str], sources: set[str],
) -> tuple[str | None, str | None, HouseholdAttribution]:
    """External account -> canonical account -> reporting household, as valuation reads them."""
    account_id = mapping.get(key)
    if account_id is None:
        sources.update(item["source_id"] for item in views["account"].values.values()
                       if (item["custodian"], item["account_number"]) == key)
        sources.update(item["source_id"] for group in views["account"].ambiguous.values()
                       for item in group if (item["custodian"], item["account_number"]) == key)
        return None, None, "CROSSWALK_UNAVAILABLE"
    sources.add(views["account"].values[account_id]["source_id"])
    reporting = views["reporting"]
    if account_id in reporting.ambiguous:
        sources.update(item["source_id"] for item in reporting.ambiguous[account_id])
        return account_id, None, "AMBIGUOUS"
    assignment = reporting.values.get(account_id)
    if assignment is None:
        return account_id, None, "HOUSEHOLD_UNAVAILABLE"
    sources.add(assignment["source_id"])
    if assignment["household_id"] not in scopes:
        # Never name a household outside the authorized scope of this result.
        return account_id, None, "HOUSEHOLD_UNAVAILABLE"
    return account_id, assignment["household_id"], "RESOLVED"


async def feed_run_impact(
    service: "ContextService", operation: "Operation", connection: sqlite3.Connection,
    arguments: dict | None,
) -> Response:
    args = FeedRunImpactArguments.model_validate(arguments)
    await service.authorize(operation, connection, "feed_runs", [])
    operation.origin_action = "feed_runs"
    known = (args.known_as_of or operation.now).astimezone(UTC)
    # Unknown, and recorded after the cutoff, are the same NOT_FOUND for an allowed caller.
    run = connection.execute(
        "SELECT id,source_system,feed,business_date,status,upstream_status,recorded_at,"
        "accepted_records,rejected_records,receipt_source_id,content IS NOT NULL AS received "
        "FROM feed_runs WHERE id=? AND recorded_at<=?", (args.run_id, utc_timestamp(known)),
    ).fetchone()
    if run is None:
        raise LookupError("Feed run unavailable at this cutoff")
    # The run's business date becomes the valuation date.
    service.resolve_cutoffs(operation, date.fromisoformat(run["business_date"]),
                            args.known_as_of)
    assert operation.day is not None and operation.known is not None
    scopes = _ids(connection, operation.day, operation.known)
    await service.authorize(operation, connection, "feed_runs", scopes)
    operation.scopes = scopes
    operation.account_scope = []
    views = household_views(connection, operation.day, operation.known)
    for household_id in scopes:
        _scope(operation, household_id, household_context(
            connection, household_id, operation.day, operation.known, views=views,
        ))
    sources = {run["receipt_source_id"]}
    fields = {
        "run_id": run["id"], "source_system": run["source_system"], "feed": run["feed"],
        "business_date": run["business_date"], "status": run["status"],
        "upstream_status": run["upstream_status"], "recorded_at": run["recorded_at"],
        "file_received": bool(run["received"]), "accepted_records": run["accepted_records"],
        "rejected_records": run["rejected_records"],
    }
    if run["feed"] not in SNAPSHOT_FEEDS:
        # Only the two snapshot feeds publish per-account availability: null, not zero.
        operation.sources = sorted(sources)
        data = FeedRunImpactData(
            **fields, account_attribution="NOT_ATTRIBUTABLE", expected_account_count=None,
            committed_account_count=None, affected_account_count=None,
            affected_household_count=None, unattributed_account_count=None,
            affected_accounts=None, affected_households=None,
        )
        return data.model_dump(mode="json"), [], [], "COMPLETE"
    # Expected accounts come from the run's manifest, never from what arrived.
    stored = connection.execute("SELECT payload,payload_hash FROM feed_manifests WHERE run_id=?",
                                (run["id"],)).fetchone()
    if stored is None or digest(stored["payload"]) != stored["payload_hash"]:
        raise sqlite3.DatabaseError("Run manifest unavailable")
    manifest = json.loads(stored["payload"])
    publications = {(row["custodian"], row["account_number"]): row for row in connection.execute(
        "SELECT custodian,account_number,manifest_account_id,available,reason "
        "FROM account_publications WHERE run_id=?", (run["id"],),
    )}
    if len(publications) != len(manifest["accounts"]):
        raise sqlite3.DatabaseError("Run publications do not mirror its manifest")
    expected: list[tuple[tuple[str, str], str | None, bool, str | None]] = []
    for account in manifest["accounts"]:
        key = (account["custodian"], account["account_number"])
        publication = publications.get(key)
        if publication is None or publication["manifest_account_id"] != account["account_id"]:
            raise sqlite3.DatabaseError("Run publications do not mirror its manifest")
        expected.append((key, account["account_id"], bool(publication["available"]),
                         publication["reason"]))
    # An account CRM shows as managed at the source's custodian, at the run's business date
    # and the cutoff, that the manifest omits is expected and affected too, as valuation
    # treats it (ingestion's own rule, read at the query's cutoffs).
    custodian = run["source_system"].rsplit("-", 1)[-1]
    named = {key for key, *_ in expected}
    accounts = [*views["account"].values.values(),
                *(item for group in views["account"].ambiguous.values() for item in group)]
    omitted = sorted({(item["custodian"], item["account_number"]) for item in accounts
                      if item["is_managed"] and item["custodian"] == custodian} - named)
    expected.extend((key, None, False, "EXPECTED_ACCOUNT_MISSING_FROM_MANIFEST")
                    for key in omitted)
    mapping = _crosswalk(views["account"])
    authorized = set(scopes)
    affected: list[tuple[tuple, AffectedAccount]] = []
    for key, label, available, reason in expected:
        if available:
            continue
        account_id, household_id, attribution = _attribute(key, mapping, views, authorized,
                                                           sources)
        item = AffectedAccount(
            account_id=account_id, manifest_account_id=label,
            reason=cast(ImpactReason, reason) if reason in IMPACT_REASONS else "OTHER",
            household_id=household_id, household_attribution=attribution,
        )
        affected.append(((account_id is None, account_id or "", label is None, label or "", key),
                         item))
    affected.sort(key=lambda pair: pair[0])
    items = [item for _, item in affected]
    households = sorted({item.household_id for item in items if item.household_id is not None})
    operation.sources = sorted(sources)
    data = FeedRunImpactData(
        **fields, account_attribution="MANIFEST", expected_account_count=len(expected),
        committed_account_count=sum(available for _, _, available, _ in expected),
        affected_account_count=len(items), affected_household_count=len(households),
        unattributed_account_count=sum(item.household_attribution != "RESOLVED"
                                       for item in items),
        affected_accounts=_listed(items, "account_id ASC (unresolved last), "
                                         "manifest_account_id ASC"),
        affected_households=_listed([AffectedHousehold(household_id=household)
                                     for household in households], "household_id ASC"),
    )
    return data.model_dump(mode="json"), [], [], "COMPLETE"


async def execute_surface(
    service: "ContextService", operation: "Operation", connection: sqlite3.Connection,
    tool: str, arguments: dict | None,
) -> tuple[dict, list[QualityFlag], list[str], str]:
    """Authorize before reading, preserve all evaluated scopes, and cap only output."""
    handlers = {"search_households": _search, "list_households": _list_households,
                "get_data_quality_report": _quality,
                "feed_runs": _feeds, "conflicts": _conflicts,
                "decision_log": _decisions, "trace": _trace,
                "decision_log_page": decision_log_page,
                "household_receipts": household_receipts, "trace_spans": trace_spans,
                "feed_health": feed_health, "feed_run_impact": feed_run_impact}
    if tool in handlers:
        return await handlers[tool](service, operation, connection, arguments)
    if tool == "eval_reports":
        EmptyArguments.model_validate(arguments)
        await service.authorize(operation, connection, "view_eval_reports", [])
        operation.origin_action = "view_eval_reports"
        operation.account_scope = []
        reports = evaluation_summaries(connection, operation.now)
        return _response(_listed(reports, "recorded_at DESC, id ASC"))
    raise ValueError("Unknown secondary surface")
