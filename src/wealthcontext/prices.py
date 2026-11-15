"""Received custody closes selected by price date before correction knowledge time."""

import json
import sqlite3
from datetime import date, datetime

from wealthcontext.clock import utc_timestamp
from wealthcontext.facts import FactView
from wealthcontext.storage import canonical


def price_view(connection: sqlite3.Connection, day: date, known: datetime) -> FactView:
    """Choose the latest eligible close, retaining ambiguity within that close.

    Price histories intentionally overlap in valid time so older closes remain
    available for freshness checks. Their close date, unlike a generic entity's
    knowledge time, determines which eligible observation should value a position.
    """
    rows = connection.execute(
        "SELECT f.payload,f.source_id,f.recorded_at,f.valid_from,f.valid_to,s.source_system "
        "FROM facts f JOIN source_records s ON s.id=f.source_id "
        "WHERE f.kind='price' AND f.valid_from<=? AND f.recorded_at<=? "
        "AND s.feed_run_id IS NOT NULL ORDER BY f.recorded_at DESC,f.source_id",
        (day.isoformat(), utc_timestamp(known)),
    ).fetchall()
    versions: dict[tuple[str, str, str], list[dict]] = {}
    for row in rows:
        payload = json.loads(row["payload"])
        if date.fromisoformat(payload["price_date"]) > day:
            continue
        key = (payload["security_id"], payload["price_date"], row["valid_from"])
        group = versions.setdefault(key, [])
        if not group or group[0]["recorded_at"] == row["recorded_at"]:
            group.append({**payload, "source_id": row["source_id"],
                          "recorded_at": row["recorded_at"],
                          "source_system": row["source_system"], "valid_to": row["valid_to"]})
    # Supersede interval-end corrections before testing validity, then resolve
    # received revisions independently for each (security, close date).
    current: list[tuple[dict, bool]] = []
    for group in versions.values():
        activity = {day.isoformat() < item["valid_to"] for item in group}
        if True in activity:
            # Tied interval ends cannot resolve themselves by discarding the
            # ended assertion. Keep the complete competing set when any applies.
            current.extend((item, len(activity) > 1) for item in group)
    current.sort(key=lambda pair: pair[0]["recorded_at"], reverse=True)
    closes: dict[tuple[str, str], list[tuple[dict, bool]]] = {}
    for item, mixed_activity in current:
        group = closes.setdefault((item["security_id"], item["price_date"]), [])
        if not group or group[0][0]["recorded_at"] == item["recorded_at"]:
            group.append(({key: value for key, value in item.items() if key != "valid_to"},
                          mixed_activity))
    selected: dict[str, tuple[str, list[tuple[dict, bool]]]] = {}
    for (security, close), group in closes.items():
        if security not in selected or close > selected[security][0]:
            selected[security] = close, group
    result = FactView()
    for security, (_close, candidates) in selected.items():
        group = [item for item, _mixed in candidates]
        payloads = {canonical({key: value for key, value in item.items()
                               if key not in {"source_id", "recorded_at", "source_system"}})
                    for item in group}
        if any(mixed for _item, mixed in candidates) or len(payloads) > 1:
            result.ambiguous[security] = group
        else:
            result.values[security] = group[0]
    return result
