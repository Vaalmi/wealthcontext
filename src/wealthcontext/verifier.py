"""Pure answer verification and server rendering over already-authorized results."""

import html
import re
import unicodedata
from collections import Counter
from datetime import date
from typing import Any

from pydantic import ValidationError

from wealthcontext.claim_resolution import (
    ResolutionError,
    bindings,
    entity_identifiers,
    resolve_claim,
    resolve_predicate,
    result_entity,
)
from wealthcontext.contracts import Answer

TEMPLATE = "Household assets in included accounts: {{c1}}."


class InvalidAnswer(ValueError):
    """One structured retry can use diagnostics without parsing exception prose."""

    def __init__(self, message: str, diagnostics: list[dict[str, str]] | None = None):
        self.diagnostics = diagnostics or [{"code": "INVALID_ANSWER", "path": "answer",
                                            "message": message}]
        super().__init__(message)


_PLACEHOLDER = re.compile(r"\{\{([cp][1-9][0-9]*)\}\}")
_NUMBER = re.compile(r"\d+(?:[.,]\d+)*", re.UNICODE)
_LINT_WORDS = set("""
largest smallest highest lowest most least increased decreased doubled halved top bottom
all none no every each only always never higher lower greater less fewer more larger smaller
biggest compared above below over under equal equals maximum minimum first second third
fourth fifth sixth seventh eighth ninth tenth eleventh twelfth zero one two three four five
six seven eight nine ten eleven twelve thirteen fourteen fifteen sixteen seventeen eighteen
nineteen twenty thirty forty fifty sixty seventy eighty ninety hundred thousand million
billion trillion half quarter double triple twice thrice dozen both neither either
complete incomplete matched unmatched
""".split())
# Reconciliation statuses beyond the single-word ones above. Their wording, and the
# enum spellings, may appear only through a reconciliation predicate placeholder.
_STATUS_PHRASES = (
    re.compile(r"(?<![A-Za-z])pending[\s_-]+settlement(?![A-Za-z])", re.IGNORECASE),
    re.compile(r"(?<![A-Za-z])(?:break|breaks|broken)(?![A-Za-z])", re.IGNORECASE),
    re.compile(r"(?<![A-Za-z])reconciliation[\s_-]+(?:status[\s_-]+)?(?:is[\s_-]+)?unavailable"
               r"(?![A-Za-z])", re.IGNORECASE),
    re.compile(r"(?<![A-Za-z])unavailable[\s_-]+for[\s_-]+reconciliation(?![A-Za-z])",
               re.IGNORECASE),
)
# Inline formatting that renders inside a word ("lar**gest**" displays "largest").
_INLINE_MARKUP = re.compile(r"[*_~`\\]+")
_HTML_TAG = re.compile(r"<[^<>\n]*>")
_STATUS_WORDING = {
    "COMPLETE": "complete", "INCOMPLETE": "incomplete",
    "MATCHED": "matched", "PENDING_SETTLEMENT": "pending settlement",
    "BREAK": "a reconciliation break", "RECONCILIATION_UNAVAILABLE": "reconciliation unavailable",
    "AT_OR_ABOVE": "at or above the applied threshold", "BELOW": "below the applied threshold",
}
_REFUSAL_WORDING = {
    "NOT_AUTHORIZED": "Access denied.", "UNAUTHENTICATED": "Authentication is required.",
    "NOT_FOUND": "The requested data is unavailable.",
    "POLICY_UNAVAILABLE": "Authorization is unavailable.",
}
_DATE_FIELDS = {"as_of", "valuation_date", "known_as_of", "calculated_at", "price_date", "rate_at",
                "business_date", "recorded_at", "imported_at", "trade_date", "settle_date"}
_EMPTY_FALLBACK = "Verified data is unavailable."
_ANSWER_FIELDS = frozenset({
    "answer_markdown", "claims", "predicates", "caveats", "refusal", "id", "result_id",
    "entity", "type", "field", "household_id", "valuation_date", "unit", "format",
    "kind", "expect", "flag", "subject", "code",
})
_PREDICATE_TAGS = frozenset({"rank", "threshold", "completeness", "reconciliation"})
_SCHEMA_MESSAGES = {
    "missing": ("SCHEMA_REQUIRED_FIELD", "Required field is missing."),
    "extra_forbidden": ("SCHEMA_EXTRA_FIELD", "Remove this unsupported field."),
    "list_type": ("SCHEMA_TYPE_MISMATCH", "Expected a JSON array."),
    "model_type": ("SCHEMA_TYPE_MISMATCH", "Expected a JSON object."),
    "model_attributes_type": ("SCHEMA_TYPE_MISMATCH", "Expected a JSON object."),
    "string_type": ("SCHEMA_TYPE_MISMATCH", "Expected a JSON string."),
    "int_type": ("SCHEMA_TYPE_MISMATCH", "Expected a JSON integer."),
    "literal_error": ("SCHEMA_INVALID_VALUE", "Use a value declared in the supplied schema."),
    "union_tag_invalid": ("SCHEMA_INVALID_VALUE", "Use a declared predicate kind."),
    "union_tag_not_found": ("SCHEMA_REQUIRED_FIELD", "A declared predicate kind is required."),
    "string_pattern_mismatch": (
        "SCHEMA_INVALID_VALUE", "Value must match the field format in the supplied schema.",
    ),
    "date_from_datetime_parsing": ("SCHEMA_INVALID_VALUE", "Use an ISO date in YYYY-MM-DD format."),
    "date_parsing": ("SCHEMA_INVALID_VALUE", "Use an ISO date in YYYY-MM-DD format."),
    "date_type": ("SCHEMA_TYPE_MISMATCH", "Use an ISO date in YYYY-MM-DD format."),
}


def _diagnostic(code: str, path: str, message: str) -> dict[str, str]:
    return {"code": code, "path": path, "message": message}


def answer_schema_diagnostics(error: ValidationError) -> list[dict[str, str]]:
    """Bounded repair hints from schema metadata, without input values or arbitrary keys."""
    failures = error.errors(include_url=False, include_context=False, include_input=False)
    diagnostics = []
    for failure in failures[:19 if len(failures) > 20 else 20]:
        path = "answer"
        for component in failure["loc"][:8]:
            if type(component) is int:
                path += f"[{component}]" if 0 <= component < 100 else "[item]"
            elif component in _ANSWER_FIELDS:
                path += "." + component
            elif component not in _PREDICATE_TAGS:
                path += ".[unrecognized_field]"
        code, message = _SCHEMA_MESSAGES.get(failure["type"], (
            "SCHEMA_INVALID_VALUE", "Value or field combination violates the supplied schema.",
        ))
        diagnostics.append(_diagnostic(code, path, message))
    if len(failures) > 20:
        diagnostics.append(_diagnostic(
            "SCHEMA_DIAGNOSTICS_TRUNCATED", "answer",
            "Additional schema errors omitted; correct these and retry.",
        ))
    return diagnostics


def _escape(value: Any) -> str:
    """Server labels are data, including when a received name resembles Markdown."""
    text = str(value).replace("\n", " ").replace("\r", " ")
    return re.sub(r"([\\`*_{}\[\]()<>!#|])", r"\\\1", text)


def _evidence(result: dict) -> dict:
    return {"trace_id": result.get("trace_id"), "source_refs": result.get("source_refs", []),
            "metric_refs": result.get("metric_refs", []), "known_as_of": result.get("known_as_of")}


def _dates(value: Any) -> set[str]:
    dates: set[str] = set()
    if isinstance(value, dict):
        for key, item in value.items():
            if key in _DATE_FIELDS and isinstance(item, str):
                dates.add(item)
            elif isinstance(item, dict | list):
                dates.update(_dates(item))
    elif isinstance(value, list):
        for item in value:
            dates.update(_dates(item))
    return dates


def _lint(markdown: str, declared: set[str], cited: dict[str, dict]) -> list[dict[str, str]]:
    diagnostics = []
    placeholders = Counter(_PLACEHOLDER.findall(markdown))
    for identifier in sorted(declared):
        if placeholders[identifier] == 0:
            diagnostics.append(_diagnostic("UNUSED_REFERENCE", identifier,
                                           "Declared reference has no placeholder"))
        elif placeholders[identifier] > 1:
            diagnostics.append(_diagnostic("DUPLICATE_PLACEHOLDER", identifier,
                                           "Each reference must have exactly one placeholder"))
    for identifier in sorted(placeholders.keys() - declared):
        diagnostics.append(_diagnostic("UNKNOWN_PLACEHOLDER", identifier,
                                       "Placeholder has no declared reference"))
    remainder = _PLACEHOLDER.sub(lambda match: " " if match[1] in declared else match[0], markdown)
    if "{{" in remainder or "}}" in remainder:
        diagnostics.append(_diagnostic("MALFORMED_PLACEHOLDER", "answer_markdown",
                                       "Unresolved or malformed placeholder syntax"))
    allowed: set[str] = set()
    for result_id, result in cited.items():
        allowed.update({result_id, result.get("trace_id", "")})
        allowed.update(result.get("source_refs", []))
        allowed.update(result.get("metric_refs", []))
        allowed.update(entity_identifiers(result))
        allowed.update(_dates(result))
        allowed.update(flag["subject"] for flag in result.get("quality_flags", []))
    for token in sorted((item for item in allowed if isinstance(item, str) and item),
                        key=lambda item: (-len(item), item)):
        remainder = re.sub(r"(?<![A-Za-z0-9_:@-])" + re.escape(token)
                           + r"(?![A-Za-z0-9_@-]|[.:][A-Za-z0-9_])", " ", remainder)
    remainder = re.sub(r"(?m)^\s{0,3}(?:[-*+]|\d+[.)])\s+", " ", remainder)
    texts = [_visible(remainder)]
    # Also read the text as displayed: inline markup and HTML inside a word do not
    # split it. Both readings are checked, so joining never hides a token.
    texts.append(_visible(_INLINE_MARKUP.sub("", html.unescape(_HTML_TAG.sub("", texts[0])))))
    for token in sorted({token for text in texts for token in _NUMBER.findall(text)}):
        diagnostics.append(_diagnostic("UNVERIFIED_NUMBER", "answer_markdown",
                                       f"Numeric literal {token!r} requires a claim placeholder"))
    words = {match.group().casefold() for text in texts
             for match in re.finditer(r"[A-Za-z]+", text)}
    phrases = {" ".join(re.split(r"[\s_-]+", match.group().casefold()))
               for text in texts for pattern in _STATUS_PHRASES
               for match in pattern.finditer(text)}
    for token in sorted((words & _LINT_WORDS) | phrases):
        diagnostics.append(_diagnostic("UNVERIFIED_TOKEN", "answer_markdown",
                                       f"Token {token!r} requires a verified predicate or claim"))
    return diagnostics


def _visible(text: str) -> str:
    text = unicodedata.normalize("NFKC", text)
    return "".join(char for char in text if unicodedata.category(char) != "Cf")


def _claim_text(claim, binding, value: Any) -> str:
    unit = claim.unit
    text = (f"USD {value}" if unit == "USD" else f"{value}%" if unit == "pct"
            else f"{value} shares" if unit == "shares" else f"{value} days" if unit == "days"
            else str(value))
    return f"{_escape(binding.label)}: {text}" if claim.format == "label_and_value" else text


def _predicate_text(predicate, binding) -> str:
    if predicate.kind == "rank":
        scopes = {"household_issuers": "household issuers",
                  "household_fund_vehicles": "household fund vehicles",
                  "household_securities": "household securities"}
        scope = scopes.get(binding.rank_scope, "the recorded ranking")
        return f"largest among {scope}" if predicate.expect == 1 else (
            f"ranked {predicate.expect} among {scope}"
        )
    return _STATUS_WORDING[predicate.expect]


def _warnings(answer: Answer, cited: dict[str, dict]) -> tuple[list[dict], list[dict[str, str]]]:
    available: dict[tuple[str, str], list[tuple[str, dict]]] = {}
    required = set()
    for result_id, result in cited.items():
        for flag in result.get("quality_flags", []):
            key = flag["code"], flag["subject"]
            available.setdefault(key, []).append((result_id, flag))
            if flag["severity"] == "warning":
                required.add(key)
    requested = [(item.flag, item.subject) for item in answer.caveats]
    diagnostics = []
    if len(requested) != len(set(requested)):
        diagnostics.append(_diagnostic("DUPLICATE_CAVEAT", "caveats", "Caveats must be unique"))
    for code, subject in sorted(required - set(requested)):
        diagnostics.append(_diagnostic("MISSING_CAVEAT", "caveats",
                                       f"Required warning {code} for {subject} is missing"))
    for code, subject in sorted(set(requested) - available.keys()):
        diagnostics.append(_diagnostic("UNKNOWN_CAVEAT", "caveats",
                                       f"Caveat {code} for {subject} is absent from cited results"))
    warnings = []
    for key in sorted(set(requested) & available.keys()):
        # Preserve different received details even when one caveat discloses a
        # shared code/subject in several cited results.
        for result_id, flag in available[key]:
            warnings.append({"flag": key[0], "subject": key[1], "severity": flag["severity"],
                             "detail": flag.get("detail", {}), "result_ids": [result_id],
                             "message": f"{flag['severity'].title()}: {key[0]} — {key[1]}"})
    return warnings, diagnostics


def _sources(cited: dict[str, dict]) -> list[dict]:
    sources = []
    for result_id, result in sorted(cited.items()):
        records = []
        for row in (result.get("data") or {}).get("items", []):
            if isinstance(row, dict) and row.get("payload_hash_scope") == "stored_received_record":
                keys = ("source_record_id", "source_system", "business_date", "payload_hash")
                if all(key in row for key in keys):
                    records.append({key: row[key] for key in keys})
        sources.append({"result_id": result_id, **_evidence(result),
                        "valuation_date": result.get("valuation_date"),
                        "status": result["status"], "records": records})
    return sources


def verify_answer(answer: Answer, results: dict[str, dict]) -> dict:
    """Resolve a candidate against exact caller-authorized, immutable result envelopes."""
    diagnostics: list[dict[str, str]] = []
    references = [*answer.claims, *answer.predicates]
    if len({item.id for item in references}) != len(references):
        diagnostics.append(_diagnostic("DUPLICATE_REFERENCE_ID", "claims",
                                       "Claim and predicate IDs must be unique"))
    cited_ids = {item.result_id for item in references}
    if answer.refusal is not None:
        cited_ids.add(answer.refusal.result_id)
    cited = {}
    for result_id in sorted(cited_ids):
        result = results.get(result_id)
        if result is None or result.get("result_id") != result_id:
            diagnostics.append(_diagnostic("RESULT_UNAVAILABLE", result_id,
                                           "Referenced result is not in the authorized result set"))
        else:
            cited[result_id] = result
    diagnostics.extend(_lint(answer.answer_markdown, {item.id for item in references}, cited))
    semantic = [item.model_dump_json(exclude={"id", "format"}) for item in references]
    if len(semantic) != len(set(semantic)):
        diagnostics.append(_diagnostic("DUPLICATE_REFERENCE", "claims",
                                       "Duplicate entity/field references are not allowed"))
    claims, predicates, replacements = [], [], {}
    for claim in answer.claims:
        result = cited.get(claim.result_id)
        if result is None:
            continue
        try:
            binding, value = resolve_claim(claim, result)
            claims.append({**claim.model_dump(mode="json"), "value": value, "label": binding.label,
                           **_evidence(result)})
            replacements[claim.id] = _claim_text(claim, binding, value)
        except ResolutionError as exc:
            diagnostics.append(_diagnostic(exc.code, claim.id, str(exc)))
    for predicate in answer.predicates:
        result = cited.get(predicate.result_id)
        if result is None:
            continue
        try:
            binding, value = resolve_predicate(predicate, result)
            item = {**predicate.model_dump(mode="json"), "value": value,
                    "household_id": binding.household_id,
                    "valuation_date": result.get("valuation_date"), **_evidence(result)}
            if predicate.kind == "rank":
                item["rank_scope"] = binding.rank_scope
            if predicate.kind == "threshold":
                item["applied_threshold_pct"] = result["data"]["applied_threshold_pct"]
            replacements[predicate.id] = _predicate_text(predicate, binding)
            item["wording"] = replacements[predicate.id]
            predicates.append(item)
        except ResolutionError as exc:
            diagnostics.append(_diagnostic(exc.code, predicate.id, str(exc)))
    refusal = None
    if answer.refusal is not None and answer.refusal.result_id in cited:
        result = cited[answer.refusal.result_id]
        if result.get("status") != "ERROR" or (result.get("error") or {}).get("code") != (
            answer.refusal.code
        ):
            diagnostics.append(_diagnostic("REFUSAL_MISMATCH", "refusal",
                                           "Refusal must match an actual governed request error"))
        else:
            refusal = {**answer.refusal.model_dump(), "trace_id": result.get("trace_id"),
                       "message": _REFUSAL_WORDING.get(answer.refusal.code,
                                                        "The request could not be completed.")}
    warnings, warning_diagnostics = _warnings(answer, cited)
    diagnostics.extend(warning_diagnostics)
    if diagnostics:
        raise InvalidAnswer("; ".join(item["message"] for item in diagnostics), diagnostics)
    markdown = (refusal["message"] if refusal else _PLACEHOLDER.sub(
        lambda match: replacements[match[1]], answer.answer_markdown,
    ))
    for warning in warnings:
        markdown += f"\n\n> {_escape(warning['message'])}"
    sources = _sources(cited)
    for source in sources:
        scope = (f"Result {_escape(source['result_id'])}; status {_escape(source['status'])}; "
                 f"trace {_escape(source['trace_id'])}")
        if source["valuation_date"] is not None:
            scope += f"; valuation date {_escape(source['valuation_date'])}"
        if source["known_as_of"] is not None:
            scope += f"; known as of {_escape(source['known_as_of'])}"
        markdown += f"\n\n> Source: {scope}."
        for record in source["records"]:
            markdown += "\n> Received record: " + "; ".join(
                f"{key}: {_escape(value)}" for key, value in record.items()
            ) + "."
    return {"markdown": markdown, "claims": claims, "predicates": predicates,
            "warnings": warnings, "caveats": [item.model_dump() for item in answer.caveats],
            "refusal": refusal, "sources": sources, "cited_result_ids": sorted(cited),
            "mode": "verified"}


def deterministic_fallback(results: dict[str, dict]) -> Answer:
    """Produce a conservative candidate which goes through the identical verifier."""
    for result_id, result in sorted(results.items()):
        if result.get("result_id") != result_id:
            continue
        if result.get("status") == "ERROR" and isinstance(result.get("error"), dict):
            candidate = Answer.model_validate({
                "answer_markdown": _REFUSAL_WORDING.get(result["error"]["code"],
                                                        "The request could not be completed."),
                "claims": [], "predicates": [], "caveats": [],
                "refusal": {"result_id": result_id, "code": result["error"]["code"]},
            })
            verify_answer(candidate, results)
            return candidate
    for result_id, result in sorted(results.items()):
        found = bindings(result)
        if not found or result.get("result_id") != result_id:
            continue
        warnings = sorted({(flag["code"], flag["subject"])
                           for flag in result.get("quality_flags", [])
                           if flag["severity"] == "warning"})
        if len(warnings) > 100:
            continue
        root = found[0]
        claims = []
        phrases = ["Result status: {{p1}}."]
        day = result.get("valuation_date")
        if day is not None:
            for name in ("assets_usd", "fresh_subtotal_usd", "applied_threshold_pct", "item_count",
                         "evaluated_household_count"):
                if name not in root.fields:
                    continue
                claim_id = f"c{len(claims) + 1}"
                claims.append({"id": claim_id, "result_id": result_id,
                               "entity": result_entity(result).model_dump(), "field": name,
                               "household_id": root.household_id,
                               "valuation_date": date.fromisoformat(day),
                               "unit": root.fields[name][1], "format": "value_only"})
                labels = {"assets_usd": "Household assets", "fresh_subtotal_usd": "Fresh subtotal",
                          "applied_threshold_pct": "Applied threshold",
                          "item_count": "Returned items",
                          "evaluated_household_count": "Evaluated households"}
                phrases.append(f"{labels[name]}: {{{{{claim_id}}}}}.")
        candidate = Answer.model_validate({
            "answer_markdown": " ".join(phrases), "claims": claims,
            "predicates": [{"id": "p1", "kind": "completeness", "result_id": result_id,
                            "entity": result_entity(result).model_dump(), "field": "status",
                            "expect": result["status"]}],
            "caveats": [{"flag": code, "subject": subject} for code, subject in warnings],
            "refusal": None,
        })
        verify_answer(candidate, results)
        return candidate
    return Answer(answer_markdown=_EMPTY_FALLBACK, claims=[], predicates=[], caveats=[],
                  refusal=None)


def verify_one_claim(answer: Answer, result: dict) -> dict:
    if (answer.answer_markdown != TEMPLATE or len(answer.claims) != 1
            or answer.predicates or answer.caveats or answer.refusal is not None):
        raise InvalidAnswer("P1 supports only the declared single-claim template")
    claim = answer.claims[0]
    data = result.get("data") or {}
    if (result["status"] != "COMPLETE" or result["quality_flags"]
            or claim.id != "c1" or claim.result_id != result["result_id"]
            or claim.entity.type != "household" or claim.entity.id != data.get("household_id")
            or claim.household_id != data.get("household_id") or claim.field != "assets_usd"
            or claim.unit != "USD" or claim.format != "value_only"
            or claim.valuation_date.isoformat() != result["valuation_date"]):
        raise InvalidAnswer("Claim scope or field does not match the authorized result")
    value = data["assets_usd"]
    return {
        "markdown": TEMPLATE.replace("{{c1}}", f"USD {value}"),
        "claims": [{**claim.model_dump(mode="json"), "value": value,
                    "trace_id": result["trace_id"], "source_refs": result["source_refs"]}],
        "warnings": [], "mode": "scripted_verified_p1",
    }
