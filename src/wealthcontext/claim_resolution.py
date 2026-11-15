"""Pure, explicit bindings from recorded result contracts to answer references.

This module neither computes financial values nor searches arbitrary nested JSON.
Absent or ambiguous evidence is unavailable, even when a plausible value could
be inferred from a rounded display, a missing row, or another household.
"""

import json
import re
from dataclasses import dataclass, field
from decimal import Decimal, InvalidOperation
from typing import Any

from wealthcontext.contracts import Claim, EntityReference, Predicate


class ResolutionError(ValueError):
    def __init__(self, code: str, message: str):
        self.code = code
        super().__init__(message)


@dataclass
class Binding:
    entity_type: str
    entity_id: str
    household_id: str | None
    label: str
    fields: dict[str, tuple[Any, str]] = field(default_factory=dict)
    predicates: dict[str, Any] = field(default_factory=dict)
    rank_scope: str | None = None


_MONEY = {"assets_usd", "cash_usd", "fresh_subtotal_usd", "exposure_usd",
          "market_value_usd", "price_usd", "last_known_value_usd"}
_PERCENT = {"concentration_pct", "allocation_pct", "applied_threshold_pct"}
_COUNTS = {"item_count", "total_item_count", "evaluated_household_count",
           "determinate_household_count", "indeterminate_household_count",
           "breaching_household_count", "households_with_issues_count",
           "incomplete_household_count", "quality_flag_count", "stale_component_count",
           "missing_component_count", "warning_count", "info_count", "accepted_records",
           "rejected_records", "question_count", "passed_count", "rank", "issuer_rank",
           "book_rank"}
_NUMERIC_UNITS = {**dict.fromkeys(_MONEY, "USD"), **dict.fromkeys(_PERCENT, "pct"),
                  **dict.fromkeys(_COUNTS, "count"), "quantity": "shares",
                  "fx_rate": "fx_rate", "price_age_business_days": "days"}
_LISTS = ("issuer_exposures", "fund_vehicle_exposures", "security_exposures")
_METADATA = (*_LISTS, "breach_metadata", "indeterminate_metadata", "flags", "rejects")


def _numeric(row: dict, *, status: str = "COMPLETE") -> dict[str, tuple[Any, str]]:
    return {key: (value, _NUMERIC_UNITS[key]) for key, value in row.items()
            if key in _NUMERIC_UNITS and value is not None
            and not (status == "INCOMPLETE" and key in {"assets_usd", "cash_usd"})}


def _metadata(row: dict) -> dict[str, tuple[Any, str]]:
    return {f"{name}.{key}": (value[key], "count") for name in _METADATA
            if isinstance(value := row.get(name), dict)
            for key in ("item_count", "total_item_count") if key in value}


def _rows(value: Any) -> list[dict]:
    return [row for row in value if isinstance(row, dict)] if isinstance(value, list) else []


def bindings(result: dict) -> list[Binding]:
    """Enumerate only entities and numbers explicitly present in known wire shapes."""
    data, status = result.get("data"), result.get("status")
    if status not in {"COMPLETE", "INCOMPLETE"} or not isinstance(data, dict):
        return []
    household = data.get("household_id")
    summary = Binding("result_summary", result["result_id"], household, "Result summary",
                      {**_numeric(data, status=status), **_metadata(data)}, {"status": status})
    found = [summary]
    if isinstance(household, str):
        found.append(Binding("household", household, household,
                             data.get("household_name", household),
                             _numeric(data, status=status), {"status": status}))
    account_labels = {item.get("id"): item.get("title", item.get("id", "Account"))
                      for item in _rows(data.get("accounts"))}
    for row in _rows(data.get("accounts")) + _rows(data.get("excluded_accounts")):
        if isinstance(row.get("id"), str):
            found.append(Binding("account", row["id"], household,
                                 account_labels.get(row["id"], row["id"])))
    for row in _rows(data.get("positions")):
        values = _numeric(row)
        if row.get("listing_currency", "USD") == "USD" and row.get("price_local") is not None:
            values["price_local"] = row["price_local"], "USD"
        found.append(Binding("position", row["id"], household, row.get("security_id", row["id"]),
                             values))
    # Frozen Claim has no cash entity or non-USD monetary unit. Qualified account
    # fields identify a specific currency row; local values are citable only for USD.
    for row in _rows(data.get("cash_balances")):
        values = {f"cash.{row['currency']}.{key}": value for key, value in _numeric(row).items()}
        if row.get("currency") == "USD":
            values["cash.USD.amount_local"] = row["amount_local"], "USD"
        found.append(Binding("account", row["account_id"], household,
                             account_labels.get(row["account_id"], row["account_id"]), values))
    for account in _rows(data.get("reconciliation")):
        for row in _rows(account.get("rows")):
            values = {key: (row[key], "shares" if row.get("kind") == "quantity" else "USD")
                      for key in ("authoritative", "settled", "pending_delta", "difference")
                      if row.get(key) is not None}
            if row.get("kind") == "quantity":
                found.append(Binding("position", row["subject"], household,
                                     row.get("security_id", row["subject"]), values,
                                     {"reconciliation_status": row["reconciliation_status"]}))
            elif row.get("currency") == "USD":
                label = account_labels.get(account["account_id"], account["account_id"])
                found.append(Binding("account", account["account_id"], household,
                                     label,
                                     {f"cash.USD.{key}": value for key, value in values.items()}))
    for name in _LISTS:
        if status == "INCOMPLETE":
            continue
        listing = data.get(name, {})
        if not isinstance(listing, dict):
            continue
        for row in _rows(listing.get("items")):
            predicates = {"rank": row["rank"]} if "rank" in row else {}
            if type(row.get("meets_threshold")) is bool:
                predicates["meets_threshold"] = row["meets_threshold"]
            found.append(Binding(row["entity_type"], row["entity_id"], household, row["label"],
                                 _numeric(row), predicates, listing.get("rank_scope")))
    for row in _rows(data.get("breaches")):
        found.append(Binding("issuer", row["issuer_id"], row["household_id"], row["label"],
                             _numeric(row), {"meets_threshold": row.get("meets_threshold")}))
        # Assets are a household value, not the issuer label. Multiple breaches
        # may repeat that exact received value; inconsistent repetitions fail closed.
        found.append(Binding("household", row["household_id"], row["household_id"],
                             row["household_id"], {"assets_usd": (row["assets_usd"], "USD")}))
    for row in _rows(data.get("indeterminate")):
        found.append(Binding("household", row["household_id"], row["household_id"],
                             row["household_id"], _numeric(row, status="INCOMPLETE")))
    for row in _rows(data.get("stale_components")):
        found.append(Binding("position", row["subject"], household, row["subject"], _numeric(row)))
    for row in _rows(data.get("items")):
        if isinstance(row.get("household_id"), str):
            found.append(Binding("household", row["household_id"], row["household_id"],
                                 row.get("household_name", row["household_id"]),
                                 {**_numeric(row), **_metadata(row)}))
        elif "feed" in row and isinstance(row.get("id"), str):
            found.append(Binding("feed_run", row["id"], None, row["id"],
                                 {**_numeric(row), **_metadata(row)}))
    return found


def _unique(candidates: list[tuple[Binding, Any]]) -> tuple[Binding, Any]:
    unique = {}
    for binding, value in candidates:
        key = json.dumps([binding.entity_type, binding.entity_id, binding.household_id,
                          binding.label, binding.rank_scope, value], sort_keys=True)
        unique[key] = binding, value
    if not unique:
        raise ResolutionError("UNRESOLVED_REFERENCE",
                              "Entity or field is absent from the cited result")
    if len(unique) != 1:
        raise ResolutionError("AMBIGUOUS_REFERENCE", "Reference matches multiple scoped entities")
    return next(iter(unique.values()))


def resolve_claim(claim: Claim, result: dict) -> tuple[Binding, Any]:
    if claim.valuation_date.isoformat() != result.get("valuation_date"):
        raise ResolutionError("VALUATION_DATE_MISMATCH",
                              "Claim valuation_date does not match result")
    candidates = [(item, item.fields[claim.field]) for item in bindings(result)
                  if item.entity_type == claim.entity.type and item.entity_id == claim.entity.id
                  and item.household_id == claim.household_id and claim.field in item.fields]
    binding, (value, unit) = _unique(candidates)
    if unit != claim.unit:
        raise ResolutionError("UNIT_MISMATCH", f"Field requires unit {unit}")
    if unit in {"count", "days"}:
        if type(value) is not int or value < 0:
            raise ResolutionError("INVALID_RECORDED_VALUE", "Recorded count/age must be an integer")
    else:
        try:
            if (not isinstance(value, str)
                    or not re.fullmatch(r"-?(0|[1-9][0-9]*)(\.[0-9]+)?", value)
                    or not Decimal(value).is_finite()):
                raise InvalidOperation
        except InvalidOperation:
            raise ResolutionError("INVALID_RECORDED_VALUE",
                                  "Recorded amount must be a finite string") from None
    return binding, value


def resolve_predicate(predicate: Predicate, result: dict) -> tuple[Binding, Any]:
    key = "meets_threshold" if predicate.kind == "threshold" else predicate.field
    candidates = [(item, item.predicates[key]) for item in bindings(result)
                  if item.entity_type == predicate.entity.type
                  and item.entity_id == predicate.entity.id and key in item.predicates]
    binding, value = _unique(candidates)
    if predicate.kind == "threshold":
        threshold = (result.get("data") or {}).get("applied_threshold_pct")
        if not isinstance(threshold, str) or type(value) is not bool:
            raise ResolutionError("MISSING_DECISION",
                                  "Threshold requires an explicit engine decision")
        expected = predicate.expect == "AT_OR_ABOVE"
    else:
        expected = predicate.expect
    if type(value) is not type(expected) or value != expected:
        raise ResolutionError("PREDICATE_MISMATCH",
                              "Predicate expectation does not match recorded fact")
    return binding, value


def entity_identifiers(result: dict) -> set[str]:
    """Known IDs only; labels and arbitrary numeric strings are never lint exemptions."""
    entities = bindings(result)
    identifiers = {item.entity_id for item in entities}
    identifiers.update(item.household_id for item in entities
                       if item.household_id is not None)
    data = result.get("data")
    if result.get("status") not in {"COMPLETE", "INCOMPLETE"} or not isinstance(data, dict):
        return identifiers
    # A position is addressed by its composite position ID for claims, but its
    # explicitly returned security ID is also a legitimate literal identifier.
    # Only known identity slots qualify; numeric values and labels never do.
    rows = _rows(data.get("positions"))
    rows += [row for account in _rows(data.get("reconciliation"))
             for row in _rows(account.get("rows")) if row.get("kind") == "quantity"]
    identifiers.update(row["security_id"] for row in rows
                       if isinstance(row.get("security_id"), str))
    return identifiers


def result_entity(result: dict) -> EntityReference:
    return EntityReference(type="result_summary", id=result["result_id"])
