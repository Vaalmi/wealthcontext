"""Governed operations shared by REST, MCP and the first answer harness."""

import json
import secrets
import sqlite3
from dataclasses import dataclass, field
from datetime import UTC, date, datetime
from decimal import Decimal
from pathlib import Path
from typing import Annotated, Literal, cast
from zoneinfo import ZoneInfo

from opentelemetry.trace import Tracer
from pydantic import AfterValidator, JsonValue, StringConstraints, ValidationError

from wealthcontext.calculation import concentrated_positions
from wealthcontext.clock import utc_timestamp
from wealthcontext.contracts import (
    Answer,
    Contract,
    Envelope,
    Error,
    ErrorCode,
    Identifier,
    KnowledgeCutoff,
    QualityFlag,
)
from wealthcontext.conversation_integrity import conversation_owner, record_conversation
from wealthcontext.database import write_transaction
from wealthcontext.exposure import (
    ConcentrationEnvelope,
    ExposureEnvelope,
    serialize_concentration,
    serialize_exposure,
)
from wealthcontext.identity import Principal
from wealthcontext.metrics import catalogue
from wealthcontext.policy import PolicyClient, PolicyUnavailable
from wealthcontext.projections import project_envelope
from wealthcontext.secondary import execute_surface
from wealthcontext.snapshot import (
    CompleteSnapshot,
    SnapshotEnvelope,
    calculate_snapshot,
    valuation_date,
)
from wealthcontext.source_projection import (
    OriginAccount,
    OriginAccountScope,
    SourceScope,
    project_source_record,
)
from wealthcontext.storage import canonical, digest, entitlements, identifier, read_snapshot
from wealthcontext.surface_contracts import SURFACE_OUTPUT_MODELS
from wealthcontext.valuation import ReceivedValuation, resolve_received_valuation
from wealthcontext.verifier import InvalidAnswer, answer_schema_diagnostics, verify_answer

DENIAL = "Caller is not authorized for the requested resource"


class SnapshotArguments(Contract):
    household_id: Identifier
    as_of: date
    known_as_of: KnowledgeCutoff | None = None


def _threshold(value: str) -> str:
    return format(Decimal(value), ".2f")


ThresholdPercent = Annotated[
    str,
    StringConstraints(
        strict=True, max_length=5,
        pattern=r"^(?:(?:[1-9]|[1-4][0-9])(?:\.[0-9]{1,2})?|50(?:\.0{1,2})?)$",
    ),
    AfterValidator(_threshold),
]


class ExposureArguments(SnapshotArguments):
    group_by: Literal["issuer", "security"] = "issuer"


class ConcentrationArguments(Contract):
    as_of: date
    threshold_pct: ThresholdPercent = "10.00"
    known_as_of: KnowledgeCutoff | None = None
    household_id: Identifier | None = None


OUTPUT_MODELS: dict[str, type[Contract]] = {
    "get_household_snapshot": SnapshotEnvelope,
    "get_portfolio_exposure": ExposureEnvelope,
    "get_concentrated_positions": ConcentrationEnvelope,
}


class MetricArguments(Contract):
    metric_id: Identifier


class ProvenanceArguments(Contract):
    reference: Identifier


class RequestFailure(Exception):
    def __init__(self, code: ErrorCode, message: str, retryable: bool = False):
        self.error = Error(code=code, message=message, retryable=retryable)
        super().__init__(message)


@dataclass
class Operation:
    principal: Principal
    now: datetime
    trace_id: str
    result_id: str
    action: str
    conversation_id: str | None
    revision: int | None = None
    policy_revision: str | None = None
    as_of: date | None = None
    day: date | None = None
    known: datetime | None = None
    decisions: list[dict] = field(default_factory=list)
    scopes: list[str] = field(default_factory=list)
    sources: list[str] = field(default_factory=list)
    entitlement_facts: list[dict] | None = None
    account_scope: list[OriginAccount] | None = None
    projection: Literal["full", "no_pii"] = "full"
    origin_action: str | None = None


class ResultScopeEvidence(Contract):
    result_id: Identifier
    result_payload_hash: Annotated[str, StringConstraints(pattern=r"^[a-f0-9]{64}$")]
    action: str
    projection: Literal["full", "no_pii"]
    household_ids: list[Identifier]


class ResultIssuanceEvidence(Contract):
    result_id: Identifier
    result_payload_hash: Annotated[str, StringConstraints(pattern=r"^[a-f0-9]{64}$")]
    principal_id: Identifier | None
    conversation_id: Identifier | None
    provenance_handle_id: Identifier | None
    source_ids: list[Identifier]


class ContextService:
    def __init__(
        self, database: Path, policy: PolicyClient, tracer: Tracer,
        span_export: Path | None = None,
    ):
        self.database, self.policy, self.tracer = database, policy, tracer
        # ADR 0009 section 7: the span export trace_spans reads after authorization; None
        # (no export configured) is reported as UNAVAILABLE.
        self.span_export = span_export
        # ADR 0009 section 5: decision log cursors are sealed with a key held only by this
        # running service, so a cursor issued before a restart is refused.
        self.cursor_key = secrets.token_bytes(32)

    async def authorize(
        self, operation: Operation, connection: sqlite3.Connection, action: str,
        resources: list[str], *, filtered: bool = False,
    ) -> list[str]:
        if operation.entitlement_facts is None:
            operation.revision, operation.entitlement_facts = entitlements(connection)
        assert operation.revision is not None
        revision, facts = operation.revision, operation.entitlement_facts
        with self.tracer.start_as_current_span("authz"):
            try:
                request_day = operation.now.astimezone(ZoneInfo("America/New_York")).date()
                if filtered:
                    if action == "concentration_book":
                        decision = await self.policy.filter_candidates(
                            operation.principal, resources, facts, revision, request_day,
                        )
                    else:
                        decision = await self.policy.filter_resources(
                            operation.principal, action, resources, facts, revision, request_day,
                        )
                else:
                    decision = await self.policy.decide(
                        operation.principal, action, resources, facts, revision, request_day,
                    )
                expected_projection = ("no_pii" if operation.principal.role == "ops"
                                       and action != "metric_definition" else "full")
                if decision.projection != expected_projection:
                    raise PolicyUnavailable("unexpected projection for this principal/action")
            except PolicyUnavailable:
                operation.policy_revision = None
                operation.decisions.append({"action": action, "resources": resources,
                                            "allow": False, "reason": "POLICY_UNAVAILABLE",
                                            "policy_revision": None})
                raise RequestFailure(
                    "POLICY_UNAVAILABLE", "Authorization is unavailable", True,
                ) from None
        operation.policy_revision = decision.policy_revision
        operation.decisions.append({"action": action, "resources": resources,
                                    "allow": decision.allow, "allowed_ids": decision.allowed_ids,
                                    "reason": decision.reason,
                                    "policy_revision": decision.policy_revision,
                                    "projection": decision.projection})
        if decision.projection == "no_pii":
            operation.projection = "no_pii"
        if not decision.allow:
            raise RequestFailure("NOT_AUTHORIZED", DENIAL)
        return decision.allowed_ids

    def deny_locally(self, operation: Operation, reason: str) -> None:
        operation.decisions.append({"action": operation.action, "resources": [], "allow": False,
                                    "reason": reason, "policy_revision": operation.policy_revision})
        raise RequestFailure("NOT_AUTHORIZED", DENIAL)

    def resolve_cutoffs(
        self, operation: Operation, requested: date, known: datetime | None,
    ) -> None:
        operation.as_of = requested
        try:
            operation.day = valuation_date(requested)
        except ValueError:
            raise RequestFailure("AS_OF_OUT_OF_RANGE", "Date is outside the data window") from None
        operation.known = (known or operation.now).astimezone(UTC)

    def inventory(self, operation: Operation, connection: sqlite3.Connection) -> list[str]:
        # This identifier-only enumeration is permitted by the explicit book preflight.
        assert operation.entitlement_facts is not None
        published = {row[0] for row in connection.execute(
            "SELECT DISTINCT entity_id FROM facts WHERE kind='household' AND recorded_at<=?",
            (utc_timestamp(operation.now),),
        )}
        targets = {fact["object"] for fact in operation.entitlement_facts
                   if fact["kind"] in {"advisor_covers", "team_covers"}}
        return sorted(published | targets)

    def collect_received_scope(self, operation: Operation, received: ReceivedValuation) -> None:
        assert operation.account_scope is not None
        household = received.valuation.household_id
        accounts = [item.account for item in received.context.accounts]
        accounts.extend(received.context.excluded_accounts)
        operation.account_scope.extend(OriginAccount(
            household_id=household, account_id=account["id"], custodian=account["custodian"],
            account_number=account["account_number"],
        ) for account in accounts)

    async def financial_operation(
        self, operation: Operation, connection: sqlite3.Connection, tool: str,
        arguments: dict | None,
    ) -> tuple[dict, list[QualityFlag], list[str], str]:
        if tool == "get_portfolio_exposure":
            args = ExposureArguments.model_validate(arguments)
            await self.authorize(operation, connection, "portfolio_exposure", [args.household_id])
            operation.scopes = [args.household_id]
            self.resolve_cutoffs(operation, args.as_of, args.known_as_of)
            assert operation.day is not None and operation.known is not None
            with self.tracer.start_as_current_span("query"):
                with self.tracer.start_as_current_span("calc"):
                    received = resolve_received_valuation(connection, args.household_id,
                                                          operation.day, operation.known)
                    operation.account_scope = []
                    self.collect_received_scope(operation, received)
                    operation.sources = list(received.valuation.source_ids)
                    name = (received.context.household or {}).get("name",
                                                                 "Household name unavailable")
                    data = serialize_exposure(received.valuation, name, args.group_by)
            return (data.model_dump(mode="json"), list(received.quality_flags),
                    ["MV_POSITION@1", "CASH_USD@1", "HOUSEHOLD_ASSETS@1", "ISSUER_EXPOSURE@1"],
                    "COMPLETE" if received.valuation.complete else "INCOMPLETE")
        args = ConcentrationArguments.model_validate(arguments)
        book = args.household_id is None
        if book:
            await self.authorize(operation, connection, "concentration_book_preflight", [])
            candidates = self.inventory(operation, connection)
            operation.scopes = await self.authorize(
                operation, connection, "concentration_book", candidates, filtered=True,
            )
        else:
            assert args.household_id is not None
            await self.authorize(operation, connection, "concentrated_positions",
                                 [args.household_id])
            operation.scopes = [args.household_id]
        self.resolve_cutoffs(operation, args.as_of, args.known_as_of)
        assert operation.day is not None and operation.known is not None
        with self.tracer.start_as_current_span("query"):
            with self.tracer.start_as_current_span("calc"):
                operation.account_scope = []
                valuations, flags, sources = [], [], set()
                for household_id in operation.scopes:
                    received = resolve_received_valuation(
                        connection, household_id, operation.day, operation.known,
                        absent_as_incomplete=book,
                    )
                    self.collect_received_scope(operation, received)
                    valuations.append(received.valuation)
                    flags.extend(received.quality_flags)
                    sources.update(received.valuation.source_ids)
                operation.sources = sorted(sources)
                computed = concentrated_positions(valuations, Decimal(args.threshold_pct))
                data = serialize_concentration(computed)
        return (data.model_dump(mode="json"), flags,
                ["MV_POSITION@1", "CASH_USD@1", "HOUSEHOLD_ASSETS@1", "ISSUER_EXPOSURE@1",
                 "CONCENTRATION@1"], computed.status)

    async def execute(
        self, principal: Principal, now: datetime, tool: str, arguments: dict | None,
        conversation_id: str | None = None,
    ) -> dict:
        with self.tracer.start_as_current_span(
            "tool", attributes={"wealthcontext.tool": tool},
        ) as span:
            operation = Operation(principal, now.astimezone(UTC),
                                  format(span.get_span_context().trace_id, "032x"),
                                  identifier("r"), tool, conversation_id)
            data: dict | None = None
            flags: list[QualityFlag] = []
            metrics: list[str] = []
            error = None
            status = "COMPLETE"
            try:
                with read_snapshot(self.database) as connection:
                    if conversation_id is not None:
                        owner = conversation_owner(connection, conversation_id)
                        if owner != principal.id:
                            self.deny_locally(operation, "CONVERSATION_OWNER_MISMATCH")
                    operation.origin_action = {
                        "get_household_snapshot": "snapshot",
                        "get_portfolio_exposure": "portfolio_exposure",
                        "get_concentrated_positions": "concentrated_positions",
                        "get_metric_definition": "metric_definition",
                        "get_data_provenance": "provenance",
                        "submit_answer": "verify_claim",
                        "search_households": "search_households",
                        "list_households": "search_households",
                        "get_data_quality_report": "view_quality",
                        "feed_runs": "feed_runs", "conflicts": "view_conflicts",
                        "decision_log": "view_decision_log", "eval_reports": "view_eval_reports",
                        "trace": "trace_scope", "decision_log_page": "view_decision_log",
                        "household_receipts": "household_receipts",
                        "trace_spans": "trace_spans", "feed_health": "feed_runs",
                        "feed_run_impact": "feed_runs",
                    }.get(tool)
                    if tool == "get_household_snapshot":
                        args = SnapshotArguments.model_validate(arguments)
                        await self.authorize(operation, connection, "snapshot", [args.household_id])
                        operation.scopes = [args.household_id]
                        operation.as_of = args.as_of
                        try:
                            operation.day = valuation_date(args.as_of)
                        except ValueError:
                            raise RequestFailure(
                                "AS_OF_OUT_OF_RANGE", "Date is outside the data window",
                            ) from None
                        operation.known = (args.known_as_of or now).astimezone(UTC)
                        with self.tracer.start_as_current_span("query"):
                            with self.tracer.start_as_current_span("calc"):
                                result, flags, operation.sources = calculate_snapshot(
                                    connection, args.household_id, operation.day, operation.known,
                                )
                        status = ("COMPLETE" if isinstance(result, CompleteSnapshot)
                                  else "INCOMPLETE")
                        data = result.model_dump(mode="json")
                        operation.account_scope = [OriginAccount(
                            household_id=args.household_id, account_id=account.id,
                            custodian=account.custodian, account_number=account.account_number,
                        ) for account in [*result.accounts, *result.excluded_accounts]]
                        metrics = ["MV_POSITION@1", "CASH_USD@1", "HOUSEHOLD_ASSETS@1"]
                    elif tool in {"get_portfolio_exposure", "get_concentrated_positions"}:
                        data, flags, metrics, status = await self.financial_operation(
                            operation, connection, tool, arguments,
                        )
                    elif tool in SURFACE_OUTPUT_MODELS:
                        data, flags, metrics, status = await execute_surface(
                            self, operation, connection, tool, arguments,
                        )
                    elif tool == "get_metric_definition":
                        metric = MetricArguments.model_validate(arguments)
                        await self.authorize(operation, connection, "metric_definition", [])
                        # Definitions are not historical. Their counts carry the request's
                        # business day and instant so summary claims can cite them
                        # (SOL-P5-R1-SERVICE-001); denial still precedes this scope.
                        operation.day = operation.now.astimezone(
                            ZoneInfo("America/New_York")).date()
                        operation.known = operation.now
                        items = catalogue()
                        if metric.metric_id == "catalog":
                            data = {
                                "items": items, "item_count": len(items), "ordering": "metric_id",
                                "truncated": False, "next_cursor": None,
                            }
                        else:
                            data = next(
                                (item for item in items if item["id"] == metric.metric_id), None,
                            )
                            if data is None:
                                raise RequestFailure("NOT_FOUND", "Metric definition not found")
                    elif tool == "get_data_provenance":
                        reference = ProvenanceArguments.model_validate(arguments).reference
                        data = await self.provenance(operation, connection, reference)
                    elif tool == "submit_answer":
                        if not operation.conversation_id:
                            self.deny_locally(operation, "CONVERSATION_REQUIRED")
                        answer = Answer.model_validate(arguments)
                        await self.authorize(operation, connection, "result_preflight", [])
                        await self.authorize(operation, connection, "verify_claim", [])
                        cited_ids = {
                            item.result_id for item in [*answer.claims, *answer.predicates]
                        }
                        if answer.refusal is not None:
                            cited_ids.add(answer.refusal.result_id)
                        resolved = {}
                        for result_id in sorted(cited_ids):
                            resolved[result_id] = await self.resolve_result(
                                operation, connection, result_id,
                            )
                        data = verify_answer(answer, resolved)
                    else:
                        operation.decisions.append({
                            "action": tool, "resources": [], "allow": False,
                            "reason": "INVALID_ARGUMENT", "policy_revision": None,
                        })
                        raise RequestFailure("INVALID_ARGUMENT", "Unknown operation")
            except RequestFailure as exc:
                error = exc.error
            except ValidationError as exc:
                operation.decisions.append({
                    "action": tool, "resources": [], "allow": False,
                    "reason": "INVALID_ARGUMENT", "policy_revision": operation.policy_revision,
                })
                error = Error(code="INVALID_ARGUMENT", message="Invalid operation arguments",
                              retryable=False, details={
                                  "diagnostics": cast(JsonValue, answer_schema_diagnostics(exc)),
                              } if tool == "submit_answer" else {})
            except InvalidAnswer as exc:
                error = Error(code="INVALID_ARGUMENT",
                              message="Answer did not satisfy the verification contract",
                              retryable=False,
                              details={"diagnostics": cast(JsonValue, exc.diagnostics)})
            except LookupError:
                error = Error(code="NOT_FOUND", message="Data is not available at these cutoffs",
                              retryable=False)
            except sqlite3.Error:
                error = Error(code="INTERNAL", message="Data service unavailable", retryable=True)
            if error is not None and not operation.scopes:
                # Own neutral error envelopes can support a verified refusal without
                # granting the denied business action. Provider-generated arguments
                # and retry diagnostics have additional conversation dependencies.
                operation.origin_action = "error_result"
                if operation.principal.role == "ops":
                    operation.projection = "no_pii"
            if error is not None and not operation.decisions:
                operation.decisions.append({
                    "action": tool, "resources": [], "allow": False,
                    "reason": error.code, "policy_revision": operation.policy_revision,
                })
            handle = identifier("h") if operation.sources and error is None else None
            envelope_model = OUTPUT_MODELS.get(
                tool, SURFACE_OUTPUT_MODELS.get(tool, Envelope[dict[str, JsonValue]]),
            )
            envelope = envelope_model.model_validate({
                "trace_id": operation.trace_id, "result_id": operation.result_id,
                "status": "ERROR" if error else status, "as_of": operation.as_of,
                "valuation_date": operation.day, "known_as_of": operation.known,
                "calculated_at": operation.now, "entitlement_revision": operation.revision,
                "policy_revision": operation.policy_revision, "data": None if error else data,
                "quality_flags": [] if error else flags, "source_refs": [handle] if handle else [],
                "metric_refs": [] if error else metrics, "error": error,
            }).model_dump(mode="json")
            envelope = envelope_model.model_validate(
                project_envelope(envelope, operation.projection),
            ).model_dump(mode="json")
            try:
                self.record(operation, envelope, handle)
            except (sqlite3.Error, ValueError):
                # Failure to persist evidence cannot release an apparently successful data result.
                envelope.update(
                    status="ERROR", data=None, source_refs=[], metric_refs=[], quality_flags=[],
                    error=Error(code="INTERNAL", message="Response evidence unavailable",
                                retryable=True).model_dump(),
                )
            return envelope

    async def provenance(
        self, operation: Operation, connection: sqlite3.Connection, reference: str,
    ) -> dict:
        await self.authorize(operation, connection, "provenance_preflight", [])
        handle = connection.execute(
            "SELECT * FROM provenance_handles WHERE id=?", (reference,),
        ).fetchone()
        if not handle or (handle["principal_id"] != operation.principal.id
                          and operation.principal.role != "compliance"):
            self.deny_locally(operation, "HANDLE_OWNER_MISMATCH")
        assert handle is not None
        issuance = self.result_issuance(connection, handle["result_id"])
        try:
            sources = json.loads(handle["source_ids"])
        except (ValueError, TypeError):
            raise RequestFailure("INTERNAL", "Source issuance evidence unavailable", True) from None
        if (issuance.provenance_handle_id != reference
                or issuance.principal_id != handle["principal_id"]
                or sources != issuance.source_ids):
            raise RequestFailure("INTERNAL", "Source issuance evidence unavailable", True)
        scopes = self.result_scopes(connection, handle["result_id"])
        access_action, origin_projection = self.result_access(operation, connection,
                                                               handle["result_id"])
        await self.authorize(operation, connection, access_action, scopes)
        if origin_projection == "full" and operation.decisions[-1]["projection"] == "no_pii":
            self.deny_locally(operation, "ORIGIN_PROJECTION_NOT_REUSABLE")
        await self.authorize(operation, connection, "provenance", scopes)
        if origin_projection == "no_pii":
            operation.projection = "no_pii"
        operation.scopes = sorted(set(operation.scopes) | set(scopes))
        origin = connection.execute(
            "SELECT payload,payload_hash FROM results WHERE id=?", (handle["result_id"],),
        ).fetchone()
        if not origin or digest(origin["payload"]) != origin["payload_hash"]:
            raise RequestFailure("INTERNAL", "Recorded result evidence unavailable", True)
        try:
            origin_envelope = json.loads(origin["payload"])
            operation.as_of = (date.fromisoformat(origin_envelope["as_of"])
                               if origin_envelope.get("as_of") is not None else None)
            operation.day = (date.fromisoformat(origin_envelope["valuation_date"])
                             if origin_envelope.get("valuation_date") is not None else None)
            operation.known = (datetime.fromisoformat(origin_envelope["known_as_of"])
                               if origin_envelope.get("known_as_of") is not None else None)
            account_scope = connection.execute(
                "SELECT schema_version,payload,payload_hash FROM result_account_scopes "
                "WHERE result_id=?", (handle["result_id"],),
            ).fetchone()
            if account_scope is None:
                legacy = json.loads(origin["payload"])
                if not isinstance(legacy.get("data"), dict) or "accounts" not in legacy["data"]:
                    raise ValueError("non-snapshot result missing account metadata")
                scope = SourceScope.from_result(legacy, scopes)
            else:
                if (account_scope["schema_version"] != 1
                        or digest(account_scope["payload"]) != account_scope["payload_hash"]):
                    raise ValueError("origin account metadata hash/version mismatch")
                scope = SourceScope.from_account_scope(
                    json.loads(account_scope["payload"]), scopes, handle["result_id"],
                )
        except (ValueError, TypeError):
            raise RequestFailure("INTERNAL", "Recorded result scope unavailable", True) from None
        items = []
        for source_id in sorted(sources)[:20]:
            row = connection.execute(
                "SELECT * FROM source_records WHERE id=?", (source_id,),
            ).fetchone()
            if not row:
                raise RequestFailure("INTERNAL", "Source evidence unavailable", True)
            try:
                items.append(project_source_record(dict(row), scope,
                                                   projection=operation.projection))
            except (ValueError, TypeError):
                raise RequestFailure("INTERNAL", "Source evidence unavailable", True) from None
        return {"reference": reference, "items": items, "item_count": len(items),
                "total_item_count": len(sources), "ordering": "source_id",
                "truncated": len(sources) > len(items), "next_cursor": None}

    def record(self, operation: Operation, envelope: dict, handle: str | None) -> None:
        payload = canonical(envelope)
        with write_transaction(self.database) as connection:
            for decision in operation.decisions:
                decision_id = identifier("d")
                connection.execute("INSERT INTO decisions VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
                                   (decision_id, operation.principal.id, decision["action"],
                                    canonical(decision["resources"]), int(decision["allow"]),
                                    decision["reason"], decision["policy_revision"],
                                    operation.revision,
                                    operation.trace_id, utc_timestamp(operation.now)))
                if "projection" in decision:
                    connection.execute("INSERT INTO decision_projections VALUES (?, ?)",
                                       (decision_id, decision["projection"]))
                for resource in decision.get("allowed_ids", []):
                    connection.execute("INSERT INTO decision_authorized_resources VALUES (?, ?)",
                                       (decision_id, resource))
            # An unowned/unknown conversation cannot be linked through a foreign key.
            conversation_id = operation.conversation_id
            # An empty ID is unowned too; it must not reach the foreign key (ARGON-P5-SERVICE-001).
            if conversation_id is not None:
                try:
                    if conversation_owner(connection, conversation_id) != operation.principal.id:
                        raise sqlite3.DatabaseError("Conversation owner mismatch")
                except sqlite3.DatabaseError:
                    if envelope["status"] != "ERROR":
                        raise
                    conversation_id = None
            connection.execute("INSERT INTO results VALUES (?, ?, ?, ?, ?, ?, ?)",
                               (operation.result_id, operation.trace_id, operation.principal.id,
                                conversation_id, payload, digest(payload),
                                utc_timestamp(operation.now)))
            connection.execute("INSERT INTO result_access VALUES (?, ?, ?, 1)",
                               (operation.result_id, operation.origin_action or operation.action,
                                operation.projection))
            scope_evidence = ResultScopeEvidence(
                result_id=operation.result_id, result_payload_hash=digest(payload),
                action=operation.origin_action or operation.action,
                projection=operation.projection, household_ids=sorted(set(operation.scopes)),
            )
            scope_payload = canonical(scope_evidence.model_dump(mode="json"))
            connection.execute("INSERT INTO result_scope_integrity VALUES (?, 1, ?, ?)",
                               (operation.result_id, scope_payload, digest(scope_payload)))
            source_ids = sorted(set(operation.sources)) if handle else []
            self.record_issuance(connection, ResultIssuanceEvidence(
                result_id=operation.result_id, result_payload_hash=digest(payload),
                principal_id=operation.principal.id, conversation_id=conversation_id,
                provenance_handle_id=handle, source_ids=source_ids,
            ))
            for scope in operation.scopes:
                connection.execute("INSERT INTO result_scopes VALUES (?, ?)",
                                   (operation.result_id, scope))
            if operation.account_scope is not None and envelope["status"] != "ERROR":
                ownership = OriginAccountScope(
                    result_id=operation.result_id, household_ids=sorted(operation.scopes),
                    accounts=sorted(operation.account_scope,
                                    key=lambda item: (item.household_id, item.account_id)),
                )
                account_payload = canonical(ownership.model_dump(mode="json"))
                connection.execute("INSERT INTO result_account_scopes VALUES (?, 1, ?, ?)",
                                   (operation.result_id, account_payload, digest(account_payload)))
            if handle:
                connection.execute("INSERT INTO provenance_handles VALUES (?, ?, ?, ?)",
                                   (handle, operation.result_id, operation.principal.id,
                                    canonical(source_ids)))

    @staticmethod
    def record_issuance(
        connection: sqlite3.Connection, evidence: ResultIssuanceEvidence,
    ) -> None:
        payload = canonical(evidence.model_dump(mode="json"))
        connection.execute("INSERT INTO result_issuance_integrity VALUES (?, 1, ?, ?)",
                           (evidence.result_id, payload, digest(payload)))

    @staticmethod
    def result_issuance(
        connection: sqlite3.Connection, result_id: str,
    ) -> ResultIssuanceEvidence:
        """Verify the original owner, conversation and exact issued source membership."""
        try:
            row = connection.execute(
                "SELECT i.schema_version,i.payload,i.payload_hash,r.principal_id,r.conversation_id,"
                "r.payload_hash AS result_payload_hash FROM result_issuance_integrity i "
                "JOIN results r ON r.id=i.result_id WHERE i.result_id=?", (result_id,),
            ).fetchone()
            if (row is None or row["schema_version"] != 1
                    or digest(row["payload"]) != row["payload_hash"]):
                raise ValueError("issuance evidence missing or corrupt")
            evidence = ResultIssuanceEvidence.model_validate_json(row["payload"])
            if (evidence.result_id != result_id
                    or evidence.result_payload_hash != row["result_payload_hash"]
                    or evidence.principal_id != row["principal_id"]
                    or evidence.conversation_id != row["conversation_id"]
                    or evidence.source_ids != sorted(set(evidence.source_ids))
                    or (evidence.provenance_handle_id is None) != (not evidence.source_ids)
                    or (evidence.provenance_handle_id is not None
                        and evidence.principal_id is None)):
                raise ValueError("issuance metadata does not match original result")
            return evidence
        except (ValueError, TypeError, KeyError):
            raise RequestFailure(
                "INTERNAL", "Recorded issuance evidence unavailable", True,
            ) from None

    @staticmethod
    def result_scopes(connection: sqlite3.Connection, result_id: str) -> list[str]:
        """Validate explicit complete original scope before any redisclosure decision."""
        ContextService.result_issuance(connection, result_id)
        try:
            record = connection.execute(
                "SELECT s.schema_version,s.payload,s.payload_hash,"
                "r.payload_hash AS result_payload_hash,a.action,a.projection,a.metadata_version "
                "FROM result_scope_integrity s JOIN results r ON r.id=s.result_id "
                "JOIN result_access a ON a.result_id=r.id WHERE s.result_id=?", (result_id,),
            ).fetchone()
            if (record is None or record["schema_version"] != 1 or record["metadata_version"] != 1
                    or digest(record["payload"]) != record["payload_hash"]):
                raise ValueError("scope evidence missing or corrupt")
            evidence = ResultScopeEvidence.model_validate_json(record["payload"])
            indexed = [row[0] for row in connection.execute(
                "SELECT household_id FROM result_scopes WHERE result_id=? ORDER BY household_id",
                (result_id,),
            )]
            if (evidence.result_id != result_id
                    or evidence.result_payload_hash != record["result_payload_hash"]
                    or evidence.action != record["action"]
                    or evidence.projection != record["projection"]
                    or evidence.household_ids != sorted(set(evidence.household_ids))
                    or evidence.household_ids != indexed):
                raise ValueError("scope metadata does not match original result")
            return evidence.household_ids
        except (ValueError, TypeError, KeyError):
            raise RequestFailure("INTERNAL", "Recorded result scope unavailable", True) from None

    def result_access(
        self, operation: Operation, connection: sqlite3.Connection, result_id: str,
    ) -> tuple[str, Literal["full", "no_pii"]]:
        access = connection.execute(
            "SELECT action,projection,metadata_version FROM result_access WHERE result_id=?",
            (result_id,),
        ).fetchone()
        if (access is None or access["metadata_version"] != 1
                or access["projection"] not in {"full", "no_pii"}):
            raise RequestFailure("INTERNAL", "Recorded access evidence unavailable", True)
        return access["action"], access["projection"]

    async def resolve_result(
        self, operation: Operation, connection: sqlite3.Connection, result_id: str,
    ) -> dict:
        await self.authorize(operation, connection, "result_preflight", [])
        metadata = connection.execute(
            "SELECT principal_id, conversation_id FROM results WHERE id=?", (result_id,),
        ).fetchone()
        if (not metadata or metadata["principal_id"] != operation.principal.id
                or not operation.conversation_id
                or metadata["conversation_id"] != operation.conversation_id):
            self.deny_locally(operation, "RESULT_OWNER_OR_CONVERSATION_MISMATCH")
        scopes = self.result_scopes(connection, result_id)
        action, projection = self.result_access(operation, connection, result_id)
        await self.authorize(operation, connection, action, scopes)
        if projection == "full" and operation.decisions[-1]["projection"] == "no_pii":
            self.deny_locally(operation, "ORIGIN_PROJECTION_NOT_REUSABLE")
        if projection == "no_pii":
            operation.projection = "no_pii"
        operation.scopes = sorted(set(operation.scopes) | set(scopes))
        row = connection.execute(
            "SELECT payload, payload_hash FROM results WHERE id=?", (result_id,),
        ).fetchone()
        if not row or digest(row["payload"]) != row["payload_hash"]:
            raise RequestFailure("INTERNAL", "Recorded result evidence unavailable", True)
        return json.loads(row["payload"])

    def start_conversation(self, principal: Principal, now: datetime) -> str:
        conversation = identifier("conv")
        with write_transaction(self.database) as connection:
            connection.execute("INSERT INTO conversations VALUES (?, ?, ?)",
                               (conversation, principal.id, utc_timestamp(now)))
            record_conversation(connection, conversation, principal.id, utc_timestamp(now))
        return conversation

    def authentication_error(self, now: datetime) -> dict:
        now = now.astimezone(UTC)
        with self.tracer.start_as_current_span("authenticate") as span:
            trace = format(span.get_span_context().trace_id, "032x")
            result_id = identifier("r")
            envelope = Envelope[dict[str, JsonValue]](
                trace_id=trace, result_id=result_id, status="ERROR", as_of=None,
                valuation_date=None, known_as_of=None, calculated_at=now,
                entitlement_revision=None, policy_revision=None, data=None,
                error=Error(code="UNAUTHENTICATED", message="Valid bearer token required",
                            retryable=False),
            ).model_dump(mode="json")
            payload = canonical(envelope)
            try:
                with write_transaction(self.database) as connection:
                    connection.execute(
                        "INSERT INTO results VALUES (?, ?, NULL, NULL, ?, ?, ?)",
                        (result_id, trace, payload, digest(payload), utc_timestamp(now)),
                    )
                    connection.execute("INSERT INTO result_access VALUES (?, ?, ?, 1)",
                                       (result_id, "error_result", "full"))
                    scope_payload = canonical(ResultScopeEvidence(
                        result_id=result_id, result_payload_hash=digest(payload),
                        action="error_result", projection="full", household_ids=[],
                    ).model_dump(mode="json"))
                    connection.execute("INSERT INTO result_scope_integrity VALUES (?, 1, ?, ?)",
                                       (result_id, scope_payload, digest(scope_payload)))
                    self.record_issuance(connection, ResultIssuanceEvidence(
                        result_id=result_id, result_payload_hash=digest(payload),
                        principal_id=None, conversation_id=None, provenance_handle_id=None,
                        source_ids=[],
                    ))
                    connection.execute(
                        "INSERT INTO decisions VALUES (?, NULL, 'authenticate', '[]', 0, "
                        "'UNAUTHENTICATED', NULL, NULL, ?, ?)",
                        (identifier("d"), trace, utc_timestamp(now)),
                    )
            except sqlite3.Error:
                # Authentication remains denied even when durable storage is unavailable.
                pass
            return envelope
