"""Escaped, deliberately small Markdown subset and plain governed inspector tables."""

import html
import json
import re
from datetime import UTC, date, datetime
from pathlib import Path
from urllib.parse import urlencode

from jinja2 import Environment, FileSystemLoader, select_autoescape
from markupsafe import Markup

from wealthcontext.workbench_compliance import trace_link
from wealthcontext.workbench_operations import ATTRIBUTIONS, FEED_STATES, REASONS, feed_run_link

TEMPLATES = Environment(
    loader=FileSystemLoader(Path(__file__).parent / "templates"),
    autoescape=select_autoescape(["html"]), trim_blocks=True, lstrip_blocks=True,
)


# A backslash before ASCII punctuation is a Markdown escape (CommonMark). The verifier
# escapes server labels and identifiers this way, so `r\_abc` displays as `r_abc`.
_ESCAPED = re.compile(r"\\([!-/:-@\[-`{-~])")
_HELD = re.compile("\x00(\\d+)\x00")


def safe_markdown(value: str) -> Markup:
    # No HTML, URLs, images or arbitrary attributes are interpreted. Escape first;
    # only fixed formatting tags are introduced afterwards. Escaped punctuation is held
    # out of formatting and restored as escaped literal text.
    lines = []
    for line in value.splitlines():
        literals: list[str] = []

        def hold(match: re.Match[str], literals: list[str] = literals) -> str:
            literals.append(match[1])
            return f"\x00{len(literals) - 1}\x00"

        escaped = html.escape(_ESCAPED.sub(hold, line.replace("\x00", "")), quote=True)
        escaped = re.sub(r"\*\*([^*\n]+)\*\*", r"<strong>\1</strong>", escaped)
        escaped = re.sub(r"`([^`\n]+)`", r"<code>\1</code>", escaped)
        if escaped.startswith("- "):
            escaped = '<p class="bullet">' + escaped[2:] + "</p>"
        elif escaped.startswith("&gt; "):
            escaped = '<p class="source-line">' + escaped[5:] + "</p>"
        elif escaped:
            escaped = "<p>" + escaped + "</p>"
        if escaped:
            lines.append(_HELD.sub(lambda match, literals=literals: html.escape(
                literals[int(match[1])], quote=True), escaped))
    return Markup("\n".join(lines))


def answer_markdown(value: str, warnings: list[dict] | None = None) -> Markup:
    """Move exact server warning lines into the dedicated, visible warning callouts."""
    messages = {warning["message"] for warning in warnings or []}
    lines = [line for line in value.splitlines() if not (
        line.startswith("> ") and re.sub(r"\\(.)", r"\1", line[2:]) in messages
    )]
    return safe_markdown("\n".join(lines))


def user_display(content: str) -> dict:
    """Project display text only; keep the exact submitted transcript as context."""
    try:
        data = json.loads(content)
    except ValueError:
        data = None
    if (isinstance(data, dict) and isinstance(data.get("question"), str)
            and set(data) <= {"question", "as_of", "known_as_of"}
            and {"as_of", "known_as_of"} & set(data)):
        question, context = data["question"], content
    else:
        question = content.splitlines()[0] if content.splitlines() else content
        context = content if "\n" in content else None
    return {"role": "user", "content": content, "question": question, "request_context": context}


def display_value(value) -> str:
    return (json.dumps(value, ensure_ascii=False, sort_keys=True)
            if isinstance(value, dict | list) else str(value))


_CURRENCIES = frozenset({"USD", "EUR", "CAD", "GBP"})


def figure(claim: dict) -> str:
    """One platform value with its unit as a word; the value itself is never changed."""
    value, unit = str(claim.get("value", "")), claim.get("unit")
    return (f"{unit} {value}" if unit in _CURRENCIES else value + "%" if unit == "pct"
            else value + " shares" if unit == "shares" else value + " days" if unit == "days"
            else value)


def holdings(data: dict) -> list[dict]:
    """The returned positions, cash and stale components under their returned account.

    Grouping only: no figure is computed, compared or summed. A stale component's subject
    is `account:security`, as on its flag, so it is listed under that account. An account
    the snapshot lists as a missing component is marked, so the page can say it was not
    received.
    """
    missing = set(data.get("missing_components") or [])
    stale = data.get("stale_components") or []
    return [{"account": account,
             "positions": [row for row in data.get("positions", [])
                           if row["account_id"] == account["id"]],
             "cash": [row for row in data.get("cash_balances", [])
                      if row["account_id"] == account["id"]],
             "stale": [row for row in stale
                       if row["subject"].partition(":")[0] == account["id"]],
             "missing": account["id"] in missing}
            for account in data.get("accounts", [])]


def claim_link(answer_id: str, claim_id: str, reference: str | None = None) -> str:
    args = {"answer": answer_id, "claim": claim_id}
    if reference is not None:
        args["reference"] = reference
    return "/workbench/claim?" + urlencode(args)


def evidence_link(result_id: str, handle: str) -> str:
    return "/workbench/evidence?" + urlencode({"result": result_id, "source": handle})


def revocation_evidence_link(result_id: str, handle: str) -> str:
    """A source handle from A7's earlier turn, opened in that session's seeded store."""
    return "/workbench/revocation/evidence?" + urlencode({"result": result_id, "source": handle})


_MONTHS = ("Jan", "Feb", "Mar", "Apr", "May", "Jun", "Jul", "Aug", "Sep", "Oct", "Nov", "Dec")


def long_date(value: date | str) -> str:
    """A platform date as `30 Sep 2026`; anything that is not an ISO date is shown as given."""
    if isinstance(value, str):
        try:
            value = date.fromisoformat(value)
        except ValueError:
            return value
    return f"{value.day} {_MONTHS[value.month - 1]} {value.year}"


def long_instant(value: datetime | str) -> str:
    """A platform instant as `31 Oct 2026, 16:00 UTC`; a string that is not an ISO instant
    is shown as given."""
    if isinstance(value, str):
        try:
            value = datetime.fromisoformat(value)
        except ValueError:
            return value
        if value.tzinfo is None:
            return value.isoformat()
    moment = value.astimezone(UTC)
    return f"{long_date(moment.date())}, {moment:%H:%M} UTC"


ROLE_LABELS = {"advisor": "Advisor", "ops": "Operations", "compliance": "Compliance",
               "engineer": "Platform engineer"}
# The design system maps each platform state to one of five badge kinds. A value it
# does not name gets the neutral kind; a badge always carries the value as its word.
_STATUS_KINDS = {
    **dict.fromkeys(("COMPLETE", "MATCHED"), "complete"),
    **dict.fromkeys(("INCOMPLETE", "PRICE_STALE", "SOURCE_UNAVAILABLE", "FX_UNAVAILABLE",
                     "AMBIGUOUS_ENTITY"), "incomplete"),
    **dict.fromkeys(("QUANTITY_BREAK", "CASH_BREAK", "BREAK", "FAILED", "ERROR"), "break"),
    **dict.fromkeys(("PENDING_SETTLEMENT", "RECONCILIATION_UNAVAILABLE"), "info"),
    "NOT_AUTHORIZED": "denied",
}


def status_kind(value: object, severity: str | None = None) -> str:
    kind = _STATUS_KINDS.get(str(value))
    if severity == "warning" and kind in (None, "info"):
        return "incomplete"  # a warning never takes the informational look
    if kind is None and severity is not None:
        return "info"
    return kind or "neutral"


_STATE_LABELS = {"COMPLETE": "Complete", "INCOMPLETE": "Incomplete",
                 "NOT_AUTHORIZED": "Access denied", "ERROR": "Error",
                 # Reconciliation statuses, with the design system's labels.
                 "MATCHED": "Matched", "BREAK": "Break",
                 "PENDING_SETTLEMENT": "Pending settlement",
                 "RECONCILIATION_UNAVAILABLE": "Reconciliation unavailable"}
# The severity each reconciliation status's flag carries (reconciliation.py), so a row's
# badge and its flag in the list take the same look.
RECONCILIATION_SEVERITY = {"BREAK": "warning", "RECONCILIATION_UNAVAILABLE": "warning",
                           "PENDING_SETTLEMENT": "info"}


def state_label(value: object) -> str:
    """The badge's word: the platform's own code unless the design system names a label."""
    return _STATE_LABELS.get(str(value), str(value))


def envelope_state(envelope: dict) -> str:
    """The word a result's badge shows: its error code when it failed, else its status."""
    error = envelope.get("error")
    return str(error["code"]) if isinstance(error, dict) else str(envelope.get("status"))


def inspector_table(envelope: dict | None) -> tuple[list[str], list[dict]]:
    data = (envelope or {}).get("data")
    if not isinstance(data, dict):
        return [], []
    rows = data.get("items", data.get("results", data.get("accounts", [])))
    if not isinstance(rows, list) or not rows or not all(isinstance(row, dict) for row in rows):
        return [], []
    # The service already applies its documented result cap; show every returned row.
    columns = list(dict.fromkeys(key for row in rows for key in row))
    return columns, rows


def json_text(value: object) -> str:
    return json.dumps(value, indent=2, ensure_ascii=False, sort_keys=True)


TEMPLATES.filters.update(safe_markdown=safe_markdown, answer_markdown=answer_markdown,
                         display_value=display_value, figure=figure, long_date=long_date,
                         long_instant=long_instant, status_kind=status_kind,
                         envelope_state=envelope_state, state_label=state_label,
                         json_text=json_text)
TEMPLATES.globals.update(claim_link=claim_link, evidence_link=evidence_link,
                         trace_link=trace_link, feed_run_link=feed_run_link,
                         feed_states=FEED_STATES, reasons=REASONS, attributions=ATTRIBUTIONS,
                         revocation_evidence_link=revocation_evidence_link, holdings=holdings,
                         reconciliation_severity=RECONCILIATION_SEVERITY)
