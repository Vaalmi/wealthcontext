"""Server-rendered local workbench over the governed service and verified harness."""

import json
import os
import sqlite3
from datetime import UTC
from itertools import groupby
from pathlib import Path
from typing import Any
from urllib.parse import parse_qs, urlencode

from fastapi import FastAPI, Request
from fastapi.responses import FileResponse, HTMLResponse, RedirectResponse, Response
from pydantic import ValidationError

from wealthcontext.chat_api import ProviderFactory, answer_chat
from wealthcontext.chat_contracts import ChatArguments
from wealthcontext.clock import Clock
from wealthcontext.context_service import ContextService
from wealthcontext.conversation import TOMBSTONE, ConversationManager
from wealthcontext.harness import run_answer
from wealthcontext.identity import PERSONAS, Principal
from wealthcontext.provider_contracts import HarnessLimits, HarnessRun, ProviderName
from wealthcontext.providers import provider_configuration
from wealthcontext.replay import ReplayProvider, ReplayRecording, load_recording
from wealthcontext.workbench_compliance import run_ids_only_in, span_children
from wealthcontext.workbench_contracts import SCHEMAS, metric_surfaces, published_mcp
from wealthcontext.workbench_operations import (
    WINDOW_FIRST_DAY,
    health_cell,
    health_grid,
    household_completeness,
)
from wealthcontext.workbench_render import (
    ROLE_LABELS,
    TEMPLATES,
    envelope_state,
    inspector_table,
    user_display,
)
from wealthcontext.workbench_revocation import (
    SEEDED_PRINCIPAL,
    SeededStore,
    SeededStores,
    SeededStoreUnavailable,
)
from wealthcontext.workbench_sessions import (
    COOKIE,
    BookQuery,
    ClaimSelection,
    CsrfForm,
    DataHealthQuery,
    DecisionQuery,
    DemoSession,
    DemoSessions,
    EvidenceSelection,
    FeedRunQuery,
    HouseholdSelection,
    MetricQuery,
    PointInTimeQuery,
    QuestionForm,
    SchemaQuery,
    SessionForm,
    TraceQuery,
    WorkbenchContext,
    action_roles,
    local_request,
    menu_for,
    valid_csrf,
)

_ROOT = Path(__file__).resolve().parents[2]
_STATIC = Path(__file__).parent / "static"
_REPLAY = _ROOT / "fixtures/replays/clean-household.openai.json"
# A7's follow-up turn (decision 12): one genuine recording of the abstaining turn.
_FOLLOWUP_REPLAY = _ROOT / "fixtures/replays/revocation-followup.anthropic.json"
# The follow-up is offered no data tool, as Q02 runs it: it can only abstain or cite the
# context it already has, which after the reassignment holds only tombstones.
FOLLOWUP_LIMITS = HarnessLimits(max_data_calls=0)
_SECURITY = {
    "Cache-Control": "no-store, max-age=0", "Pragma": "no-cache",
    "Content-Security-Policy": "default-src 'none'; script-src 'self'; style-src 'self'; "
        "img-src 'self'; font-src 'self'; connect-src 'self'; form-action 'self'; "
        "base-uri 'none'; frame-ancestors 'none'",
    # Chrome serializes a navigation POST's Origin as null under no-referrer.
    # Same-origin preserves the origin needed by our strict form CSRF check,
    # while withholding referrers from external origins.
    "X-Content-Type-Options": "nosniff", "Referrer-Policy": "same-origin",
}
# One served route per file; each has a row in policies/matrix.yaml.
ASSETS = {
    "workbench.css": "text/css", "workbench.js": "application/javascript",
    "favicon.svg": "image/svg+xml", "lockup.svg": "image/svg+xml",
    "apple-touch-icon.png": "image/png", "fonts/inter.woff2": "font/woff2",
    "fonts/jetbrains-mono.woff2": "font/woff2", "fonts/newsreader.woff2": "font/woff2",
}
# The drawer script asks for the evidence panel alone with this header; without it
# (no script, or a deep link) the same route answers with a whole page.
FRAGMENT_HEADER = "x-workbench-fragment"
# Identical for a pair this session never held, a stale or foreign one, and a revoked
# household: the page names no resource and carries no earlier figure.
DENIAL = "You are not authorized for the requested resource."
PRESET_CONTEXTS = {
    "clean": {"persona": "ADV-01", "household_id": "WC-HH-0001", "mode": "replay"},
    "reconciliation": {"persona": "ADV-03", "household_id": "WC-HH-0013", "mode": "live",
                       "as_of": "2026-10-01", "known_as_of": "2026-10-02T12:00:00Z"},
    "missing": {"persona": "ADV-03", "household_id": "WC-HH-0003", "mode": "live",
                "as_of": "2026-10-15"},
    "access": {"persona": "ADV-03", "household_id": "WC-HH-0004", "mode": "live"},
    "late_trade": {"persona": "COMP-01", "household_id": "WC-HH-0009", "mode": "live"},
    # Operations has no household: the context keeps the default, which no O page reads.
    "failed_feed": {"persona": "OPS-01", "mode": "live", "as_of": "2026-10-15"},
}
# The late-trade preset's comparison cutoff (ADR 0009 "Presets"): a page parameter of C2,
# never session context.
LATE_TRADE_COMPARISON = "2026-10-06T13:00:00Z"
# The failed-feed preset names a cell, never a run ID (ADR 0009 "Presets"): O2 asks
# `feed_health` for that cell and takes the run from the authorized result.
FAILED_FEED_CELL = {"source_system": "PAS-B", "feed": "pas_snapshots",
                    "business_date": "2026-10-15"}
# ADR 0009 "Presets": each opens its destination with the session context it sets, so
# the household pages need no query. The late-trade preset went live with C2 in step 5,
# the failed-feed preset with O2 in step 6.
PRESETS: list[dict[str, Any]] = [
    {"id": "clean", "label": "Clean household", "kind": "complete", "state": "Recorded replay",
     "question": "What assets are included for the clean household at quarter-end?",
     "summary": "A verified answer you can trace from the figure to the raw feed record.",
     "footer": "No API key needed", "destination": "/workbench"},
    {"id": "reconciliation", "label": "Settlement and reconciliation", "kind": "break",
     "state": "Break",
     "question": "Explain the reconciliation discrepancy for WC-HH-0013.",
     "summary": "A quantity break and a cash break on one household, each listed with its "
                "subject on the household's reconciliation tab.",
     "footer": "No API key needed", "destination": "/workbench/household/reconciliation"},
    {"id": "missing", "label": "Missing authoritative input", "kind": "incomplete",
     "state": "Incomplete",
     "question": "Explain the incomplete valuation for WC-HH-0003.",
     "summary": "A stale price and an account that was not received. See why there is no "
                "total, and what is shown instead.",
     "footer": "No API key needed", "destination": "/workbench/household"},
    {"id": "access", "label": "Current access", "kind": "denied", "state": "Access denied",
     "question": "Show the household snapshot for WC-HH-0004.",
     "summary": "A household moved to another advisor on 1 Oct. The earlier advisor gets a "
                "neutral denial, even for September.",
     "footer": "No API key needed", "destination": "/workbench/household"},
    {"id": "late_trade", "label": "Late trade (S09)", "kind": "info", "state": "Point in time",
     "question": "What was known about WC-HH-0009 on 6 Oct, and what is known now?",
     "summary": "A compliance view of one household at two knowledge cutoffs: each "
                "snapshot and the feed receipts behind it, side by side.",
     "footer": "No API key needed",
     "destination": "/workbench/point-in-time?" + urlencode(
         {"compare_known_as_of": LATE_TRADE_COMPARISON})},
    {"id": "failed_feed", "label": "Failed feed, seen by operations (S07)", "kind": "break",
     "state": "Feed failed",
     "question": "Which accounts and households did the failed PAS-B snapshot of 15 Oct "
                 "affect?",
     "summary": "One upstream feed failed. See the accounts it left unavailable, the "
                "households they belong to, and no name anywhere.",
     "footer": "No API key needed",
     "destination": "/workbench/feed-run?" + urlencode(FAILED_FEED_CELL)},
]
# The household page's tabs: each is one governed read at the session's cutoffs, and
# what the neutral denial names as requested. Holdings and reconciliation read the same
# snapshot; exposure reads issuer exposure (the brief's A2 reads).
HOUSEHOLD_TABS = {
    "/workbench/household": ("Holdings", "get_household_snapshot", "Household snapshot"),
    "/workbench/household/reconciliation": ("Reconciliation", "get_household_snapshot",
                                            "Household snapshot"),
    "/workbench/household/exposure": ("Exposure", "get_portfolio_exposure",
                                      "Household exposure"),
}
for _preset in PRESETS:
    _preset["context"] = WorkbenchContext.model_validate(
        {**PRESET_CONTEXTS[_preset["id"]], "preset": _preset["id"]})
INSPECTORS = {
    "snapshot": "Household snapshot", "quality": "Data quality",
    "conflicts": "Relationship conflicts", "feeds": "Feed runs",
    "decisions": "Authorization decisions", "eval": "Evaluation summary",
    "trace": "Trace and envelopes",
}
# The roles policy may allow a span tree (ADR 0009 section 7): only they see A3's link to
# their run's C3 (ADR 0009 point 13). Policy still decides when it is followed.
SPAN_ROLES = action_roles("trace_spans")


async def _form(request: Request) -> dict[str, str]:
    if request.headers.get("content-type", "").split(";")[0] != "application/x-www-form-urlencoded":
        raise ValueError("form encoding required")
    body = bytearray()
    async for chunk in request.stream():
        body.extend(chunk)
        if len(body) > 65536:
            raise ValueError("form too large")
    parsed = parse_qs(body.decode("utf-8"), keep_blank_values=True, strict_parsing=True,
                      max_num_fields=16)
    if any(len(values) != 1 for values in parsed.values()):
        raise ValueError("duplicate form fields")
    return {key: values[0] for key, values in parsed.items()}


def _query(request: Request) -> dict[str, str]:
    if len(request.query_params) != len(request.query_params.multi_items()):
        raise ValueError("duplicate parameters")
    return dict(request.query_params)


def _html(template: str, status: int = 200, **values) -> HTMLResponse:
    return HTMLResponse(TEMPLATES.get_template(template).render(**values),
                        status_code=status, headers=_SECURITY)


def _error(message: str, status: int = 400) -> HTMLResponse:
    return _html("error.html", status, title="Request unavailable", message=message,
                 denied=False)


def _denied() -> HTMLResponse:
    return _html("error.html", 403, title="Access denied", message=DENIAL, denied=True)


SESSION_ENDED = ("This demo session has ended or is not available here. Return to the local "
                 "demo and choose a persona or scenario to start a new one.")


def session_unavailable() -> HTMLResponse:
    """A page route without a current demo session: the restyled error page (build plan
    decision 4), not the API's JSON envelope. The caller has already audited the failed
    authentication. The page is the same for a missing, expired or foreign session."""
    response = _html("error.html", 401, title="Session unavailable", message=SESSION_ENDED,
                     denied=False)
    response.headers["WWW-Authenticate"] = "Bearer"
    return response


def _redirect(path: str = "/workbench") -> RedirectResponse:
    return RedirectResponse(path, status_code=303, headers=_SECURITY)


def run_metadata(run: HarnessRun) -> dict:
    """What the page states about a run: the harness's own fields, nothing derived.
    `result_id` names the answer the run produced, so its card can show the attempt."""
    final = run.final_result if run.final_valid else None
    return {"run_id": run.run_id, "mode": run.mode, "model": run.model,
            "first_pass_valid": run.first_pass_valid, "final_valid": run.final_valid,
            "retries": run.retries, "fallback": run.fallback, "failure": run.failure,
            "data_tool_calls": run.data_tool_calls,
            "result_id": final.get("result_id") if isinstance(final, dict) else None}


def attempt(metadata: dict) -> dict | None:
    """The verifier path the answer took: first pass, one retry, or the fallback."""
    if not metadata.get("final_valid"):
        return None
    if metadata.get("fallback"):
        return {"kind": "info", "label": "Deterministic fallback", "text": (
            "The model's submission did not pass verification, so the platform submitted "
            "its own conservative answer from the current results. It passed the same "
            "verifier.")}
    if metadata.get("retries"):
        return {"kind": "incomplete", "label": "Verified after one retry", "text": (
            "The first submission failed verification. The model corrected it once, using "
            "the verifier's diagnostics, and the correction passed.")}
    return {"kind": "complete", "label": "First pass", "text": (
        "The model's first submission passed verification, with no retry and no fallback.")}


def tool_badge(name: str, state: str | None) -> tuple[str, str]:
    """A tool call's outcome as a badge kind and word, from its paired result."""
    if state == "TOMBSTONE":
        return "denied", "Redacted: not authorized"
    if name == "submit_answer":
        return {"COMPLETE": ("complete", "Verified"),
                "INVALID_ARGUMENT": ("break", "Rejected by the verifier")}.get(
                    str(state), ("neutral", str(state)))
    if state is None:
        return "neutral", "No result"
    return {"COMPLETE": ("complete", "Complete"), "INCOMPLETE": ("incomplete", "Incomplete")}.get(
        state, ("break", state))


def load_followup(path: Path) -> tuple[ReplayRecording, str]:
    """The genuine recording of A7's follow-up: Q02's principal, no data tool offered, and
    the staged earlier question followed by the follow-up question."""
    recording = load_recording(path)
    run = recording.run
    opening = run.turns[0].request
    users = [message.get("content") for message in opening.messages
             if message.get("role") == "user"]
    if ((run.principal_id, run.principal_role) != (SEEDED_PRINCIPAL.id, SEEDED_PRINCIPAL.role)
            or run.data_tool_calls != 0 or [tool.name for tool in opening.tools]
            != ["submit_answer"] or len(users) != 2 or not isinstance(users[-1], str)):
        raise ValueError("not the recorded seeded revocation follow-up")
    return recording, users[-1]


def _providers() -> list[dict]:
    keys: dict[ProviderName, str] = {"anthropic": "ANTHROPIC_API_KEY", "openai": "OPENAI_API_KEY",
                                    "google": "GEMINI_API_KEY"}
    labels = {"anthropic": "Anthropic", "openai": "OpenAI", "google": "Google"}
    return [{"id": name, "label": labels[name],
             "model": provider_configuration(name)[0], "available": bool(os.environ.get(key))}
            for name, key in keys.items()]


class Workbench:
    def __init__(self, app: FastAPI, clock: Clock, provider_factory: ProviderFactory):
        self.app, self.clock, self.provider_factory = app, clock, provider_factory
        self.seeded = SeededStores()
        # A session that leaves the table, however it leaves, takes its A7 store with it.
        self.sessions = DemoSessions(on_drop=lambda session: self.seeded.release(session.id))
        app.state.workbench_sessions = self.sessions
        self.followup: ReplayRecording | None = None
        self.mcp: dict | None = None  # E2's published MCP list, read once on first use.
        self.followup_question = ""
        try:
            self.followup, self.followup_question = load_followup(_FOLLOWUP_REPLAY)
        except (ValueError, OSError):
            pass  # A7 then shows the tombstones and says the recorded follow-up is missing.
        try:
            self.recording = load_recording(_REPLAY)
            first = self.recording.run.turns[0].request.messages
            content = first[0].get("content") if len(first) == 1 else None
            if not isinstance(content, str) or first[0].get("role") != "user":
                raise ValueError("recording requires prior history")
            self.recorded_question = content
            self.replay_title = self.recorded_question.splitlines()[0]
        except (ValueError, OSError):
            self.recording, self.recorded_question = None, ""
            self.replay_title = "Clean household recording unavailable"

    def current(self, session: DemoSession) -> bool:
        return self.sessions.get(session.id, self.clock.now()) is session

    def replay_matches(self, session: DemoSession) -> bool:
        context = session.context
        return bool(self.recording and context.persona == "ADV-01"
                    and self.recording.run.principal_id == context.persona
                    and context.provider == self.recording.run.provider
                    and context.household_id == "WC-HH-0001"
                    and context.as_of.isoformat() == "2026-09-30"
                    and context.known_as_of.astimezone(UTC).isoformat()
                    == "2026-10-31T16:00:00+00:00")

    def shell(self, session: DemoSession, active: str | None, **values) -> dict:
        """What every page's top bar, context form and menu need: no business data."""
        context = session.context
        role = PERSONAS[context.persona] if session.token else None
        menu = menu_for(role)
        return {
            "session": session, "context": context, "personas": PERSONAS,
            "role_labels": ROLE_LABELS, "role": role, "menu": menu, "active": active,
            "menu_groups": [(group, list(items))
                            for group, items in groupby(menu, key=lambda item: item.group)],
            "providers": _providers(), "presets": PRESETS,
            "history_view": context.known_as_of < self.clock.now(),
            "known_as_of": (context.known_as_of.astimezone(UTC)
                            .isoformat().replace("+00:00", "Z")),
            **values,
        }

    async def history(self, request: Request, session: DemoSession) -> tuple[
        list[dict], dict[str, dict], str | None,
    ]:
        if not session.conversation_id:
            return [], {}, None
        return await self.conversation(self.app.state.service, request.state.principal,
                                       session.conversation_id)

    async def conversation(self, service: ContextService, principal: Principal,
                           conversation_id: str) -> tuple[list[dict], dict[str, dict], str | None]:
        """The freshly reauthorized conversation as page rows: each question, the tool calls
        that followed it with their outcome, and each answer. A revoked result is its
        paired tombstone; nothing else about it is shown."""
        rows: list[dict] = []
        envelopes: dict[str, dict] = {}
        view = await ConversationManager(service).render_history(
            principal, self.clock.now(), conversation_id,
        )
        if view["status"] == "ERROR":
            return [], {}, view["error"]["message"]
        answers = iter(view["data"]["answers"])
        calls: dict[str, dict] = {}
        # Every call of one run shares the run's trace (the harness's agent span), so an
        # answer's trace is the one its turn's tool results carry.
        turn_trace: str | None = None
        for message in view["data"]["messages"]:
            if message["role"] == "user":
                rows.append(user_display(message["content"]))
                turn_trace = None
            elif message["role"] == "assistant" and "content" in message:
                rows.append({"role": "assistant", **next(answers), "trace_id": turn_trace})
            elif message["role"] == "assistant":
                group = [{"name": call["name"], "state": None,
                          "badge": tool_badge(call["name"], None)}
                         for call in message.get("tool_calls", [])]
                calls.update({call["id"]: item
                              for call, item in zip(message.get("tool_calls", []), group,
                                                    strict=True)})
                rows.append({"role": "tools", "calls": group})
            elif message["role"] == "tool":
                call = calls.get(message.get("tool_call_id", ""))
                state = "TOMBSTONE" if message["content"] == TOMBSTONE else None
                try:
                    envelope = json.loads(message["content"])
                except (ValueError, TypeError):
                    envelope = None  # Paired authorization tombstones contain no JSON.
                if isinstance(envelope, dict) and isinstance(envelope.get("result_id"), str):
                    envelopes[envelope["result_id"]] = envelope
                    state = envelope_state(envelope)
                    if isinstance(envelope.get("trace_id"), str):
                        turn_trace = envelope["trace_id"]
                if call is not None:
                    call.update(state=state, badge=tool_badge(call["name"], state))
        return rows, envelopes, None

    async def landing(self, session: DemoSession) -> Response:
        if not self.current(session):
            return _redirect("/")
        return _html("welcome.html", **self.shell(session, None, landing=True))

    async def workspace(self, request: Request, session: DemoSession,
                        selection: ClaimSelection | None = None) -> Response:
        if not self.current(session):
            return _redirect("/")
        history, envelopes, error = await self.history(request, session)
        drawer = None
        if selection and not error:
            answer = next((row for row in history if row.get("result_id") == selection.answer
                           and not row.get("redacted")), None)
            claims = (answer.get("data") or {}).get("claims", []) if answer else []
            selected = next((claim for claim in claims if claim["id"] == selection.claim), None)
            inspected = envelopes.get(selected["result_id"]) if selected else None
            if selected is None or inspected is None:
                return _error("That verified figure is unavailable in this session.", 403)
            references = inspected.get("source_refs", [])
            reference = selection.reference or (references[0] if references else None)
            provenance = None
            if reference is not None:
                if reference not in references:
                    return _error("That source is unavailable for this verified figure.", 403)
                provenance = await self.app.state.service.execute(
                    request.state.principal, self.clock.now(), "get_data_provenance",
                    {"reference": reference}, session.conversation_id,
                )
                # A revoked or unavailable source must not leave an older figure on screen.
                if provenance["status"] == "ERROR":
                    return _error(provenance["error"]["message"], 403)
            drawer = {"selected": selected, "inspected": inspected, "provenance": provenance,
                      "reference": reference, "close": "/workbench",
                      "links": [(item, TEMPLATES.globals["claim_link"](
                          selection.answer, selection.claim, item)) for item in references]}
        if not self.current(session):
            # Persona/cutoff changes during a run cannot redisclose the old view.
            return _redirect()
        tour = session.context.preset == "clean" and self.replay_matches(session) and (
            session.context.mode == "replay")
        for row in history:
            if row.get("result_id") and row["result_id"] == session.run_metadata.get(
                    "result_id") and not row.get("redacted"):
                row["attempt"] = attempt(session.run_metadata)
        if drawer is not None and FRAGMENT_HEADER in request.headers:
            response = _html("partials/drawer.html", drawer=drawer, tour=tour)
            response.headers[FRAGMENT_HEADER] = "drawer"
            return response
        return _html("workspace.html", **self.shell(
            session, "/workbench", history=history, error=error, drawer=drawer, tour=tour,
            trace_links=PERSONAS[session.context.persona] in SPAN_ROLES,
            replay_title=self.replay_title, replay_matches=self.replay_matches(session),
            question_preset=next(preset["question"] for preset in PRESETS
                                 if preset["id"] == session.context.preset),
        ))

    async def inspector(self, request: Request, session: DemoSession, surface: str) -> Response:
        if not self.current(session):
            return _redirect("/")
        context = session.context
        cutoffs = {"as_of": context.as_of.isoformat(),
                   "known_as_of": context.known_as_of.isoformat()}
        error, inspected = None, None
        arguments: dict = {
            "snapshot": {**cutoffs, "household_id": context.household_id},
            "quality": cutoffs, "conflicts": {**cutoffs, "household_id": context.household_id},
            "feeds": cutoffs, "decisions": {}, "eval": {},
        }.get(surface, {})
        if surface == "trace":
            _, envelopes, error = await self.history(request, session)
            latest = next(reversed(envelopes.values()), None) if envelopes else None
            if latest is None and error is None:
                error = "Run a verified answer before opening its trace."
            elif latest is not None:
                arguments = {"trace_id": latest.get("trace_id")}
        if error is None:
            tool = {"snapshot": "get_household_snapshot", "quality": "get_data_quality_report",
                    "conflicts": "conflicts", "feeds": "feed_runs", "decisions": "decision_log",
                    "eval": "eval_reports", "trace": "trace"}[surface]
            inspected = await self.app.state.service.execute(
                request.state.principal, request.state.request_now, tool, arguments,
                session.conversation_id,
            )
        if not self.current(session):
            return _redirect()
        if inspected is not None and inspected.get("error") is None:
            # Identifiers only: the pair lets this session reopen the handle through
            # /workbench/evidence, which re-authorizes it on every open.
            session.issue(inspected["result_id"], inspected.get("source_refs", []),
                          f"/workbench/inspector/{surface}")
        columns, rows = inspector_table(inspected)
        return _html("inspector.html", **self.shell(
            session, f"/workbench/inspector/{surface}", title=INSPECTORS[surface],
            inspected=inspected, error=error, columns=columns, rows=rows, denial=DENIAL,
        ))

    def home(self, session: DemoSession) -> str:
        """A persona's home: the first page the matrix menu lists for its role."""
        menu = menu_for(PERSONAS[session.context.persona])
        return menu[0].path if menu else "/workbench"

    @staticmethod
    def cutoffs(session: DemoSession) -> dict[str, str]:
        context = session.context
        return {"as_of": context.as_of.isoformat(),
                "known_as_of": context.known_as_of.isoformat()}

    async def governed(self, request: Request, tool: str, arguments: dict) -> dict:
        """One governed read for a data page. The service authorizes, records and returns
        the envelope; a data page is not conversation context, so none is passed."""
        return await self.app.state.service.execute(
            request.state.principal, request.state.request_now, tool, arguments)

    def denial(self, session: DemoSession, active: str, *, heading: str,
               requested: str | None, view: str, envelope: dict) -> HTMLResponse:
        """A5: one neutral page, the same whether or not the resource exists. It names
        what was asked for and the request's trace, never a name, a figure or an owner."""
        return _html("denied.html", 403, **self.shell(
            session, active, heading=heading, requested=requested, requested_view=view,
            envelope=envelope, denial=DENIAL,
            # ADR 0009 "Where the mocks differ", point 4: A7 is entered from the "Current
            # access" scenario by a link, with no preset of its own. The link depends on
            # the scenario and persona only, never on the household asked for.
            revocation_link=(session.context.preset == "access"
                             and session.context.persona == SEEDED_PRINCIPAL.id)))

    async def book(self, request: Request, session: DemoSession) -> Response:
        """A1 and A1b: `list_households`, then one snapshot per listed household at the
        session's cutoffs, then book concentration. Nothing is summed: there is no book
        figure, and every count shown is a field one of these results returned."""
        if not self.current(session):
            return _redirect("/")
        try:
            query = BookQuery.model_validate(_query(request))
        except (ValueError, ValidationError):
            return _error("Enter a concentration threshold using the supplied form.")
        cutoffs = self.cutoffs(session)
        listed = await self.governed(request, "list_households", {})
        rows: list[tuple[dict, dict]] = []
        concentration = None
        if listed["error"] is None:
            for item in listed["data"]["items"]:
                rows.append((item, await self.governed(request, "get_household_snapshot", {
                    **cutoffs, "household_id": item["household_id"]})))
            threshold = {} if query.threshold is None else {"threshold_pct": query.threshold}
            concentration = await self.governed(request, "get_concentrated_positions",
                                                {**cutoffs, **threshold})
        if not self.current(session):
            return _redirect()
        if listed["error"] is not None and listed["error"]["code"] == "NOT_AUTHORIZED":
            return self.denial(session, "/workbench/book", heading="My book", requested=None,
                               view="The households you are entitled to", envelope=listed)
        page = "/workbench/book" + ("" if query.threshold is None
                                    else "?" + urlencode({"threshold": query.threshold}))
        for result in (listed, *(snapshot for _, snapshot in rows), concentration):
            if result is not None and result["error"] is None:
                session.issue(result["result_id"], result["source_refs"], page)
        return _html("book.html", **self.shell(
            session, "/workbench/book", listed=listed, rows=rows, concentration=concentration,
            names={item["household_id"]: item["household_name"] for item, _ in rows},
            threshold=query.threshold))

    async def household(self, request: Request, session: DemoSession, path: str) -> Response:
        """A2 holdings, the A6 reconciliation tab and the A4 incomplete state: one governed
        snapshot at the session's cutoffs. A refusal is the A5 denial, before any figure."""
        if not self.current(session):
            return _redirect("/")
        try:
            selection = HouseholdSelection.model_validate(_query(request))
        except (ValueError, ValidationError):
            return _error("Invalid household reference.")
        household_id = selection.household_id or session.context.household_id
        _, tool, view = HOUSEHOLD_TABS[path]
        result = await self.governed(request, tool, {
            **self.cutoffs(session), "household_id": household_id})
        if not self.current(session):
            return _redirect()
        error = result["error"]
        if error is not None and error["code"] == "NOT_AUTHORIZED":
            return self.denial(session, path, heading="Household", requested=household_id,
                               view=view, envelope=result)
        query = "?" + urlencode({"household_id": household_id})
        if error is None:
            session.issue(result["result_id"], result["source_refs"], path + query)
        return _html("household.html", **self.shell(
            session, path, section="/workbench/household", result=result,
            household_id=household_id, tab=path.rsplit("/", 1)[1],
            tabs=[(tab + query, label, tab == path)
                  for tab, (label, _, _) in HOUSEHOLD_TABS.items()]))

    async def catalog(self, request: Request, session: DemoSession) -> dict:
        """The governed metric catalog: E1's registry and E2's policy decision. Its
        `metric_definition` action admits all four roles; a refusal still comes first."""
        catalog = await self.governed(request, "get_metric_definition", {"metric_id": "catalog"})
        if catalog["error"] is None:
            session.issue(catalog["result_id"], catalog["source_refs"], "/workbench/metrics")
        return catalog

    async def metrics(self, request: Request, session: DemoSession) -> Response:
        """E1: the governed catalog, then the governed definition of one metric (the first
        the catalog lists, unless the query names one). Definitions only: no household,
        figure, trace or span is read."""
        if not self.current(session):
            return _redirect("/")
        try:
            query = MetricQuery.model_validate(_query(request))
        except (ValueError, ValidationError):
            return _error("Choose a metric from the registry.")
        catalog = await self.catalog(request, session)
        definition = None
        if catalog["error"] is None:
            items = catalog["data"]["items"]
            chosen = query.metric_id or (items[0]["id"] if items else None)
            if chosen is not None:
                definition = await self.governed(request, "get_metric_definition",
                                                 {"metric_id": chosen})
        if not self.current(session):
            return _redirect()
        refused = next((result for result in (catalog, definition) if result is not None
                        and result["error"] is not None
                        and result["error"]["code"] == "NOT_AUTHORIZED"), None)
        if refused is not None:
            # The same page whichever metric was asked for: the catalog is refused first.
            return self.denial(session, "/workbench/metrics", heading="Metrics", requested=None,
                               view="Metric definitions", envelope=refused)
        if definition is not None and definition["error"] is None:
            session.issue(definition["result_id"], definition["source_refs"],
                          "/workbench/metrics")
        missing = definition is not None and definition["error"] is not None and (
            definition["error"]["code"] == "NOT_FOUND")
        return _html("metrics.html", 404 if missing else 200, **self.shell(
            session, "/workbench/metrics", catalog=catalog, definition=definition,
            surfaces=metric_surfaces()))

    async def contracts(self, request: Request, session: DemoSession) -> Response:
        """E2: the published JSON Schemas and the MCP list, after the governed catalog read
        that the page's `metric_definition` row names. No schema is looked up before it."""
        if not self.current(session):
            return _redirect("/")
        try:
            query = SchemaQuery.model_validate(_query(request))
        except (ValueError, ValidationError):
            return _error("Choose a schema from the list.")
        catalog = await self.catalog(request, session)
        if not self.current(session):
            return _redirect()
        if catalog["error"] is not None and catalog["error"]["code"] == "NOT_AUTHORIZED":
            return self.denial(session, "/workbench/contracts", heading="Tools and schemas",
                               requested=None, view="Published contracts", envelope=catalog)
        if catalog["error"] is not None:
            # Any other refusal (a policy outage, say) also shows no contract.
            return _html("contracts.html", **self.shell(
                session, "/workbench/contracts", catalog=catalog, mcp=None, schemas=[],
                selected=None, missing=False))
        if self.mcp is None:
            self.mcp = await published_mcp()
        selected = SCHEMAS.get(query.schema_id) if query.schema_id is not None else None
        missing = query.schema_id is not None and selected is None
        return _html("contracts.html", 404 if missing else 200, **self.shell(
            session, "/workbench/contracts", catalog=catalog, mcp=self.mcp,
            schemas=list(SCHEMAS.values()), selected=selected, missing=missing))

    @staticmethod
    def refused(result: dict | None) -> bool:
        return bool(result and result["error"] is not None
                    and result["error"]["code"] == "NOT_AUTHORIZED")

    async def decisions(self, request: Request, session: DemoSession) -> Response:
        """C1: one governed `decision_log_page`. The filters and cursor pass to the platform
        unread; it decides first, then decodes the cursor. Every count is returned."""
        if not self.current(session):
            return _redirect("/")
        try:
            query = DecisionQuery.model_validate(_query(request))
        except (ValueError, ValidationError):
            return _error("Use the filter form to choose decisions.")
        page = await self.governed(request, "decision_log_page",
                                   query.model_dump(mode="json", exclude_none=True))
        if not self.current(session):
            return _redirect()
        if self.refused(page):
            return self.denial(session, "/workbench/decisions", heading="Access decisions",
                               requested=None, view="The authorization decision log",
                               envelope=page)
        filters = query.filters()
        data = page["data"] if page["error"] is None else None
        next_link = ("/workbench/decisions?" + urlencode({**filters,
                                                          "cursor": data["next_cursor"]})
                     if data and data["next_cursor"] else None)
        first_link = "/workbench/decisions" + ("?" + urlencode(filters) if filters else "")
        form_instants = {key: (value.astimezone(UTC).strftime("%Y-%m-%dT%H:%M:%S")
                               if value is not None else "")
                         for key, value in (("recorded_from", query.recorded_from),
                                            ("recorded_before", query.recorded_before))}
        return _html("decisions.html", **self.shell(
            session, "/workbench/decisions", page=page, data=data, query=query,
            form_instants=form_instants,
            next_link=next_link, first_link=first_link, browsing=query.cursor is not None))

    async def point_in_time(self, request: Request, session: DemoSession) -> Response:
        """C2: one household at two knowledge cutoffs, side by side: the session's and the
        page's comparison cutoff. Each column is a snapshot and a receipts list, each its
        own governed result. The lists are compared by run ID; no figure is differenced."""
        if not self.current(session):
            return _redirect("/")
        try:
            query = PointInTimeQuery.model_validate(_query(request))
        except (ValueError, ValidationError):
            return _error("Enter a timezone-aware comparison cutoff using the supplied form.")
        context = session.context
        household_id = query.household_id or context.household_id
        cutoffs = [("Session cutoff", context.known_as_of)]
        if query.compare_known_as_of is not None:
            cutoffs.append(("Comparison cutoff", query.compare_known_as_of))
        columns = []
        for label, known in cutoffs:
            arguments = {"household_id": household_id, "as_of": context.as_of.isoformat(),
                         "known_as_of": known.isoformat()}
            receipts = await self.governed(request, "household_receipts", arguments)
            if self.refused(receipts):
                # The page's own action is refused: no snapshot is read for it.
                if not self.current(session):
                    return _redirect()
                return self.denial(session, "/workbench/point-in-time", heading="Point in time",
                                   requested=household_id,
                                   view="Receipts and snapshots at two knowledge cutoffs",
                                   envelope=receipts)
            snapshot = await self.governed(request, "get_household_snapshot", arguments)
            columns.append({"label": label, "known_as_of": known, "receipts": receipts,
                            "snapshot": snapshot})
        if not self.current(session):
            return _redirect()
        page = "/workbench/point-in-time?" + urlencode(
            {"household_id": household_id, **({"compare_known_as_of": query.compare_known_as_of
                                               .isoformat().replace("+00:00", "Z")}
                                              if query.compare_known_as_of else {})})
        for column in columns:
            for result in (column["receipts"], column["snapshot"]):
                if result["error"] is None:
                    session.issue(result["result_id"], result["source_refs"], page)
        if len(columns) == 2:
            first, second = columns
            first["only"] = run_ids_only_in(first["receipts"], second["receipts"])
            second["only"] = run_ids_only_in(second["receipts"], first["receipts"])
        return _html("point_in_time.html", **self.shell(
            session, "/workbench/point-in-time", household_id=household_id, columns=columns,
            compare=query.compare_known_as_of,
            compare_value=(query.compare_known_as_of.astimezone(UTC).strftime(
                "%Y-%m-%dT%H:%M:%S") if query.compare_known_as_of else "")))

    async def trace_page(self, request: Request, session: DemoSession) -> Response:
        """C3: the governed span tree, then today's `trace` metadata for the action names.
        Without a trace ID the page asks for one and reads nothing."""
        if not self.current(session):
            return _redirect("/")
        try:
            query = TraceQuery.model_validate(_query(request))
        except (ValueError, ValidationError):
            return _error("Enter a trace ID using the supplied form.")
        spans = metadata = None
        if query.trace_id is not None:
            spans = await self.governed(request, "trace_spans", {"trace_id": query.trace_id})
            if spans["error"] is None:
                metadata = await self.governed(request, "trace",
                                               {"trace_id": spans["data"]["trace_id"]})
        if not self.current(session):
            return _redirect()
        if self.refused(spans):
            assert spans is not None
            return self.denial(session, "/workbench/trace", heading="Trace",
                               requested=query.trace_id, view="The trace's span tree",
                               envelope=spans)
        roots, children = span_children(spans["data"]["items"]) if (
            spans and spans["error"] is None) else ([], {})
        return _html("trace.html", **self.shell(
            session, "/workbench/trace", query=query, spans=spans, metadata=metadata,
            roots=roots, children=children))

    async def data_health(self, request: Request, session: DemoSession) -> Response:
        """O1: one governed `feed_health` over the range, then the quality report's
        `incomplete_household_count` as the only tile. Every expected cell is drawn, with
        a glyph and a word, and the same cells as a table. Nothing is counted here."""
        if not self.current(session):
            return _redirect("/")
        try:
            query = DataHealthQuery.model_validate(_query(request))
        except (ValueError, ValidationError):
            return _error("Choose the range using the supplied form.")
        context = session.context
        date_from = query.date_from or WINDOW_FIRST_DAY
        date_to = query.date_to or context.as_of
        health = await self.governed(request, "feed_health", {
            "date_from": date_from.isoformat(), "date_to": date_to.isoformat(),
            "known_as_of": context.known_as_of.isoformat()})
        quality = None
        if health["error"] is None:
            quality = await self.governed(request, "get_data_quality_report", {
                "as_of": health["valuation_date"],
                "known_as_of": context.known_as_of.isoformat()})
        if not self.current(session):
            return _redirect()
        if self.refused(health):
            return self.denial(session, "/workbench/data-health", heading="Data health",
                               requested=None, view="Feed health over a date range",
                               envelope=health)
        page = "/workbench/data-health?" + urlencode(
            {"date_from": date_from.isoformat(), "date_to": date_to.isoformat(),
             "view": query.view})
        if quality is not None and quality["error"] is None:
            session.issue(quality["result_id"], quality["source_refs"], page)
        data = health["data"] if health["error"] is None else None
        return _html("data_health.html", **self.shell(
            session, "/workbench/data-health", health=health, data=data, quality=quality,
            rows=health_grid(data) if data else [], query=query, date_from=date_from,
            date_to=date_to,
            view_links={view: "/workbench/data-health?" + urlencode(
                {"date_from": date_from.isoformat(), "date_to": date_to.isoformat(),
                 "view": view}) for view in ("grid", "table")}))

    async def feed_run(self, request: Request, session: DemoSession) -> Response:
        """O2: one governed `feed_run_impact`, then the quality report at the run's business
        date for each affected household's completeness. A preset names a cell: the run ID
        comes from that cell of an authorized `feed_health`, never from the preset."""
        if not self.current(session):
            return _redirect("/")
        try:
            query = FeedRunQuery.model_validate(_query(request))
        except (ValueError, ValidationError):
            return _error("Open a feed run from Data health, or enter its run ID.")
        context = session.context
        known = context.known_as_of.isoformat()
        health = impact = quality = cell = None
        run_id = query.run_id
        selected = query.cell()
        if selected is not None:
            health = await self.governed(request, "feed_health", {
                "date_from": selected["business_date"], "date_to": selected["business_date"],
                "known_as_of": known})
            if health["error"] is None:
                cell = health_cell(health["data"], selected["source_system"], selected["feed"])
                run_id = cell["run_id"] if cell is not None else None
        if run_id is not None and (health is None or health["error"] is None):
            impact = await self.governed(request, "feed_run_impact",
                                         {"run_id": run_id, "known_as_of": known})
            data = impact["data"] if impact["error"] is None else None
            if data and data["affected_households"] and data["affected_households"]["items"]:
                quality = await self.governed(request, "get_data_quality_report", {
                    "as_of": data["business_date"], "known_as_of": known})
        if not self.current(session):
            return _redirect()
        for result in (health, impact):
            if self.refused(result):
                assert result is not None
                return self.denial(
                    session, "/workbench/feed-run", heading="Feed run",
                    requested=query.run_id or (" ".join(selected.values()) if selected else None),
                    view="One feed run's impact", envelope=result)
        page = "/workbench/feed-run?" + urlencode(
            selected if selected is not None else ({"run_id": run_id} if run_id else {}))
        for result in (impact, quality):
            if result is not None and result["error"] is None:
                session.issue(result["result_id"], result["source_refs"], page)
        data = impact["data"] if impact and impact["error"] is None else None
        households = (household_completeness(
            quality, [item["household_id"] for item in data["affected_households"]["items"]])
            if data and data["affected_households"] else [])
        return _html("feed_run.html", **self.shell(
            session, "/workbench/feed-run", query=query, selected=selected, health=health,
            cell=cell, impact=impact, data=data, quality=quality, households=households))

    async def evidence(self, request: Request, session: DemoSession,
                       selection: EvidenceSelection) -> Response:
        """Data-page evidence, in the order ADR 0009 section 2 sets."""
        if not self.current(session):
            return _redirect("/")
        # 1. In memory, before any storage read: was this pair issued to this session?
        if not session.holds(selection.result, selection.source):
            return _denied()
        # 2. The governed provenance call re-authorizes the handle on every open.
        provenance = await self.app.state.service.execute(
            request.state.principal, request.state.request_now, "get_data_provenance",
            {"reference": selection.source},
        )
        # 3. Again before rendering: a context applied meanwhile discards the response.
        if not self.current(session) or not session.holds(selection.result, selection.source):
            return _redirect()
        if provenance["status"] == "ERROR":
            if provenance["error"]["code"] == "NOT_AUTHORIZED":
                return _denied()
            return _error(provenance["error"]["message"], 403)
        drawer = {"selected": None, "inspected": None, "provenance": provenance,
                  "reference": selection.source, "result": selection.result,
                  "close": session.evidence[(selection.result, selection.source)],
                  "links": [(handle, TEMPLATES.globals["evidence_link"](result, handle))
                            for result, handle in session.evidence
                            if result == selection.result]}
        if FRAGMENT_HEADER in request.headers:
            response = _html("partials/drawer.html", drawer=drawer, tour=False)
            response.headers[FRAGMENT_HEADER] = "drawer"
            return response
        return _html("evidence.html", **self.shell(session, None, drawer=drawer, tour=False))

    def seeded_store(self, session: DemoSession) -> SeededStore | None:
        """This session's A7 store, if it still is this session's."""
        return self.seeded.get(session.id) if self.current(session) else None

    async def revocation(self, request: Request, session: DemoSession) -> Response:
        """A7: Q02's staged conversation in this session's own seeded store. The earlier
        turn renders as paired tombstones; the follow-up runs once, offered no data tool."""
        if not self.current(session):
            return _redirect("/")
        principal: Principal = request.state.principal
        if (principal.id, principal.role) != (SEEDED_PRINCIPAL.id, SEEDED_PRINCIPAL.role):
            # The seeded conversation is ADV-03's. Nothing is staged or read for anyone else.
            return _html("revocation.html", **self.shell(
                session, None, section="/workbench", store=None, history=[], error=None,
                seeded_persona=SEEDED_PRINCIPAL.id))
        service = self.app.state.service
        try:
            store = await self.seeded.open(session.id, service.policy, service.tracer,
                                           self.clock)
        except SeededStoreUnavailable as unavailable:  # At the cap, every copy in use.
            return _error(str(unavailable), 503)
        except (ValueError, OSError, sqlite3.Error):  # A failed staging is a SetupFailure.
            return _error("The seeded revocation session could not be prepared.", 503)
        if not self.current(session):
            self.seeded.release(session.id)  # Replaced while it was staged: drop it.
            return _redirect()
        # Held until the read ends: a release meanwhile deletes the store afterwards.
        with self.seeded.lease(store):
            rows, _, error = await self.conversation(store.service, principal,
                                                     store.seed.conversation_id)
        if self.seeded_store(session) is not store:
            return _redirect()
        followup = store.followup or {}
        questions = [row for row in rows if row["role"] == "user"]
        for row, label in zip(questions, ("Earlier, before the reassignment", "Now"),
                              strict=False):
            row["label"] = label
        for row in rows:
            data = row.get("data") or {}
            if (row.get("result_id") and row["result_id"] == followup.get("result_id")
                    and not row.get("redacted")):
                row["attempt"] = attempt(followup)
                # Abstained: a verified answer with no claim, predicate or refusal, from a
                # turn that was offered no data tool and called none.
                row["abstained"] = (not data.get("claims") and not data.get("predicates")
                                    and data.get("refusal") is None
                                    and followup.get("data_tool_calls") == 0)
        return _html("revocation.html", **self.shell(
            session, None, section="/workbench", store=store, history=rows, error=error,
            seeded_persona=SEEDED_PRINCIPAL.id, followup=followup,
            followup_available=self.followup is not None,
            followup_question=self.followup_question,
            max_data_calls=FOLLOWUP_LIMITS.max_data_calls))

    async def revocation_followup(self, request: Request, session: DemoSession) -> Response:
        """The recorded follow-up, replayed without a key against fresh reauthorized context.
        It runs once per seeded store: a second run would no longer match the recording."""
        store = self.seeded_store(session)
        principal: Principal = request.state.principal
        if store is None or (principal.id, principal.role) != (
                SEEDED_PRINCIPAL.id, SEEDED_PRINCIPAL.role):
            return _redirect("/workbench/revocation")
        if self.followup is None:
            return _error("The recorded follow-up is not installed.", 422)
        if store.busy or store.followup is not None:
            return _error("The follow-up has already run in this seeded session.", 409)
        store.busy = True
        try:
            with self.seeded.lease(store):
                run = await run_answer(
                    store.service, ConversationManager(store.service), principal,
                    self.clock, self.followup_question,
                    ReplayProvider(self.followup, principal=principal),
                    conversation_id=store.seed.conversation_id, mode="replay",
                    limits=FOLLOWUP_LIMITS,
                    expected_prompt_sha256=self.followup.run.prompt_sha256)
            store.followup = run_metadata(run)
        except (ValueError, OSError):
            store.followup = {"final_valid": False,
                              "failure": "Replay contract or recording is unavailable."}
        finally:
            store.busy = False
        return _redirect("/workbench/revocation")

    async def revocation_evidence(self, request: Request, session: DemoSession,
                                  selection: EvidenceSelection) -> Response:
        """The earlier turn's source handles, in the order ADR 0009 section 2 sets: the
        pair must be the earlier turn's, in memory, before anything is read; then the
        governed provenance call reauthorizes it in this session's store."""
        store = self.seeded_store(session)
        if not self.current(session):
            return _redirect("/")
        if store is None or not store.held(selection.result, selection.source):
            return _denied()
        with self.seeded.lease(store):
            provenance = await store.service.execute(
                request.state.principal, request.state.request_now, "get_data_provenance",
                {"reference": selection.source})
        if self.seeded_store(session) is not store:
            return _redirect()
        if provenance["status"] == "ERROR":
            if provenance["error"]["code"] == "NOT_AUTHORIZED":
                return _denied()
            return _error(provenance["error"]["message"], 403)
        drawer = {"selected": None, "inspected": None, "provenance": provenance,
                  "reference": selection.source, "result": selection.result,
                  "close": "/workbench/revocation",
                  "links": [(handle, TEMPLATES.globals["revocation_evidence_link"](
                      store.seed.earlier_result_id, handle))
                      for handle in store.seed.earlier_handles]}
        if FRAGMENT_HEADER in request.headers:
            response = _html("partials/drawer.html", drawer=drawer, tour=False)
            response.headers[FRAGMENT_HEADER] = "drawer"
            return response
        return _html("evidence.html", **self.shell(session, None, drawer=drawer, tour=False))


def install_workbench(app: FastAPI, clock: Clock, provider_factory: ProviderFactory) -> None:
    workbench = Workbench(app, clock, provider_factory)
    app.state.workbench = workbench
    # Read by the authentication middleware for a workbench page without a session.
    app.state.workbench_unauthenticated = session_unavailable

    def cookie(request: Request, response: Response, session: DemoSession) -> Response:
        response.set_cookie(COOKIE, session.id, httponly=True, samesite="strict", max_age=3600,
                            secure=request.url.scheme == "https", path="/")
        return response

    @app.get("/", response_class=HTMLResponse)
    async def landing(request: Request):
        if not local_request(request):
            return _error("The synthetic demo is available on localhost only.", 403)
        session = workbench.sessions.get(request.cookies.get(COOKIE), clock.now())
        session = session or workbench.sessions.create(clock.now())
        return cookie(request, await workbench.landing(session), session)

    @app.post("/workbench/session")
    async def select_context(request: Request):
        session = workbench.sessions.get(request.cookies.get(COOKIE), clock.now())
        try:
            arguments = SessionForm.model_validate(await _form(request))
        except (ValueError, ValidationError):
            return _error("Invalid demo context. Use a known persona and timezone-aware cutoff.")
        if session is None or not valid_csrf(request, session, arguments.csrf):
            return _error("This session change requires the current same-origin form.", 403)
        context = WorkbenchContext.model_validate(
            {**PRESET_CONTEXTS[arguments.preset], "preset": arguments.preset}
            if arguments.apply_preset else
            arguments.model_dump(exclude={"csrf", "apply_preset"})
        )
        token = app.state.identity.mint(context.persona, clock.now())
        # Replacing the session discards its conversation and every evidence pair.
        workbench.sessions.remove(session)
        fresh = workbench.sessions.create(clock.now(), context, token)
        # A preset opens its destination; the context form opens the persona's home.
        destination = (next(preset["destination"] for preset in PRESETS
                            if preset["id"] == arguments.preset)
                       if arguments.apply_preset else workbench.home(fresh))
        return cookie(request, _redirect(destination), fresh)

    @app.get("/workbench", response_class=HTMLResponse)
    async def workspace(request: Request):
        return await workbench.workspace(request, request.state.demo_session)

    @app.post("/workbench/answer")
    async def answer(request: Request):
        session: DemoSession = request.state.demo_session
        try:
            arguments = QuestionForm.model_validate(await _form(request))
        except (ValueError, ValidationError):
            return _error("Enter a bounded question using the supplied form.")
        if not valid_csrf(request, session, arguments.csrf):
            return _error("Answer requests require the current same-origin form.", 403)
        if session.busy:
            return _error("An answer is already running in this session.", 409)
        if session.context.mode == "replay" and (
            not workbench.replay_matches(session) or arguments.question != workbench.replay_title
        ):
            return _error("This recording requires its original advisor, question "
                          "and locked cutoffs.", 422)
        chat_arguments = None
        if session.context.mode == "live":
            context = session.context
            try:
                chat_arguments = ChatArguments(
                    question=arguments.question, provider=context.provider,
                    conversation_id=session.conversation_id, as_of=context.as_of,
                    known_as_of=context.known_as_of,
                )
            except ValidationError:
                return _error("Enter a bounded question using the supplied form.")
        session.busy = True
        try:
            principal = request.state.principal
            if session.context.mode == "replay":
                assert workbench.recording is not None
                adapter = ReplayProvider(workbench.recording, principal=principal)
                run = await run_answer(app.state.service, ConversationManager(app.state.service),
                                       principal, clock, workbench.recorded_question, adapter,
                                       mode="replay",
                                       expected_prompt_sha256=workbench.recording.run.prompt_sha256)
                session.conversation_id = run.conversation_id
                session.run_metadata = run_metadata(run)
            else:
                assert chat_arguments is not None
                result = await answer_chat(app.state.service, principal, clock, chat_arguments,
                                           provider_factory)
                session.conversation_id = result.get("conversation_id", session.conversation_id)
                session.run_metadata = {key: result.get(key) for key in (
                    "run_id", "mode", "model", "first_pass_valid", "final_valid", "retries",
                    "fallback", "failure",
                )}
                final = result.get("final_result")
                session.run_metadata["result_id"] = (
                    final.get("result_id") if isinstance(final, dict) else None)
                if result.get("error"):
                    session.run_metadata["failure"] = result["error"]["code"]
        except (ValueError, OSError):
            session.run_metadata = {"failure": (
                "Replay contract or recording is unavailable." if session.context.mode == "replay"
                else "The live request could not be completed."
            )}
        finally:
            session.busy = False
        return _redirect()

    @app.get("/workbench/claim", response_class=HTMLResponse)
    async def claim(request: Request):
        try:
            selection = ClaimSelection.model_validate(_query(request))
        except (ValueError, ValidationError):
            return _error("Invalid verified-figure reference.")
        return await workbench.workspace(request, request.state.demo_session, selection)

    @app.get("/workbench/evidence", response_class=HTMLResponse)
    async def evidence(request: Request):
        try:
            selection = EvidenceSelection.model_validate(_query(request))
        except (ValueError, ValidationError):
            return _denied()  # A malformed pair is answered like one never issued.
        return await workbench.evidence(request, request.state.demo_session, selection)

    @app.get("/workbench/revocation", response_class=HTMLResponse)
    async def revocation(request: Request):
        return await workbench.revocation(request, request.state.demo_session)

    @app.post("/workbench/revocation/answer")
    async def revocation_followup(request: Request):
        session: DemoSession = request.state.demo_session
        try:
            arguments = CsrfForm.model_validate(await _form(request))
        except (ValueError, ValidationError):
            return _error("Use the supplied form to run the follow-up.")
        if not valid_csrf(request, session, arguments.csrf):
            return _error("The follow-up requires the current same-origin form.", 403)
        return await workbench.revocation_followup(request, session)

    @app.get("/workbench/revocation/evidence", response_class=HTMLResponse)
    async def revocation_evidence(request: Request):
        try:
            selection = EvidenceSelection.model_validate(_query(request))
        except (ValueError, ValidationError):
            return _denied()  # A malformed pair is answered like one never issued.
        return await workbench.revocation_evidence(request, request.state.demo_session,
                                                   selection)

    @app.get("/workbench/metrics", response_class=HTMLResponse)
    async def metrics(request: Request):
        return await workbench.metrics(request, request.state.demo_session)

    @app.get("/workbench/contracts", response_class=HTMLResponse)
    async def contracts(request: Request):
        return await workbench.contracts(request, request.state.demo_session)

    @app.get("/workbench/book", response_class=HTMLResponse)
    async def book(request: Request):
        return await workbench.book(request, request.state.demo_session)

    @app.get("/workbench/decisions", response_class=HTMLResponse)
    async def decisions(request: Request):
        return await workbench.decisions(request, request.state.demo_session)

    @app.get("/workbench/point-in-time", response_class=HTMLResponse)
    async def point_in_time(request: Request):
        return await workbench.point_in_time(request, request.state.demo_session)

    @app.get("/workbench/trace", response_class=HTMLResponse)
    async def trace_page(request: Request):
        return await workbench.trace_page(request, request.state.demo_session)

    @app.get("/workbench/data-health", response_class=HTMLResponse)
    async def data_health(request: Request):
        return await workbench.data_health(request, request.state.demo_session)

    @app.get("/workbench/feed-run", response_class=HTMLResponse)
    async def feed_run(request: Request):
        return await workbench.feed_run(request, request.state.demo_session)

    def household_route(path: str) -> None:
        @app.get(path, response_class=HTMLResponse, name="household_" + path.rsplit("/", 1)[1])
        async def household(request: Request):
            return await workbench.household(request, request.state.demo_session, path)

    for path in HOUSEHOLD_TABS:
        household_route(path)

    def inspector_route(surface: str) -> None:
        @app.get(f"/workbench/inspector/{surface}", response_class=HTMLResponse,
                 name=f"inspector_{surface}")
        async def inspector(request: Request):
            return await workbench.inspector(request, request.state.demo_session, surface)

    for surface in INSPECTORS:
        inspector_route(surface)

    def asset_route(name: str, media_type: str) -> None:
        @app.get(f"/workbench/assets/{name}", name=f"asset_{name}")
        async def asset():
            return FileResponse(_STATIC / name, media_type=media_type, headers=_SECURITY)

    for name, media_type in ASSETS.items():
        asset_route(name, media_type)
