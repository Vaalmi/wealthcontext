"""Hand-authored received fixtures: expected numbers never call production code."""

import hashlib
import json
import subprocess
import sys
import tempfile
import unittest
from copy import deepcopy
from datetime import UTC, date, datetime, timedelta
from decimal import Decimal, localcontext
from pathlib import Path

from reference_oracle import EvidenceError, evaluate, load_received_history
from reference_oracle.history import State

ROOT = Path(__file__).resolve().parents[1]
DAY = date(2026, 9, 30)
KNOWN = datetime(2026, 10, 31, tzinfo=UTC)
HH = "WC-HH-0001"
A1 = {"account_id": "WC-AC-0001", "custodian": "A", "account_number": "00420017"}
A2 = {"account_id": "WC-AC-0002", "custodian": "A", "account_number": "00420018"}


def raw_json(value):
    # A test fixture writer, not an import of the oracle's wire implementation.
    return json.dumps(
        value, sort_keys=True, separators=(",", ":"), ensure_ascii=False, allow_nan=False
    ).encode()


def checksum(raw):
    return hashlib.sha256(raw).hexdigest()


def row(kind, record, day=DAY, **fields):
    return {
        "kind": kind,
        "source_record_id": record,
        "source_asserted_at": None,
        "valid_from": day.isoformat(),
        "valid_to": "9999-12-31",
        **fields,
    }


def security(
    identifier="WC-SEC-0001",
    *,
    currency="USD",
    country="US",
    issuer="WC-ISS-0001",
    kind="COMMON_STOCK",
    day=DAY,
):
    return row(
        "security",
        identifier,
        day,
        id=identifier,
        issuer_id=issuer,
        issuer_name="Synthetic issuer",
        name="Synthetic security",
        ticker="SYN",
        type=kind,
        currency=currency,
        listing_country=country,
        cusip="990000101",
        identifier_status="synthetic_unallocated",
        isin=None,
    )


def position(quantity="100", *, account=A1, sec="WC-SEC-0001", day=DAY):
    return row(
        "position",
        account["account_id"] + sec,
        day,
        custodian=account["custodian"],
        account_number=account["account_number"],
        security_id=sec,
        quantity=quantity,
        valid_to=(day + timedelta(days=1)).isoformat(),
    )


def cash(amount="500", *, account=A1, currency="USD", day=DAY):
    return row(
        "cash",
        account["account_id"] + currency,
        day,
        custodian=account["custodian"],
        account_number=account["account_number"],
        currency=currency,
        amount=amount,
        valid_to=(day + timedelta(days=1)).isoformat(),
    )


def quote(price="25", *, sec="WC-SEC-0001", currency="USD", day=DAY):
    return row(
        "price",
        sec,
        day,
        security_id=sec,
        price=price,
        currency=currency,
        price_date=day.isoformat(),
    )


def fix(currency, rate, *, day=DAY):
    return row(
        "fx",
        currency,
        day,
        currency=currency,
        usd_rate=rate,
        rate_at=day.isoformat() + "T15:00:00+00:00",
        valid_to=(day + timedelta(days=1)).isoformat(),
    )


class Export:
    def __init__(self, directory):
        self.directory = directory
        self.events = []

    def feed(
        self,
        source,
        feed,
        rows,
        *,
        day=DAY,
        at=None,
        accounts=(),
        intended=None,
        cut=0,
        status="SUCCESS",
        absent=False,
    ):
        header = {
            "format": "wealthcontext-feed-v1",
            "source_system": source,
            "feed": feed,
            "business_date": day.isoformat(),
        }
        expected_rows = rows if intended is None else intended
        expected_bytes = (
            raw_json(header) + b"\n" + b"".join(raw_json(item) + b"\n" for item in expected_rows)
        )
        contents = raw_json(header) + b"\n" + b"".join(raw_json(item) + b"\n" for item in rows)
        if cut:
            contents = contents[:-cut]
        declarations = []
        for account in accounts:
            group = [
                item
                for item in expected_rows
                if item.get("custodian") == account["custodian"]
                and item.get("account_number") == account["account_number"]
            ]
            digest = checksum(b"[" + b",".join(sorted(raw_json(item) for item in group)) + b"]")
            declarations.append({**account, "record_count": len(group), "digest": digest})
        manifest = {
            "source_system": source,
            "feed": feed,
            "business_date": day.isoformat(),
            "record_count": len(expected_rows),
            "checksum": checksum(expected_bytes),
            "expected_account_ids": [item["account_id"] for item in accounts],
            "accounts": declarations,
        }
        number = len(self.events)
        manifest_name, content_name = f"{number}.manifest.json", f"{number}.received.jsonl"
        manifest_bytes = raw_json(manifest)
        (self.directory / manifest_name).write_bytes(manifest_bytes)
        if not absent:
            (self.directory / content_name).write_bytes(contents)
        entry = {
            "manifest": manifest_name,
            "manifest_sha256": checksum(manifest_bytes),
            "content": None if absent else content_name,
            "content_sha256": None if absent else checksum(contents),
            "delivery_id": f"hand-delivery-{number}",
            "upstream_status": status,
            "recorded_at": at or day.isoformat() + "T22:00:00+00:00",
        }
        self.events.append(entry)
        self.save()
        return entry

    def save(self):
        (self.directory / "index.json").write_bytes(
            raw_json({"version": 1, "seed": "ignored", "events": self.events})
        )

    def crm(self, accounts=(A1,), *, day=DAY, extra=(), at=None):
        rows = [row("household", HH, day, id=HH, name="Synthetic household")]
        for account in accounts:
            rows.extend(
                [
                    row(
                        "account",
                        account["account_id"],
                        day,
                        id=account["account_id"],
                        custodian=account["custodian"],
                        account_number=account["account_number"],
                        is_managed=account.get("managed", True),
                    ),
                    row(
                        "reporting",
                        "report-" + account["account_id"],
                        day,
                        account_id=account["account_id"],
                        household_id=HH,
                    ),
                ]
            )
        self.feed("CRM", "crm", rows + list(extra), day=day, at=at)

    def snapshots(self, rows, *, day=DAY, accounts=(A1,), custody=None):
        self.feed("PAS-A", "pas_snapshots", rows, day=day, accounts=accounts)
        self.feed(
            "CUSTODY-A",
            "custody_snapshots",
            rows if custody is None else custody,
            day=day,
            accounts=accounts,
        )


class OracleTests(unittest.TestCase):
    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory(prefix="wealthcontext-reference-oracle-")
        self.addCleanup(self.temporary.cleanup)
        self.directory = Path(self.temporary.name)
        self.export = Export(self.directory)

    def evaluate(self, *, day=DAY, known=KNOWN, threshold="10.00"):
        return evaluate(
            load_received_history(self.directory),
            household_ids=[HH],
            as_of=day,
            known_as_of=known,
            threshold_pct=Decimal(threshold),
        )

    def household(self, **kwargs):
        return self.evaluate(**kwargs)["households"][0]

    def usd(self, *, day=DAY, quantity="100", price="25", amount="500"):
        self.export.crm(day=day)
        self.export.feed("SECMASTER", "securities", [security(day=day)], day=day)
        self.export.snapshots([position(quantity, day=day), cash(amount, day=day)], day=day)
        self.export.feed("CUSTODY-A", "prices", [quote(price, day=day)], day=day)

    def test_usd_assets_use_only_pas_cash_and_preserve_unknown_source_time(self):
        self.usd()
        result = self.household()
        self.assertEqual(result["status"], "COMPLETE")
        self.assertEqual(result["assets_usd"], "3000.00")
        self.assertEqual(result["cash_usd"], "500.00")
        self.assertEqual(result["positions"][0]["market_value_usd"], "2500.00")
        self.assertEqual(result["quality_flags"], [])
        self.assertIsNone(
            load_received_history(self.directory).receipts[0].rows[0]["source_asserted_at"]
        )

    def test_hand_fx_625_and_rate_evidence(self):
        self.export.crm()
        self.export.feed("SECMASTER", "securities", [security(currency="EUR", country="DE")])
        self.export.snapshots([position("10"), cash("100", currency="CAD")])
        self.export.feed("CUSTODY-A", "prices", [quote("50", currency="EUR")])
        self.export.feed("FX", "fx", [fix("EUR", "1.10"), fix("CAD", "0.75")])
        result = self.household()
        self.assertEqual(result["assets_usd"], "625.00")
        self.assertEqual(result["positions"][0]["market_value_usd"], "550.00")
        self.assertEqual(result["cash_usd"], "75.00")
        self.assertEqual(result["positions"][0]["fx"]["rate_at"], "2026-09-30T15:00:00+00:00")

    def foreign(self, *, missing=False):
        day = date(2026, 10, 8)
        self.export.crm(day=day)
        definitions, holdings, prices, fixes = [], [cash("100", day=day)], [], []
        for index, (currency, country, quantity, price, rate) in enumerate(
            (
                ("EUR", "DE", "10", "20", "1.10"),
                ("CAD", "CA", "20", "15", "0.75"),
                ("GBP", "GB", "30", "10", "1.25"),
            ),
            1,
        ):
            sec = f"WC-SEC-{index:04}"
            definitions.append(security(sec, currency=currency, country=country, day=day))
            holdings.extend(
                [position(quantity, sec=sec, day=day), cash("100", currency=currency, day=day)]
            )
            prices.append(quote(price, sec=sec, currency=currency, day=day))
            if currency != "GBP" or not missing:
                fixes.append(fix(currency, rate, day=day))
        self.export.feed("SECMASTER", "securities", definitions, day=day)
        self.export.snapshots(holdings, day=day)
        self.export.feed("CUSTODY-A", "prices", prices, day=day)
        self.export.feed("FX", "fx", fixes, day=day)
        return day

    def test_s10_complete_foreign_assets(self):
        result = self.household(day=self.foreign())
        self.assertEqual(result["assets_usd"], "1230.00")
        self.assertEqual(result["cash_usd"], "410.00")
        self.assertEqual(
            [item["market_value_usd"] for item in result["positions"]],
            ["220.00", "225.00", "375.00"],
        )

    def test_s10_missing_gbp_fix_has_no_total_or_partial_percentage(self):
        day = self.foreign(missing=True)
        result = self.evaluate(day=day)
        facts = result["households"][0]
        self.assertEqual(facts["status"], "INCOMPLETE")
        self.assertEqual(facts["fresh_subtotal_usd"], "730.00")
        self.assertNotIn("assets_usd", facts)
        self.assertNotIn("issuer_exposures", facts)
        self.assertEqual(len(facts["missing_components"]), 2)
        self.assertEqual({item["code"] for item in facts["blocking_flags"]}, {"FX_UNAVAILABLE"})
        self.assertEqual(result["breaches"], [])
        self.assertEqual(result["indeterminate"][0]["fresh_subtotal_usd"], "730.00")

    def test_us_stale_four_days_preserves_last_known_value(self):
        day, close = date(2026, 10, 15), date(2026, 10, 9)
        self.export.crm(day=day)
        self.export.feed("SECMASTER", "securities", [security(day=day)], day=day)
        self.export.snapshots([position("40", day=day), cash("300", day=day)], day=day)
        self.export.feed("CUSTODY-A", "prices", [quote("28", day=close)], day=close)
        facts = self.household(day=day)
        self.assertEqual(facts["fresh_subtotal_usd"], "300.00")
        self.assertNotIn("assets_usd", facts)
        self.assertEqual(facts["stale_components"][0]["last_known_value_usd"], "1120.00")
        self.assertEqual(facts["stale_components"][0]["age_business_days"], 4)

    def test_non_us_one_day_fresh_two_days_stale_and_us_one_day_stale(self):
        for country, age, expected in (
            ("DE", 1, "COMPLETE"),
            ("DE", 2, "INCOMPLETE"),
            ("US", 1, "INCOMPLETE"),
        ):
            with self.subTest(country=country, age=age):
                self.export = Export(self.directory)
                close = DAY - timedelta(days=age)
                self.export.crm()
                self.export.feed("SECMASTER", "securities", [security(country=country)])
                self.export.snapshots([position("10"), cash("100")])
                self.export.feed("CUSTODY-A", "prices", [quote("20", day=close)], day=close)
                self.assertEqual(self.household()["status"], expected)

    def test_b1_equality_and_s15_mmf_denominator_without_issuer_breach(self):
        self.export.crm()
        self.export.feed(
            "SECMASTER", "securities", [security(), security("WC-SEC-0017", kind="MMF")]
        )
        self.export.snapshots([position("4"), position("150", sec="WC-SEC-0017"), cash("750")])
        self.export.feed("CUSTODY-A", "prices", [quote(), quote("1", sec="WC-SEC-0017")])
        result = self.evaluate()
        facts = result["households"][0]
        self.assertEqual(facts["assets_usd"], "1000.00")
        self.assertEqual(len(result["breaches"]), 1)
        self.assertEqual(result["breaches"][0]["concentration_pct"], "10.00")
        self.assertEqual(facts["fund_vehicle_exposures"]["items"][0]["allocation_pct"], "15.00")
        self.assertEqual(facts["issuer_exposures"]["item_count"], 1)

    def test_display_rounding_to_threshold_does_not_create_breach(self):
        self.usd(quantity="1", price="999.96", amount="9000.04")
        result = self.evaluate()
        self.assertEqual(result["households"][0]["assets_usd"], "10000.00")
        self.assertEqual(
            result["households"][0]["issuer_exposures"]["items"][0]["concentration_pct"], "10.00"
        )
        self.assertEqual(result["breaches"], [])

    def test_sum_unrounded_position_values_and_ignore_callers_decimal_context(self):
        self.export.crm()
        self.export.feed("SECMASTER", "securities", [security(), security("WC-SEC-0002")])
        self.export.snapshots([position("1"), position("1", sec="WC-SEC-0002")])
        self.export.feed("CUSTODY-A", "prices", [quote("0.005"), quote("0.005", sec="WC-SEC-0002")])
        with localcontext() as caller:
            caller.prec = 3
            result = self.household()
        self.assertEqual(result["assets_usd"], "0.01")
        self.assertEqual(
            [item["market_value_usd"] for item in result["positions"]], ["0.01", "0.01"]
        )

    def test_precision_boundary_combines_separate_position_and_cash_subtotals(self):
        self.export.crm(accounts=(A1, A2))
        self.export.feed("SECMASTER", "securities", [security()])
        self.export.snapshots([cash("0.4", account=A2), position("1"), cash("0.4")],
                              accounts=(A1, A2))
        self.export.feed("CUSTODY-A", "prices", [quote("1000000000000000000000000000")])
        result = self.household()
        # At precision 28, the separate cash subtotal 0.8 rounds the combined
        # 10^27 + 0.8 up to 10^27 + 1. Adding each 0.4 directly would lose both.
        self.assertEqual(result["assets_usd"], "1000000000000000000000000001.00")
        self.assertEqual(result["cash_usd"], "0.80")

    def test_precision_order_uses_component_ids_not_received_position_order(self):
        for reversed_rows in (False, True):
            with self.subTest(reversed_rows=reversed_rows):
                self.export = Export(self.directory)
                self.export.crm()
                identifiers = [f"WC-SEC-{index:04}" for index in (1, 2, 3)]
                self.export.feed("SECMASTER", "securities",
                                 [security(identifier) for identifier in identifiers])
                rows = [position("1", sec=identifier) for identifier in identifiers]
                self.export.snapshots(list(reversed(rows)) if reversed_rows else rows)
                self.export.feed("CUSTODY-A", "prices", [
                    quote("1000000000000000000000000000"),
                    quote("0.6", sec=identifiers[1]), quote("0.6", sec=identifiers[2]),
                ])
                result = self.household()
                # Sorted accumulation rounds each addition of 0.6 up by one;
                # grouping the small terms first would instead add only one.
                expected = "1000000000000000000000000002.00"
                self.assertEqual(result["assets_usd"], expected)
                self.assertEqual(result["issuer_exposures"]["items"][0]["market_value_usd"],
                                 expected)

    def test_precision_order_uses_component_ids_not_received_cash_order(self):
        self.export.crm()
        self.export.snapshots([
            cash("0.6", currency="USD"), cash("0.6", currency="EUR"),
            cash("1000000000000000000000000000", currency="CAD"),
        ])
        self.export.feed("FX", "fx", [fix("CAD", "1"), fix("EUR", "1")])
        result = self.household()
        self.assertEqual(result["cash_usd"], "1000000000000000000000000002.00")
        self.assertEqual(result["assets_usd"], result["cash_usd"])

    def test_b2_verified_empty_account_is_complete_but_indeterminate(self):
        self.export.crm()
        self.export.snapshots([])
        result = self.evaluate()
        self.assertEqual(result["households"][0]["assets_usd"], "0.00")
        self.assertEqual(result["households"][0]["status"], "COMPLETE")
        self.assertEqual(result["breaches"], [])
        self.assertEqual(result["indeterminate"][0]["reason"], "NONPOSITIVE_ASSETS")

    def test_funds_never_group_by_sponsor_and_tied_issuers_sort_by_id(self):
        self.export.crm()
        self.export.feed(
            "SECMASTER",
            "securities",
            [
                security("WC-SEC-0001", issuer="WC-ISS-0002"),
                security("WC-SEC-0002", issuer="WC-ISS-0001"),
                security("WC-SEC-0003", issuer="WC-ISS-0001"),
                security("WC-SEC-0004", issuer="WC-ISS-0001", kind="ETF"),
                security("WC-SEC-0005", issuer="WC-ISS-0001", kind="MUTUAL_FUND"),
            ],
        )
        self.export.snapshots(
            [
                position(amount, sec=f"WC-SEC-{index:04}")
                for index, amount in enumerate(("2", "1", "1", "3", "4"), 1)
            ]
        )
        self.export.feed(
            "CUSTODY-A", "prices", [quote("100", sec=f"WC-SEC-{index:04}") for index in range(1, 6)]
        )
        facts = self.household()
        self.assertEqual(facts["assets_usd"], "1100.00")
        self.assertEqual(
            [
                (item["issuer_id"], item["market_value_usd"], item["rank"])
                for item in facts["issuer_exposures"]["items"]
            ],
            [("WC-ISS-0001", "200.00", 1), ("WC-ISS-0002", "200.00", 2)],
        )
        self.assertEqual(facts["fund_vehicle_exposures"]["item_count"], 2)
        self.assertEqual(
            [
                (item["security_id"], item["market_value_usd"], item["rank"])
                for item in facts["fund_vehicle_exposures"]["items"]
            ],
            [("WC-SEC-0005", "400.00", 1), ("WC-SEC-0004", "300.00", 2)],
        )
        self.assertTrue(
            all(
                "concentration_pct" not in item for item in facts["fund_vehicle_exposures"]["items"]
            )
        )

    def test_s09_exact_cutoff_uses_corrected_received_snapshot(self):
        self.usd(amount="1000")
        correction = "2026-10-06T14:00:00+00:00"
        self.export.feed(
            "PAS-A", "pas_snapshots", [position("110"), cash("799")], at=correction, accounts=[A1]
        )
        before = datetime(2026, 10, 6, 13, 59, 59, 999999, tzinfo=UTC)
        self.assertEqual(self.household(known=before)["assets_usd"], "3500.00")
        self.assertEqual(
            self.household(known=datetime.fromisoformat(correction))["assets_usd"], "3549.00"
        )
        self.assertEqual(
            self.household(known=datetime(2055, 1, 1, tzinfo=UTC))["assets_usd"], "3549.00"
        )

    def test_latest_close_date_wins_over_late_correction_to_older_close(self):
        self.usd(quantity="1", price="25", amount="0")
        old = DAY - timedelta(days=1)
        self.export.feed(
            "CUSTODY-A", "prices", [quote("999", day=old)], day=old, at="2026-10-02T14:00:00+00:00"
        )
        self.assertEqual(self.household()["assets_usd"], "25.00")
        self.export.feed("CUSTODY-A", "prices", [quote("26")], at="2026-10-03T14:00:00+00:00")
        self.assertEqual(self.household()["assets_usd"], "26.00")

    def test_missing_current_fix_never_carries_prior_fix_forward(self):
        self.export.crm()
        self.export.snapshots([cash("100", currency="EUR")])
        old = DAY - timedelta(days=1)
        self.export.feed("FX", "fx", [fix("EUR", "1.10", day=old)], day=old)
        self.export.feed("FX", "fx", [])
        self.assertEqual(self.household()["fresh_subtotal_usd"], "0.00")
        self.assertNotIn("assets_usd", self.household())

    def test_failed_current_pas_has_no_prior_day_carry_forward(self):
        old = DAY - timedelta(days=1)
        self.usd(day=old)
        self.export.feed(
            "PAS-A",
            "pas_snapshots",
            [],
            intended=[position(), cash()],
            accounts=[A1],
            status="FAILED",
            absent=True,
        )
        facts = self.household()
        self.assertEqual(facts["missing_account_ids"], [A1["account_id"]])
        self.assertEqual(facts["positions"], [])
        self.assertNotIn("assets_usd", facts)

    def test_real_truncation_quarantines_whole_account_but_salvages_other_account(self):
        self.export.crm(accounts=(A1, A2))
        self.export.feed("SECMASTER", "securities", [security()])
        full = [cash("7", account=A2), position(), cash()]
        self.export.feed(
            "PAS-A", "pas_snapshots", full, accounts=[A1, A2], cut=18, status="TIMEOUT"
        )
        self.export.feed("CUSTODY-A", "custody_snapshots", full, accounts=[A1, A2])
        self.export.feed("CUSTODY-A", "prices", [quote()])
        facts = self.household()
        self.assertEqual(facts["fresh_subtotal_usd"], "7.00")
        self.assertEqual(facts["positions"], [])
        self.assertEqual(facts["missing_account_ids"], [A1["account_id"]])

    def test_secondary_truncation_does_not_block_assets(self):
        self.usd()
        self.export.feed(
            "CUSTODY-A",
            "custody_snapshots",
            [position(), cash()],
            accounts=[A1],
            cut=18,
            status="TIMEOUT",
            at="2026-10-01T14:00:00+00:00",
        )
        facts = self.household()
        self.assertEqual(facts["assets_usd"], "3000.00")
        self.assertEqual(facts["blocking_flags"], [])
        self.assertIn(
            "RECONCILIATION_UNAVAILABLE", {item["code"] for item in facts["quality_flags"]}
        )

    def test_manifest_omission_is_not_zero_and_held_away_requires_no_manifest(self):
        held = {
            "account_id": "WC-AC-0031",
            "custodian": "A",
            "account_number": "HELD",
            "managed": False,
        }
        self.export.crm(accounts=(A1, A2, held))
        self.export.snapshots([cash("100")], accounts=(A1,))
        facts = self.household()
        self.assertEqual(facts["missing_account_ids"], [A2["account_id"]])
        self.assertEqual(facts["excluded_account_ids"], [held["account_id"]])
        self.assertEqual(facts["fresh_subtotal_usd"], "100.00")

    def test_corrected_snapshot_removes_old_components_and_reingest_is_noop(self):
        self.usd()
        original = self.household()
        self.export.events.append(deepcopy(self.export.events[-1]))
        self.export.events[-1]["recorded_at"] = "2030-01-01T00:00:00+00:00"
        self.export.save()
        self.assertEqual(original, self.household())
        self.export.feed(
            "PAS-A", "pas_snapshots", [cash("500")], accounts=[A1], at="2026-10-02T14:00:00+00:00"
        )
        self.assertEqual(self.household()["assets_usd"], "500.00")
        self.assertEqual(self.household()["positions"], [])

    def test_same_time_conflicting_prices_remain_ambiguous(self):
        self.usd(quantity="1", amount="100")
        changed = quote("26")
        changed["source_record_id"] = "competing-price"
        self.export.feed("CUSTODY-A", "prices", [changed])
        facts = self.household()
        self.assertNotIn("assets_usd", facts)
        self.assertEqual(facts["fresh_subtotal_usd"], "100.00")
        self.assertEqual({flag["code"] for flag in facts["blocking_flags"]}, {"AMBIGUOUS"})

    def test_unknown_isin_default_has_same_assertion_meaning_without_rewriting_raw_evidence(self):
        self.usd(quantity="1", amount="7")
        omitted = security()
        omitted.pop("isin")
        self.export.feed("SECMASTER", "securities", [omitted])
        facts = self.household()
        self.assertEqual(facts["assets_usd"], "32.00")
        rows = [item for receipt in load_received_history(self.directory).receipts
                for item in receipt.rows if item and item["kind"] == "security"]
        self.assertEqual(len(rows), 2)
        self.assertIsNone(rows[0]["isin"])
        self.assertNotIn("isin", rows[1])

    def test_identical_fx_payloads_from_tied_different_file_dates_remain_ambiguous(self):
        for reverse in (False, True):
            with self.subTest(reverse=reverse):
                self.export = Export(self.directory)
                self.export.crm()
                self.export.feed("SECMASTER", "securities",
                                 [security(currency="GBP", country="GB")])
                self.export.snapshots([position("1"), cash("7")])
                self.export.feed("CUSTODY-A", "prices", [quote("25", currency="GBP")])
                dates = [DAY, DAY - timedelta(days=1)]
                for day in reversed(dates) if reverse else dates:
                    self.export.feed("FX", "fx", [fix("GBP", "1.25")], day=day,
                                     at="2026-09-30T22:00:00Z")
                facts = self.household()
                self.assertEqual(facts["status"], "INCOMPLETE")
                self.assertEqual(facts["fresh_subtotal_usd"], "7.00")
                state = State(load_received_history(self.directory), KNOWN)
                self.assertEqual(state.fx("GBP", DAY), (None, "AMBIGUOUS_FX"))

    def test_tied_snapshot_rekeyed_raw_row_is_unresolved_until_later_publication(self):
        self.usd(quantity="1", amount="7")
        self.export.feed("PAS-A", "pas_snapshots", [
            {**position("1"), "source_record_id": "different-received-identity"}, cash("7"),
        ], accounts=(A1,))
        cutoff = datetime(2026, 10, 2, 14, tzinfo=UTC)
        before = cutoff - timedelta(microseconds=1)
        unresolved = self.household(known=before)
        self.assertEqual(unresolved["fresh_subtotal_usd"], "0.00")
        self.assertNotIn("assets_usd", unresolved)
        self.export.feed("PAS-A", "pas_snapshots", [position("2"), cash("7")],
                         accounts=(A1,), at=cutoff.isoformat())
        self.assertEqual(self.household(known=before), unresolved)
        self.assertEqual(self.household(known=cutoff)["assets_usd"], "57.00")

    def test_missing_or_ambiguous_security_does_not_hide_independent_price_diagnostic(self):
        for security_state in ("missing", "ambiguous", "valid"):
            for price_state in ("missing", "ambiguous", "valid"):
                with self.subTest(security=security_state, price=price_state):
                    self.export = Export(self.directory)
                    self.export.crm()
                    self.export.snapshots([position("1"), cash("7")])
                    definitions = [] if security_state == "missing" else [security()]
                    if security_state == "ambiguous":
                        definitions.append({**security(), "name": "Conflicting security"})
                    if definitions:
                        self.export.feed("SECMASTER", "securities", definitions)
                    prices = [] if price_state == "missing" else [quote("25")]
                    if price_state == "ambiguous":
                        prices.append(quote("26"))
                    if prices:
                        self.export.feed("CUSTODY-A", "prices", prices)
                    result = self.household()
                    expected = set()
                    if security_state != "valid":
                        expected.add(("AMBIGUOUS" if security_state == "ambiguous"
                                      else "SOURCE_UNAVAILABLE", "SECURITY"))
                    if price_state != "valid":
                        expected.add(("AMBIGUOUS" if price_state == "ambiguous"
                                      else "SOURCE_UNAVAILABLE",
                                      "AMBIGUOUS_PRICE" if price_state == "ambiguous"
                                      else "MISSING_PRICE"))
                    self.assertEqual({(flag["code"], flag["reason"])
                                      for flag in result["blocking_flags"]}, expected)
                    if expected:
                        self.assertEqual(result["status"], "INCOMPLETE")
                        self.assertEqual(result["fresh_subtotal_usd"], "7.00")
                        self.assertNotIn("assets_usd", result)
                    else:
                        self.assertEqual(result["assets_usd"], "32.00")

    def test_valid_time_end_correction_does_not_resurrect_member(self):
        person = row(
            "person", "person", id="WC-PER-1", name="Synthetic person", external_person_key="P1"
        )
        member = row("membership", "member", person_id="WC-PER-1", household_id=HH)
        self.export.crm(extra=(person, member))
        self.export.snapshots([])
        self.export.feed(
            "CRM",
            "crm",
            [{**member, "valid_to": "2026-10-01"}],
            day=date(2026, 10, 1),
            at="2026-10-01T13:00:00+00:00",
        )
        next_day = date(2026, 10, 1)
        self.export.snapshots([], day=next_day)
        # Missing a departed person's current record cannot block this account.
        self.export.feed(
            "CRM",
            "crm",
            [{**person, "valid_to": "2026-10-01"}],
            day=next_day,
            at="2026-10-01T13:00:00+00:00",
        )
        self.assertEqual(self.household(day=next_day)["status"], "COMPLETE")

    def test_closed_account_balance_is_included(self):
        self.usd()
        ownership = row(
            "ownership",
            "owner",
            custodian="A",
            account_number=A1["account_number"],
            status="CLOSED",
            title="Synthetic account",
            registration="INDIVIDUAL",
            owners=[{"entity_id": "WC-PER-1", "entity_type": "person", "role": "owner"}],
        )
        self.export.feed("CUSTODY-A", "custody_accounts", [ownership])
        facts = self.household()
        self.assertEqual(facts["assets_usd"], "3000.00")
        self.assertIn(
            "CLOSED_ACCOUNT_WITH_BALANCE", {item["code"] for item in facts["quality_flags"]}
        )

    def test_invalid_later_ownership_status_cannot_remove_closed_balance_warning(self):
        for index, status in enumerate(("INVALID", "closed", " CLOSED ", "OPEN", "CLOSED")):
            with self.subTest(status=status):
                directory = self.directory / f"ownership-{index}"
                directory.mkdir()
                export = Export(directory)
                export.crm()
                export.snapshots([cash("7")])
                ownership = row(
                    "ownership", "owner", custodian="A", account_number=A1["account_number"],
                    status="CLOSED", title="Synthetic account", registration="INDIVIDUAL",
                    owners=[{"entity_id": "WC-PER-1", "entity_type": "person", "role": "owner"}],
                )
                export.feed("CUSTODY-A", "custody_accounts", [ownership])
                export.feed("CUSTODY-A", "custody_accounts", [{**ownership, "status": status}],
                            at="2026-10-02T14:00:00Z")
                history = load_received_history(directory)
                for hour in (13, 14):
                    facts = evaluate(history, household_ids=[HH], as_of=DAY,
                                     known_as_of=datetime(2026, 10, 2, hour, tzinfo=UTC))[
                        "households"
                    ][0]
                    self.assertEqual(facts["assets_usd"], "7.00")
                    self.assertEqual(
                        "CLOSED_ACCOUNT_WITH_BALANCE" in {
                            item["code"] for item in facts["quality_flags"]
                        },
                        status != "OPEN" or hour == 13,
                    )

    def test_price_correction_obeys_knowledge_cutoff(self):
        self.usd(quantity="1", amount="0")
        self.export.feed("CUSTODY-A", "prices", [quote("30")], at="2026-10-02T14:00:00+00:00")
        self.assertEqual(
            self.household(known=datetime(2026, 10, 2, 13, tzinfo=UTC))["assets_usd"], "25.00"
        )
        self.assertEqual(
            self.household(known=datetime(2026, 10, 2, 14, tzinfo=UTC))["assets_usd"], "30.00"
        )

    def test_index_byte_hash_tampering_is_rejected(self):
        self.usd()
        (self.directory / self.export.events[0]["content"]).write_bytes(b"changed")
        with self.assertRaises(EvidenceError):
            load_received_history(self.directory)

    def test_invalid_receipt_timestamp_raises_public_evidence_error(self):
        self.usd()
        for timestamp in (
            "2026-02-30T15:00:00Z", "2026-09-30T24:00:00Z", "1" * 4301,
            "0001-01-01T00:00:00+00:01", "9999-12-31T23:59:59-00:01",
        ):
            with self.subTest(timestamp=timestamp[:40]):
                self.export.events[0]["recorded_at"] = timestamp
                self.export.save()
                with self.assertRaises(EvidenceError):
                    load_received_history(self.directory)

    def test_manifest_source_and_feed_types_raise_public_evidence_error(self):
        self.usd()
        entry = self.export.events[0]
        path = self.directory / entry["manifest"]
        original = json.loads(path.read_bytes())
        for field in ("source_system", "feed"):
            for value in ([], {}):
                with self.subTest(field=field, value=value):
                    altered = raw_json({**original, field: value})
                    path.write_bytes(altered)
                    entry["manifest_sha256"] = checksum(altered)
                    self.export.save()
                    with self.assertRaises(EvidenceError):
                        load_received_history(self.directory)

    def test_export_path_traversal_and_naive_cutoff_are_rejected(self):
        self.usd()
        with self.assertRaises(EvidenceError):
            self.household(known=datetime(2026, 10, 1))
        self.export.events[0]["manifest"] = "../outside.json"
        self.export.save()
        with self.assertRaises(EvidenceError):
            load_received_history(self.directory)

    def test_weekend_resolution_and_window_bounds(self):
        friday = date(2026, 10, 16)
        self.usd(day=friday)
        self.assertEqual(
            self.household(day=date(2026, 10, 18))["valuation_date"], friday.isoformat()
        )
        with self.assertRaisesRegex(ValueError, "AS_OF_OUT_OF_RANGE"):
            self.household(day=date(2026, 10, 19))

    def test_cli_all_households_resolves_weekend_before_scoping(self):
        friday = date(2026, 10, 16)
        self.usd(day=friday)
        self.export.feed(
            "CRM", "crm", [row("household", "bounded-household", friday, id=HH,
                                name="Synthetic household", valid_to="2026-10-17")],
            day=friday, at="2026-10-16T23:00:00Z",
        )
        process = subprocess.run(
            [sys.executable, "-B", "-S", "-m", "reference_oracle", str(self.directory),
             "--all-households", "--as-of", "2026-10-18",
             "--known-as-of", "2026-10-31T00:00:00Z"],
            cwd=ROOT, capture_output=True, text=True, check=False,
        )
        self.assertEqual(process.returncode, 0, process.stderr)
        result = json.loads(process.stdout)
        self.assertEqual(result["valuation_date"], "2026-10-16")
        self.assertEqual(result["evaluated_household_count"], 1)
        self.assertEqual(result["households"][0]["assets_usd"], "3000.00")

    def test_cli_all_households_preserves_ambiguous_household_in_full_scope(self):
        self.usd()
        self.export.feed("CRM", "crm", [row("household", "competing-name", id=HH,
                                              name="Different synthetic household")])
        process = subprocess.run(
            [sys.executable, "-B", "-S", "-m", "reference_oracle", str(self.directory),
             "--all-households", "--as-of", DAY.isoformat(),
             "--known-as-of", KNOWN.isoformat()],
            cwd=ROOT, capture_output=True, text=True, check=False,
        )
        self.assertEqual(process.returncode, 0, process.stderr)
        result = json.loads(process.stdout)
        self.assertEqual(result["evaluated_household_count"], 1)
        self.assertEqual(result["indeterminate_household_count"], 1)
        self.assertEqual(result["indeterminate"][0]["household_id"], HH)
        self.assertEqual(result["households"][0]["status"], "INCOMPLETE")
        self.assertEqual(result["households"][0]["blocking_flags"], [{
            "code": "AMBIGUOUS_ENTITY", "subject": HH, "severity": "warning",
            "reason": "HOUSEHOLD",
        }])

    def test_cli_invalid_threshold_returns_json_error(self):
        self.usd()
        for threshold in ("not-a-number", "1.001", "51"):
            with self.subTest(threshold=threshold):
                process = subprocess.run(
                    [sys.executable, "-B", "-S", "-m", "reference_oracle", str(self.directory),
                     "--household", HH, "--threshold-pct", threshold,
                     "--known-as-of", "2026-10-31T00:00:00Z"],
                    cwd=ROOT, capture_output=True, text=True, check=False,
                )
                self.assertEqual(process.returncode, 2)
                self.assertIn("error", json.loads(process.stderr))
                self.assertEqual(process.stdout, "")

    def test_no_production_imports_even_when_production_is_unavailable(self):
        self.usd()
        code = """
import importlib.abc, sys
class Forbidden(importlib.abc.MetaPathFinder):
    def find_spec(self, fullname, path=None, target=None):
        if fullname == 'wealthcontext' or fullname.startswith('wealthcontext.'):
            raise RuntimeError('production import forbidden')
sys.meta_path.insert(0, Forbidden())
sys.path.insert(0, sys.argv[1])
from reference_oracle import evaluate, load_received_history
from datetime import date, datetime, UTC
result = evaluate(load_received_history(sys.argv[2]), household_ids=['WC-HH-0001'],
                  as_of=date(2026,9,30), known_as_of=datetime(2026,10,31,tzinfo=UTC))
assert result['households'][0]['assets_usd'] == '3000.00'
assert not any(name.startswith('wealthcontext') for name in sys.modules)
"""
        process = subprocess.run(
            [sys.executable, "-B", "-I", "-S", "-c", code, str(ROOT), str(self.directory)],
            capture_output=True,
            text=True,
            check=False,
        )
        self.assertEqual(process.returncode, 0, process.stderr)

    def test_delayed_crm_resolves_already_received_financial_and_ownership_evidence(self):
        self.export.crm(accounts=(), at="2026-09-30T20:00:00+00:00")
        self.export.feed("SECMASTER", "securities", [security()])
        self.export.snapshots([position(), cash()])
        self.export.feed("CUSTODY-A", "prices", [quote()])
        self.export.feed(
            "CUSTODY-A",
            "custody_accounts",
            [
                row(
                    "ownership",
                    "original-owner",
                    custodian="A",
                    account_number=A1["account_number"],
                    status="CLOSED",
                    title="Synthetic account",
                    registration="INDIVIDUAL",
                    owners=[{"entity_id": "WC-PER-1", "entity_type": "person", "role": "owner"}],
                )
            ],
        )
        before = self.household(known=datetime(2026, 9, 30, 22, tzinfo=UTC))
        self.assertEqual(before["included_account_ids"], [])
        self.export.crm(at="2026-09-30T23:00:00+00:00")
        after = self.household(known=datetime(2026, 9, 30, 23, tzinfo=UTC))
        self.assertEqual(after["assets_usd"], "3000.00")
        self.assertIn(
            "CLOSED_ACCOUNT_WITH_BALANCE", {item["code"] for item in after["quality_flags"]}
        )
        self.assertEqual(
            after["pas_publications"][0]["evidence"]["recorded_at"], "2026-09-30T22:00:00+00:00"
        )

    def test_retrospective_crosswalk_swap_rebinds_without_reingestion(self):
        self.export.crm(accounts=(A1, A2))
        self.export.feed("SECMASTER", "securities", [security()])
        self.export.snapshots(
            [position("1"), cash("10"), position("2", account=A2), cash("20", account=A2)],
            accounts=(A1, A2),
        )
        self.export.feed("CUSTODY-A", "prices", [quote("20")])
        before = self.household()
        swapped = (
            {**A1, "account_number": A2["account_number"]},
            {**A2, "account_number": A1["account_number"]},
        )
        self.export.crm(accounts=swapped, at="2026-10-02T14:00:00+00:00")
        historical = self.household(known=datetime(2026, 10, 2, 13, tzinfo=UTC))
        after = self.household(known=datetime(2026, 10, 2, 14, tzinfo=UTC))
        self.assertEqual([item["quantity"] for item in before["positions"]], ["1", "2"])
        self.assertEqual([item["quantity"] for item in historical["positions"]], ["1", "2"])
        self.assertEqual([item["quantity"] for item in after["positions"]], ["2", "1"])
        self.assertEqual(before["assets_usd"], "90.00")
        self.assertEqual(after["assets_usd"], "90.00")
        self.assertEqual(after["pas_publications"][0]["account_id"], A1["account_id"])
        self.assertEqual(
            after["pas_publications"][0]["as_observed_manifest_account_id"], A2["account_id"]
        )
        self.assertEqual(
            after["positions"][0]["evidence"]["content_sha256"],
            before["positions"][1]["evidence"]["content_sha256"],
        )

    def test_crosswalk_valid_date_and_receipt_cutoff_both_apply(self):
        self.export.crm(accounts=(A1, A2))
        self.export.snapshots([cash("10"), cash("20", account=A2)], accounts=(A1, A2))
        next_day = DAY + timedelta(days=1)
        self.export.snapshots(
            [cash("30", day=next_day), cash("40", account=A2, day=next_day)],
            accounts=(A1, A2),
            day=next_day,
        )
        corrections = []
        for account, new_number in ((A1, A2["account_number"]), (A2, A1["account_number"])):
            corrections.extend(
                [
                    row(
                        "account",
                        account["account_id"],
                        id=account["account_id"],
                        custodian="A",
                        account_number=account["account_number"],
                        is_managed=True,
                        valid_to=next_day.isoformat(),
                    ),
                    row(
                        "account",
                        account["account_id"],
                        next_day,
                        id=account["account_id"],
                        custodian="A",
                        account_number=new_number,
                        is_managed=True,
                    ),
                ]
            )
        self.export.feed("CRM", "crm", corrections, day=next_day, at="2026-10-02T14:00:00+00:00")
        historical = self.household()
        before = self.household(day=next_day, known=datetime(2026, 10, 2, 13, tzinfo=UTC))
        after = self.household(day=next_day, known=datetime(2026, 10, 2, 14, tzinfo=UTC))
        self.assertEqual(
            [item["amount_local"] for item in historical["cash_components"]], ["10", "20"]
        )
        self.assertEqual([item["amount_local"] for item in before["cash_components"]], ["30", "40"])
        self.assertEqual([item["amount_local"] for item in after["cash_components"]], ["40", "30"])

    def test_managed_account_learned_after_file_must_be_in_manifest(self):
        self.export.crm()
        self.export.snapshots([cash("100")])
        self.export.crm(accounts=(A1, A2), at="2026-10-02T14:00:00+00:00")
        facts = self.household()
        self.assertEqual(facts["status"], "INCOMPLETE")
        self.assertEqual(facts["missing_account_ids"], [A2["account_id"]])
        self.assertEqual(facts["fresh_subtotal_usd"], "100.00")

    def test_latest_file_omission_does_not_carry_older_account_publication_forward(self):
        self.export.crm(accounts=(A1, A2))
        self.export.snapshots([cash("100"), cash("200", account=A2)], accounts=(A1, A2))
        self.assertEqual(self.household()["assets_usd"], "300.00")
        self.export.feed(
            "PAS-A", "pas_snapshots", [cash("100")], accounts=[A1], at="2026-10-02T14:00:00+00:00"
        )
        facts = self.household()
        self.assertEqual(facts["fresh_subtotal_usd"], "100.00")
        self.assertEqual(facts["missing_account_ids"], [A2["account_id"]])

    def test_price_v1_v2_v1_reasserts_deduplicated_source_at_latest_receipt(self):
        self.usd(quantity="1", amount="0")
        self.export.feed("CUSTODY-A", "prices", [quote("26")], at="2026-10-02T14:00:00+00:00")
        self.export.feed(
            "CUSTODY-A",
            "prices",
            [quote(), quote("100", sec="WC-SEC-0002")],
            at="2026-10-03T14:00:00+00:00",
        )
        self.assertEqual(
            self.household(known=datetime(2026, 10, 2, 15, tzinfo=UTC))["assets_usd"], "26.00"
        )
        facts = self.household(known=datetime(2026, 10, 3, 14, tzinfo=UTC))
        self.assertEqual(facts["assets_usd"], "25.00")
        original_price = next(
            item
            for item in facts["evidence"]
            if item["feed"] == "prices" and item["source_record_id"] == "WC-SEC-0001"
        )
        self.assertEqual(original_price["recorded_at"], "2026-09-30T22:00:00+00:00")
        self.assertEqual(original_price["asserted_recorded_at"], "2026-10-03T14:00:00+00:00")

    def test_reasserted_old_price_alongside_competing_price_is_ambiguous(self):
        self.usd(quantity="1", amount="100")
        self.export.feed("CUSTODY-A", "prices", [quote("26")], at="2026-10-02T14:00:00+00:00")
        self.export.feed(
            "CUSTODY-A", "prices", [quote(), quote("26")], at="2026-10-03T14:00:00+00:00"
        )
        facts = self.household()
        self.assertEqual(facts["status"], "INCOMPLETE")
        self.assertEqual(facts["fresh_subtotal_usd"], "100.00")
        self.assertNotIn("assets_usd", facts)

    def test_multi_close_file_ignores_future_and_selects_latest_eligible_date(self):
        self.export.crm()
        self.export.feed("SECMASTER", "securities", [security()])
        self.export.snapshots([position("1")])
        self.export.feed(
            "CUSTODY-A",
            "prices",
            [
                quote("20", day=DAY - timedelta(days=1)),
                quote("25"),
                {**quote("999"), "price_date": "2026-10-01"},
            ],
        )
        self.assertEqual(self.household()["assets_usd"], "25.00")

    def test_cli_runs_independently_and_returns_json(self):
        self.usd()
        process = subprocess.run(
            [
                sys.executable,
                "-B",
                "-m",
                "reference_oracle",
                str(self.directory),
                "--household",
                HH,
                "--known-as-of",
                KNOWN.isoformat(),
                "--summary",
            ],
            cwd=ROOT,
            capture_output=True,
            text=True,
            check=False,
        )
        self.assertEqual(process.returncode, 0, process.stderr)
        self.assertEqual(json.loads(process.stdout)["households"][0]["assets_usd"], "3000.00")

    def _relationship_end_disagreement(self, kind):
        self.usd()
        boundary = date(2026, 10, 1)
        self.export.snapshots([position(day=boundary), cash(day=boundary)], day=boundary)
        self.export.feed("CUSTODY-A", "prices", [quote(day=boundary)], day=boundary)
        fields = {
            "membership": {"person_id": "WC-PER-0001", "household_id": HH},
            "reporting": {"account_id": A1["account_id"], "household_id": HH},
            "account": {
                "id": A1["account_id"],
                "custodian": "A",
                "account_number": A1["account_number"],
                "is_managed": True,
            },
        }[kind]
        opened = row(kind, "tied-interval", **fields)
        closed = {**opened, "valid_to": boundary.isoformat()}
        records = [closed, opened]
        if kind == "membership":
            records.append(
                row(
                    "person",
                    "person",
                    id="WC-PER-0001",
                    external_person_key="P1",
                    name="Synthetic person",
                )
            )
        self.export.feed("CRM", "crm", records, at="2026-09-30T23:00:00+00:00")
        both_active = self.household()
        self.assertEqual(both_active["status"], "COMPLETE")
        self.assertEqual(both_active["assets_usd"], "3000.00")
        before_fix = datetime(2026, 10, 2, 13, 59, 59, 999999, tzinfo=UTC)
        unresolved = self.household(day=boundary, known=before_fix)
        self.assertEqual(unresolved["status"], "INCOMPLETE")
        self.assertNotIn("assets_usd", unresolved)
        self.assertIn("AMBIGUOUS_ENTITY", {flag["code"] for flag in unresolved["blocking_flags"]})
        expected_fresh = "3000.00" if kind == "membership" else "0.00"
        self.assertEqual(unresolved["fresh_subtotal_usd"], expected_fresh)
        if kind == "account":
            state = State(load_received_history(self.directory), before_fix)
            self.assertIsNone(state.resolve_account(("A", A1["account_number"]), boundary))
        self.export.feed("CRM", "crm", [opened], at="2026-10-02T14:00:00+00:00")
        restored = self.household(day=boundary, known=datetime(2026, 10, 2, 14, tzinfo=UTC))
        self.assertEqual(restored["status"], "COMPLETE")
        self.assertEqual(restored["assets_usd"], "3000.00")
        self.assertEqual(self.household(day=boundary, known=before_fix), unresolved)

    def test_membership_interval_ends_preserve_uncertainty_and_historical_cutoff(self):
        self._relationship_end_disagreement("membership")

    def test_reporting_interval_ends_cannot_silently_include_an_account(self):
        self._relationship_end_disagreement("reporting")

    def test_crosswalk_interval_ends_cannot_silently_bind_received_balances(self):
        self._relationship_end_disagreement("account")

    def _price_end_disagreement(self):
        self.export.crm()
        self.export.feed("SECMASTER", "securities", [security()])
        self.export.snapshots([position("3"), cash("7")])
        boundary = date(2026, 10, 1)
        self.export.snapshots([position("3", day=boundary), cash("7", day=boundary)], day=boundary)
        older = quote("9", day=DAY - timedelta(days=1))
        opened = quote("11")
        closed = {**opened, "valid_to": boundary.isoformat()}
        self.export.feed("CUSTODY-A", "prices", [older, closed, opened])
        return boundary, opened

    def test_price_interval_ends_agree_while_active_but_not_at_the_end_boundary(self):
        boundary, _opened = self._price_end_disagreement()
        self.assertEqual(self.household()["assets_usd"], "40.00")
        unresolved = self.household(day=boundary)
        self.assertEqual(unresolved["status"], "INCOMPLETE")
        self.assertEqual(unresolved["fresh_subtotal_usd"], "7.00")
        self.assertEqual(unresolved["missing_components"], [f"{A1['account_id']}:WC-SEC-0001"])
        self.assertEqual(unresolved["stale_components"], [])
        self.assertEqual(
            {flag["code"] for flag in unresolved["blocking_flags"]}, {"AMBIGUOUS"}
        )
        result = self.evaluate(day=boundary)
        self.assertEqual(result["breaches"], [])
        self.assertEqual(len(result["indeterminate"]), 1)

    def test_price_interval_end_ambiguity_resolves_by_correction_or_newer_close(self):
        for correction_kind in ("same_close", "newer_close"):
            with self.subTest(correction_kind=correction_kind):
                self.export = Export(self.directory)
                boundary, opened = self._price_end_disagreement()
                cutoff = datetime(2026, 10, 2, 14, tzinfo=UTC)
                before = cutoff - timedelta(microseconds=1)
                historical = self.household(day=boundary, known=before)
                corrected = opened if correction_kind == "same_close" else quote("12", day=boundary)
                self.export.feed("CUSTODY-A", "prices", [corrected], at=cutoff.isoformat())
                self.assertEqual(self.household(day=boundary, known=before), historical)
                resolved = self.household(day=boundary, known=cutoff)
                self.assertEqual(resolved["missing_components"], [])
                if correction_kind == "same_close":
                    self.assertEqual(resolved["status"], "INCOMPLETE")  # US close is one day old.
                    self.assertEqual(
                        resolved["stale_components"][0]["last_known_value_usd"], "33.00"
                    )
                    self.assertEqual(
                        {flag["code"] for flag in resolved["blocking_flags"]}, {"PRICE_STALE"}
                    )
                else:
                    self.assertEqual(resolved["status"], "COMPLETE")
                    self.assertEqual(resolved["assets_usd"], "43.00")

    def test_late_older_close_interval_uncertainty_does_not_replace_newer_close(self):
        boundary, opened = self._price_end_disagreement()
        self.export.feed("CUSTODY-A", "prices", [quote("12", day=boundary)], day=boundary)
        self.export.feed(
            "CUSTODY-A",
            "prices",
            [opened, {**opened, "valid_to": boundary.isoformat()}],
            day=boundary,
            at="2026-10-02T14:00:00+00:00",
        )
        result = self.household(day=boundary)
        self.assertEqual(result["status"], "COMPLETE")
        self.assertEqual(result["assets_usd"], "43.00")
        self.assertEqual(result["positions"][0]["price_date"], boundary.isoformat())

    def test_issuer_fund_and_security_rank_scopes_are_independent(self):
        self.export.crm()
        self.export.feed(
            "SECMASTER",
            "securities",
            [
                security(),
                security("WC-SEC-0002", issuer="WC-ISS-0002"),
                security("WC-SEC-0017", kind="MMF"),
            ],
        )
        self.export.snapshots(
            [
                position("100"),
                position("100", sec="WC-SEC-0002"),
                position("300", sec="WC-SEC-0017"),
                cash("500"),
            ]
        )
        self.export.feed(
            "CUSTODY-A",
            "prices",
            [quote("1", sec=sec) for sec in ("WC-SEC-0001", "WC-SEC-0002", "WC-SEC-0017")],
        )
        facts = self.household()
        for name, scope, identity, percentage in (
            ("issuer_exposures", "household_issuers", "WC-ISS-0001", "concentration_pct"),
            ("fund_vehicle_exposures", "household_fund_vehicles", "WC-SEC-0017", "allocation_pct"),
            ("security_exposures", "household_securities", "WC-SEC-0017", "allocation_pct"),
        ):
            group = facts[name]
            self.assertEqual(group["rank_scope"], scope)
            first = group["items"][0]
            self.assertEqual(first.get("issuer_id", first.get("security_id")), identity)
            self.assertEqual(first["rank"], 1)
            self.assertIn(percentage, first)
            other = "allocation_pct" if percentage == "concentration_pct" else "concentration_pct"
            self.assertNotIn(other, first)
            self.assertFalse(group["truncated"])
            self.assertIsNone(group["next_cursor"])
            self.assertEqual(group["item_count"], group["total_item_count"])

    def test_nonpositive_total_omits_percentage_fields_even_when_zero_rows_exist(self):
        self.export.crm()
        self.export.feed(
            "SECMASTER", "securities", [security(), security("WC-SEC-0017", kind="MMF")]
        )
        self.export.snapshots([position("0"), position("0", sec="WC-SEC-0017")])
        self.export.feed("CUSTODY-A", "prices", [quote(), quote("1", sec="WC-SEC-0017")])
        result = self.evaluate()
        facts = result["households"][0]
        self.assertEqual(facts["assets_usd"], "0.00")
        for name in ("issuer_exposures", "fund_vehicle_exposures", "security_exposures"):
            self.assertTrue(facts[name]["items"])
            for item in facts[name]["items"]:
                self.assertNotIn("concentration_pct", item)
                self.assertNotIn("allocation_pct", item)
        self.assertEqual(result["breaches"], [])
        self.assertEqual(result["indeterminate"][0]["reason"], "NONPOSITIVE_ASSETS")

    def test_top20_display_cap_follows_full_valuation_and_threshold_comparison(self):
        self.export.crm()
        securities = [f"WC-SEC-{index:04}" for index in range(1, 26)]
        self.export.feed(
            "SECMASTER",
            "securities",
            [security(sec, issuer=f"WC-ISS-{index:04}") for index, sec in enumerate(securities, 1)],
        )
        self.export.snapshots([position("1", sec=sec) for sec in reversed(securities)])
        self.export.feed("CUSTODY-A", "prices", [quote("40", sec=sec) for sec in securities])
        result = self.evaluate(threshold="4.00")
        facts = result["households"][0]
        self.assertEqual(facts["assets_usd"], "1000.00")
        for name in ("issuer_exposures", "security_exposures"):
            self.assertEqual(facts[name]["item_count"], 20)
            self.assertEqual(facts[name]["total_item_count"], 25)
            self.assertTrue(facts[name]["truncated"])
            self.assertIsNone(facts[name]["next_cursor"])
            self.assertEqual([item["rank"] for item in facts[name]["items"]], list(range(1, 21)))
        self.assertEqual(result["breach_metadata"]["total_item_count"], 25)
        self.assertEqual(result["evaluated_household_count"], 1)
        self.assertEqual(result["breaching_household_count"], 1)
        self.assertEqual(result["breach_metadata"]["item_count"], 20)
        self.assertTrue(result["breach_metadata"]["truncated"])
        self.assertEqual([item["book_rank"] for item in result["breaches"]], list(range(1, 21)))
        self.assertEqual([item["issuer_rank"] for item in result["breaches"]], list(range(1, 21)))
        self.assertTrue(all(item["concentration_pct"] == "4.00" for item in result["breaches"]))
        self.assertTrue(all("rank" not in item for item in result["breaches"]))
        self.assertEqual(self.evaluate(threshold="4.01")["breaches"], [])

    def _book_crm(self, count):
        households, accounts, records = [], [], []
        for index in range(1, count + 1):
            household = f"WC-HH-{index:04}"
            account = {
                "account_id": f"WC-AC-{index:04}",
                "custodian": "A",
                "account_number": f"{index:08}",
            }
            households.append(household)
            accounts.append(account)
            records.extend(
                [
                    row("household", household, id=household, name=f"Synthetic household {index}"),
                    row(
                        "account",
                        account["account_id"],
                        id=account["account_id"],
                        custodian="A",
                        account_number=account["account_number"],
                        is_managed=True,
                    ),
                    row(
                        "reporting",
                        "report-" + account["account_id"],
                        account_id=account["account_id"],
                        household_id=household,
                    ),
                ]
            )
        self.export.feed("CRM", "crm", records)
        return households, accounts

    def test_all_indeterminate_households_are_retained_beyond_twenty(self):
        households, accounts = self._book_crm(32)
        self.export.feed("SECMASTER", "securities", [security()])
        active = [position("1", account=account) for account in accounts[-4:]]
        self.export.feed("PAS-A", "pas_snapshots", active, accounts=accounts[:14] + accounts[-4:])
        self.export.feed("CUSTODY-A", "custody_snapshots", active, accounts=accounts)
        self.export.feed("CUSTODY-A", "prices", [quote()])
        result = evaluate(
            load_received_history(self.directory),
            household_ids=households,
            as_of=DAY,
            known_as_of=KNOWN,
        )
        self.assertEqual(len(result["breaches"]), 4)
        self.assertEqual(result["evaluated_household_count"], 32)
        self.assertEqual(result["determinate_household_count"], 4)
        self.assertEqual(result["indeterminate_household_count"], 28)
        self.assertEqual(result["breaching_household_count"], 4)
        self.assertEqual([item["household_id"] for item in result["breaches"]], households[-4:])
        self.assertEqual(result["indeterminate_metadata"]["item_count"], 28)
        self.assertEqual(result["indeterminate_metadata"]["total_item_count"], 28)
        self.assertFalse(result["indeterminate_metadata"]["truncated"])
        self.assertIsNone(result["indeterminate_metadata"]["next_cursor"])
        self.assertEqual(
            [item["household_id"] for item in result["indeterminate"]], households[:-4]
        )
        reasons = [item["reason"] for item in result["indeterminate"]]
        self.assertEqual(reasons.count("NONPOSITIVE_ASSETS"), 14)
        self.assertEqual(reasons.count("INCOMPLETE_ASSETS"), 14)

    def test_breach_book_rank_is_distinct_from_household_issuer_rank(self):
        households, accounts = self._book_crm(3)
        self.export.feed(
            "SECMASTER", "securities", [security(), security("WC-SEC-0002", issuer="WC-ISS-0002")]
        )
        holdings = [
            position("5", account=accounts[0]),
            position("5", account=accounts[0], sec="WC-SEC-0002"),
            position("9", account=accounts[1]),
            cash("10", account=accounts[1]),
            position("9", account=accounts[2], sec="WC-SEC-0002"),
            cash("10", account=accounts[2]),
        ]
        self.export.snapshots(holdings, accounts=accounts)
        self.export.feed("CUSTODY-A", "prices", [quote("10"), quote("10", sec="WC-SEC-0002")])
        result = evaluate(
            load_received_history(self.directory),
            household_ids=list(reversed(households)),
            as_of=DAY,
            known_as_of=KNOWN,
        )
        self.assertEqual(
            [
                (item["household_id"], item["book_rank"], item["issuer_rank"])
                for item in result["breaches"]
            ],
            [
                (households[1], 1, 1),
                (households[2], 2, 1),
                (households[0], 3, 1),
                (households[0], 4, 2),
            ],
        )

    def test_book_rank_uses_unrounded_ratio_independent_of_callers_context(self):
        households, accounts = self._book_crm(2)
        self.export.feed("SECMASTER", "securities", [security()])
        self.export.snapshots(
            [
                position("1000.01", account=accounts[0]),
                cash("8999.99", account=accounts[0]),
                position("1000.02", account=accounts[1]),
                cash("8999.98", account=accounts[1]),
            ],
            accounts=accounts,
        )
        self.export.feed("CUSTODY-A", "prices", [quote("1")])
        with localcontext() as caller:
            caller.prec = 3
            result = evaluate(
                load_received_history(self.directory),
                household_ids=households,
                as_of=DAY,
                known_as_of=KNOWN,
            )
        self.assertEqual(
            [item["household_id"] for item in result["breaches"]], list(reversed(households))
        )
        self.assertEqual(
            [item["concentration_pct"] for item in result["breaches"]], ["10.00", "10.00"]
        )

    def test_threshold_accepts_two_decimals_and_rejects_extra_precision(self):
        self.usd()
        self.assertEqual(self.evaluate(threshold="10.01")["applied_threshold_pct"], "10.01")
        with self.assertRaises(ValueError):
            self.evaluate(threshold="10.001")

    def _pas_price_fixture(self, pas_price, *, custody_price: str | None = "25", custody_day=DAY):
        self.export.crm()
        self.export.feed("SECMASTER", "securities", [security()])
        observed = {**position(), "pas_price": pas_price}
        self.export.snapshots([observed, cash()], custody=[position(), cash()])
        if custody_price is not None:
            self.export.feed(
                "CUSTODY-A", "prices", [quote(custody_price, day=custody_day)], day=custody_day
            )

    def test_absent_null_or_equal_pas_price_does_not_invent_a_mismatch(self):
        for observation in (
            "absent",
            None,
            {"price": "25.00", "price_date": DAY.isoformat(), "currency": "USD"},
        ):
            with self.subTest(observation=observation):
                self.export = Export(self.directory)
                if observation == "absent":
                    self.usd()
                else:
                    self._pas_price_fixture(observation)
                facts = self.household()
                self.assertEqual(facts["status"], "COMPLETE")
                self.assertEqual(facts["assets_usd"], "3000.00")
                self.assertEqual(facts["quality_flags"], [])

    def test_pas_price_mismatch_is_info_and_cites_both_received_records(self):
        observation = {"price": "26", "price_date": DAY.isoformat(), "currency": "USD"}
        self._pas_price_fixture(observation)
        facts = self.household()
        self.assertEqual(facts["status"], "COMPLETE")
        self.assertEqual(facts["assets_usd"], "3000.00")
        self.assertEqual(facts["positions"][0]["price"], "25")
        self.assertEqual(facts["blocking_flags"], [])
        self.assertEqual(len(facts["quality_flags"]), 1)
        mismatch = facts["quality_flags"][0]
        self.assertEqual(
            (mismatch["code"], mismatch["severity"]), ("PRICE_SOURCE_MISMATCH", "info")
        )
        self.assertEqual(mismatch["subject"], f"{A1['account_id']}:WC-SEC-0001")
        detail = mismatch["detail"]
        self.assertEqual(detail["reasons"], ["PRICE"])
        self.assertEqual(detail["pas_price"], observation)
        self.assertEqual(detail["custody_price"]["price"], "25")
        self.assertEqual(detail["pas_evidence"], facts["positions"][0]["evidence"])
        self.assertEqual(detail["pas_evidence"]["source_system"], "PAS-A")
        self.assertEqual(detail["custody_evidence"]["source_system"], "CUSTODY-A")
        raw = load_received_history(self.directory)
        original = next(item for item in raw.receipts if item.feed == "pas_snapshots")
        self.assertEqual(original.rows[0]["pas_price"], observation)
        self.assertIsNone(original.rows[0]["source_asserted_at"])
        self.assertEqual(detail["pas_evidence"]["content_sha256"], checksum(original.content))

    def test_pas_price_comparison_is_unrounded(self):
        self._pas_price_fixture(
            {"price": "25.003", "price_date": DAY.isoformat(), "currency": "USD"},
            custody_price="25.004",
        )
        facts = self.household()
        self.assertEqual(facts["assets_usd"], "3000.40")
        self.assertEqual(facts["quality_flags"][0]["detail"]["reasons"], ["PRICE"])

    def test_pas_price_reasons_compare_price_only_when_date_and_currency_agree(self):
        for currency, close, expected in (
            ("EUR", DAY.isoformat(), ["CURRENCY"]),
            ("USD", "2026-09-29", ["CLOSE_DATE"]),
            ("GBP", "2026-09-29", ["CURRENCY", "CLOSE_DATE"]),
        ):
            with self.subTest(currency=currency, close=close):
                self.export = Export(self.directory)
                self._pas_price_fixture({"price": "999", "price_date": close, "currency": currency})
                facts = self.household()
                self.assertEqual(facts["assets_usd"], "3000.00")
                self.assertEqual(facts["quality_flags"][0]["detail"]["reasons"], expected)
                self.assertEqual(facts["blocking_flags"], [])

    def test_pas_price_cannot_replace_missing_or_ambiguous_custody(self):
        observation = {"price": "26", "price_date": DAY.isoformat(), "currency": "USD"}
        for state in ("missing", "ambiguous"):
            with self.subTest(state=state):
                self.export = Export(self.directory)
                self._pas_price_fixture(observation, custody_price=None)
                if state == "ambiguous":
                    self.export.feed("CUSTODY-A", "prices", [quote("25"), quote("27")])
                facts = self.household()
                self.assertEqual(facts["status"], "INCOMPLETE")
                self.assertNotIn("assets_usd", facts)
                self.assertEqual(facts["fresh_subtotal_usd"], "500.00")
                self.assertEqual(facts["positions"], [])
                self.assertEqual(
                    {item["code"] for item in facts["quality_flags"]},
                    {"AMBIGUOUS" if state == "ambiguous" else "SOURCE_UNAVAILABLE"},
                )

    def test_fresh_pas_price_does_not_repair_stale_custody(self):
        self._pas_price_fixture(
            {"price": "26", "price_date": DAY.isoformat(), "currency": "USD"},
            custody_day=DAY - timedelta(days=1),
        )
        facts = self.household()
        self.assertEqual(facts["status"], "INCOMPLETE")
        self.assertNotIn("assets_usd", facts)
        self.assertEqual(facts["fresh_subtotal_usd"], "500.00")
        self.assertEqual(facts["stale_components"][0]["last_known_value_usd"], "2500.00")
        self.assertEqual(
            {item["code"] for item in facts["quality_flags"]},
            {"PRICE_STALE", "PRICE_SOURCE_MISMATCH"},
        )
        mismatch = next(
            item for item in facts["quality_flags"] if item["code"] == "PRICE_SOURCE_MISMATCH"
        )
        self.assertEqual(mismatch["detail"]["reasons"], ["CLOSE_DATE"])

    def test_late_pas_price_correction_changes_warning_not_authoritative_amount(self):
        self._pas_price_fixture({"price": "26", "price_date": DAY.isoformat(), "currency": "USD"})
        before = datetime(2026, 10, 2, 13, 59, 59, 999999, tzinfo=UTC)
        original = self.household(known=before)
        corrected = {
            **position(),
            "pas_price": {"price": "25", "price_date": DAY.isoformat(), "currency": "USD"},
        }
        self.export.feed(
            "PAS-A",
            "pas_snapshots",
            [corrected, cash()],
            accounts=[A1],
            at="2026-10-02T14:00:00+00:00",
        )
        self.assertEqual(self.household(known=before), original)
        after = self.household(known=datetime(2026, 10, 2, 14, tzinfo=UTC))
        self.assertEqual(original["assets_usd"], after["assets_usd"])
        self.assertEqual(original["assets_usd"], "3000.00")
        self.assertEqual(after["quality_flags"], [])
        self.assertNotEqual(
            original["positions"][0]["evidence"]["content_sha256"],
            after["positions"][0]["evidence"]["content_sha256"],
        )
        self.assertEqual(original["issuer_exposures"], after["issuer_exposures"])

    def test_invalid_pas_observation_quarantines_its_whole_account_and_salvages_peer(self):
        valid = {"price": "25", "price_date": DAY.isoformat(), "currency": "USD"}
        bad_observations = [
            {"price": "25", "currency": "USD"},
            {**valid, "extra": "not allowed"},
            {**valid, "price": -1},
            {**valid, "price": "-1"},
            {**valid, "price": "9" * 29},
            {**valid, "price": "NaN"},
            {**valid, "price_date": "2026-9-30"},
            {**valid, "currency": "JPY"},
            [],
        ]
        for observation in bad_observations:
            with self.subTest(observation=observation):
                self.export = Export(self.directory)
                self.export.crm(accounts=(A1, A2))
                self.export.feed("SECMASTER", "securities", [security()])
                bad = {**position(), "pas_price": observation}
                good = [position(), cash(), cash("7", account=A2)]
                self.export.snapshots(
                    [bad, cash(), cash("7", account=A2)], accounts=(A1, A2), custody=good
                )
                self.export.feed("CUSTODY-A", "prices", [quote()])
                facts = self.household()
                self.assertEqual(facts["status"], "INCOMPLETE")
                self.assertEqual(facts["fresh_subtotal_usd"], "7.00")
                self.assertEqual(facts["missing_account_ids"], [A1["account_id"]])
                self.assertEqual(facts["positions"], [])
                self.assertNotIn("assets_usd", facts)

    def test_custody_position_rejects_nonnull_pas_observation_but_allows_null(self):
        for observation in (
            None,
            {"price": "25", "price_date": DAY.isoformat(), "currency": "USD"},
        ):
            with self.subTest(observation=observation):
                self.export = Export(self.directory)
                self.export.crm()
                self.export.feed("SECMASTER", "securities", [security()])
                self.export.snapshots(
                    [position(), cash()], custody=[{**position(), "pas_price": observation}, cash()]
                )
                self.export.feed("CUSTODY-A", "prices", [quote()])
                facts = self.household()
                self.assertEqual(facts["status"], "COMPLETE")
                self.assertEqual(facts["assets_usd"], "3000.00")
                expected = set() if observation is None else {"RECONCILIATION_UNAVAILABLE"}
                self.assertEqual({item["code"] for item in facts["quality_flags"]}, expected)

    def test_pas_observation_is_not_a_field_on_cash_records(self):
        self.export.crm()
        invalid = {
            **cash(),
            "pas_price": {"price": "1", "price_date": DAY.isoformat(), "currency": "USD"},
        }
        self.export.snapshots([invalid], custody=[cash()])
        facts = self.household()
        self.assertEqual(facts["status"], "INCOMPLETE")
        self.assertEqual(facts["fresh_subtotal_usd"], "0.00")
        self.assertEqual(facts["missing_account_ids"], [A1["account_id"]])

    def test_current_fx_file_and_valid_interval_cannot_refresh_yesterdays_fix_timestamp(self):
        self.export.crm()
        self.export.snapshots([cash("50"), cash("100", currency="EUR")])
        stale = {**fix("EUR", "1.10"), "rate_at": "2026-09-29T15:00:00+00:00"}
        self.export.feed("FX", "fx", [stale])
        cutoff = datetime(2026, 10, 2, 14, tzinfo=UTC)
        before = cutoff - timedelta(microseconds=1)
        facts = self.household(known=before)
        self.assertEqual(facts["status"], "INCOMPLETE")
        self.assertEqual(facts["fresh_subtotal_usd"], "50.00")
        self.assertNotIn("assets_usd", facts)
        self.assertEqual({item["code"] for item in facts["blocking_flags"]}, {"FX_UNAVAILABLE"})
        self.export.feed("FX", "fx", [fix("EUR", "1.10")], at=cutoff.isoformat())
        self.assertEqual(self.household(known=before), facts)
        self.assertEqual(self.household(known=cutoff)["assets_usd"], "160.00")

    def test_current_fix_timestamp_is_eligible_at_exact_known_boundary(self):
        self.export.crm()
        self.export.snapshots([cash("50"), cash("100", currency="EUR")])
        future_asserted_fix = {**fix("EUR", "1.10"), "rate_at": "2026-09-30T23:00:00+00:00"}
        # Even received bytes cannot make a future-dated fix eligible early.
        self.export.feed("FX", "fx", [future_asserted_fix], at="2026-09-30T22:00:00+00:00")
        cutoff = datetime(2026, 9, 30, 23, tzinfo=UTC)
        before = self.household(known=cutoff - timedelta(microseconds=1))
        self.assertEqual(before["fresh_subtotal_usd"], "50.00")
        self.assertNotIn("assets_usd", before)
        after = self.household(known=cutoff)
        self.assertEqual(after["assets_usd"], "160.00")
        self.assertEqual(after["cash_components"][0]["fx"]["rate_at"], cutoff.isoformat())

    def test_malformed_snapshot_account_keys_do_not_crash_or_publish_partial_account(self):
        for field, value in (("custodian", ["A"]), ("account_number", {"unexpected": "shape"})):
            with self.subTest(field=field):
                self.export = Export(self.directory)
                self.export.crm(accounts=(A1, A2))
                intended = [position(), cash(), cash("7", account=A2)]
                malformed = [{**position(), field: value}, cash(), cash("7", account=A2)]
                self.export.feed(
                    "PAS-A",
                    "pas_snapshots",
                    malformed,
                    intended=intended,
                    accounts=(A1, A2),
                    status="PARTIAL",
                )
                self.export.feed("CUSTODY-A", "custody_snapshots", intended, accounts=(A1, A2))
                facts = self.household()
                self.assertEqual(facts["status"], "INCOMPLETE")
                self.assertEqual(facts["fresh_subtotal_usd"], "7.00")
                self.assertEqual(facts["missing_account_ids"], [A1["account_id"]])
                self.assertEqual(facts["positions"], [])


if __name__ == "__main__":
    unittest.main()
