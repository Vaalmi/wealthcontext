"""Standalone CLI: consumes received exports, never imports the application."""

import argparse
import json
import sys
from datetime import date
from decimal import Decimal, InvalidOperation

from .finance import evaluate, valuation_date
from .history import State
from .wire import EvidenceError, instant, load_received_history


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("directory", help="directory containing index.json and received evidence")
    parser.add_argument("--known-as-of", required=True, help="aware knowledge-time cutoff")
    parser.add_argument("--as-of", default="2026-09-30", help="business date (default quarter end)")
    scope = parser.add_mutually_exclusive_group()
    scope.add_argument("--household", action="append", help="explicit household scope; repeatable")
    scope.add_argument(
        "--all-households", action="store_true", help="all received synthetic households"
    )
    parser.add_argument("--threshold-pct", default="10.00")
    parser.add_argument(
        "--validate-only", action="store_true", help="inspect all known received files"
    )
    parser.add_argument("--summary", action="store_true", help="omit detailed component evidence")
    args = parser.parse_args()
    if not args.validate_only and not (args.household or args.all_households):
        parser.error("choose --household or --all-households (oracle scope is not authorization)")
    try:
        history = load_received_history(args.directory)
        known = instant(args.known_as_of)
        day = valuation_date(date.fromisoformat(args.as_of))
        state = State(history, known)
        if args.validate_only:
            receipts = [item for item in history.receipts if item.recorded_at <= known]
            publications = list(state.publications.values())
            result = {
                "received_events": len(receipts),
                "unique_runs": len(
                    {(item.source, item.content_hash or item.delivery_id) for item in receipts}
                ),
                "received_file_events": sum(item.content is not None for item in receipts),
                "distinct_received_paths": len(
                    {item.content_name for item in receipts if item.content_name is not None}
                ),
                "distinct_received_contents": len(
                    {item.content_hash for item in receipts if item.content_hash is not None}
                ),
                "missing_content_events": sum(item.content is None for item in receipts),
                "invalid_file_headers": sum(
                    item.content is not None and not item.header_valid for item in receipts
                ),
                "malformed_row_fragments": sum(
                    row is None for item in receipts for row in item.rows
                ),
                "snapshot_account_publications": len(publications),
                "unavailable_snapshot_accounts": sum(not item.available for item in publications),
                "note": ("Wire/evidence and finance-relevant validation; "
                         "not a full ingestion-contract validator."),
            }
        else:
            household_view = state.view("household", day)
            households = args.household or sorted(
                household_view.values.keys() | household_view.ambiguous.keys()
            )
            result = evaluate(
                history,
                household_ids=households,
                as_of=day,
                known_as_of=known,
                threshold_pct=Decimal(args.threshold_pct),
            )
            if args.summary:
                result["households"] = [
                    {
                        key: value
                        for key, value in item.items()
                        if key
                        in {
                            "household_id",
                            "status",
                            "assets_usd",
                            "cash_usd",
                            "fresh_subtotal_usd",
                            "fresh_cash_subtotal_usd",
                            "stale_components",
                            "missing_components",
                            "missing_account_ids",
                            "included_account_ids",
                            "excluded_account_ids",
                            "quality_flags",
                            "issuer_exposures",
                            "fund_vehicle_exposures",
                        }
                    }
                    for item in result["households"]
                ]
        json.dump(result, sys.stdout, indent=2, sort_keys=True, allow_nan=False)
        sys.stdout.write("\n")
        return 0
    except (EvidenceError, ValueError, LookupError, OSError, InvalidOperation) as exc:
        json.dump({"error": str(exc)}, sys.stderr)
        sys.stderr.write("\n")
        return 2


if __name__ == "__main__":
    raise SystemExit(main())
