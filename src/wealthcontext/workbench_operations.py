"""Page helpers for the operations screens O1 and O2: structure only, never arithmetic.

Each helper arranges what one governed result returned. Nothing here adds, subtracts or
compares a figure: the O1 grid places the returned cells under their returned pairs and
business dates, and O2 looks each affected household up in the returned quality report.
Every count either page shows is a field a contract returned (ADR 0009 sections 3 and 4).
"""

from datetime import date
from urllib.parse import urlencode

from wealthcontext.generator import START

# The data window's first business day: O1's default range runs from it to the session's
# valuation date, so it never exceeds 25 business days (ADR 0009 point 11).
WINDOW_FIRST_DAY: date = START

# Each feed-cell state as a colour pair, a glyph and a word, so no state is carried by
# colour alone. The kinds are the design system's five badge kinds.
FEED_STATES: dict[str, dict[str, str]] = {
    "SUCCESS": {"kind": "complete", "glyph": "complete", "label": "Success"},
    "PARTIAL": {"kind": "incomplete", "glyph": "incomplete", "label": "Partial"},
    "FAILED": {"kind": "break", "glyph": "break", "label": "Failed"},
    "TIMEOUT": {"kind": "break", "glyph": "clock", "label": "Timed out"},
    "NOT_RECEIVED": {"kind": "denied", "glyph": "missing", "label": "Not received"},
}

# How O2 names each stored reason and household attribution; the code is shown with it.
REASONS: dict[str, str] = {
    "ACCOUNT_MANIFEST_MISMATCH": "Publication did not match the manifest",
    "SNAPSHOT_VALIDITY_MISMATCH": "Rows not valid on the business date",
    "DUPLICATE_ACCOUNT_COMPONENT": "Duplicate position or cash row",
    "INVALID_ACCOUNT_ROW": "A row failed validation",
    "EXPECTED_ACCOUNT_MISSING_FROM_MANIFEST": "Managed in CRM, omitted from the manifest",
    "OTHER": "Another stored reason",
}
ATTRIBUTIONS: dict[str, str] = {
    "RESOLVED": "Resolved", "CROSSWALK_UNAVAILABLE": "Account not resolved",
    "HOUSEHOLD_UNAVAILABLE": "Household not resolved", "AMBIGUOUS": "Ambiguous household",
}


def feed_run_link(run_id: str) -> str:
    """O2 for one run. The platform authorizes the caller before it looks the run up."""
    return "/workbench/feed-run?" + urlencode({"run_id": run_id})


def health_grid(data: dict) -> list[dict]:
    """The returned cells as one row per returned pair, in the returned business dates'
    order. A cell the platform did not return stays None, never a state of its own."""
    cells = {(item["business_date"], item["source_system"], item["feed"]): item
             for item in data["items"]}
    return [{"source_system": pair["source_system"], "feed": pair["feed"],
             "cells": [cells.get((day, pair["source_system"], pair["feed"]))
                       for day in data["business_dates"]]}
            for pair in data["pairs"]]


def health_cell(data: dict, source_system: str, feed: str) -> dict | None:
    """The one cell a single-day `feed_health` returned for a pair, if it returned it."""
    return next((item for item in data["items"]
                 if (item["source_system"], item["feed"]) == (source_system, feed)), None)


def household_completeness(quality: dict | None, household_ids: list[str]) -> list[dict]:
    """Each affected household's `complete` as the quality report returned it.

    A household the report did not return is said to be so, and why when the report says
    it was capped; nothing is inferred from its absence."""
    data = quality.get("data") if quality and quality.get("error") is None else None
    returned = ({item["household_id"]: item for item in data["items"]}
                if isinstance(data, dict) else {})
    rows = []
    for household_id in household_ids:
        item = returned.get(household_id)
        if item is not None:
            rows.append({"household_id": household_id,
                         "state": "COMPLETE" if item["complete"] else "INCOMPLETE",
                         "item": item})
        else:
            rows.append({"household_id": household_id, "item": None,
                         "state": ("NOT_RETURNED_CAPPED" if isinstance(data, dict)
                                   and data["truncated"] else "NOT_RETURNED")})
    return rows
