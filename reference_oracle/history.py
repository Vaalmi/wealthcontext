"""Small event-replay model, separate from production storage and ingestion."""

from dataclasses import dataclass, replace
from datetime import date, datetime
from typing import Any

from .wire import (
    SNAPSHOTS,
    EvidenceError,
    History,
    Receipt,
    account_digest,
    business_date,
    canonical,
    external_key,
    sha256,
    timestamp_assertion,
    valid_row,
)


def semantic(row: dict) -> dict:
    value = {
        key: value
        for key, value in row.items()
        if key
        not in {
            "source_record_id",
            "source_asserted_at",
            "valid_from",
            "valid_to",
            "note",
        }
    }
    # The received schema declares an absent ISIN as unknown, exactly like
    # explicit null. Normalize assertion meaning without rewriting raw evidence.
    if row.get("kind") == "security":
        value.setdefault("isin", None)
    if row.get("kind") == "price":
        value["price_date"] = business_date(value["price_date"]).isoformat()
    if row.get("kind") == "fx":
        # Validation canonicalizes spelling and microsecond precision, but the
        # reported offset remains assertion evidence. Keep raw publication bytes.
        value["rate_at"] = timestamp_assertion(value["rate_at"])
    return value


def _raw_identity(receipt: Receipt, row: dict) -> tuple[str, str, date, str]:
    """Publication identity includes every raw field, even nonfinancial ones."""
    return receipt.source, row["source_record_id"], receipt.day, sha256(canonical(row))


@dataclass(frozen=True)
class Observation:
    row: dict
    receipt: Receipt
    asserted_at: datetime

    @property
    def at(self) -> datetime:
        return self.asserted_at

    @property
    def start(self) -> date:
        return business_date(self.row["valid_from"])

    @property
    def end(self) -> date:
        return business_date(self.row["valid_to"])

    def evidence(self) -> dict:
        return {
            **self.receipt.evidence(self.row),
            "asserted_recorded_at": self.asserted_at.isoformat(),
        }


@dataclass(frozen=True)
class Publication:
    external: tuple[str, str]
    upstream_account_id: str
    receipt: Receipt
    available: bool
    reason: str | None
    records: tuple[Observation, ...] = ()


@dataclass
class View:
    values: dict[Any, Observation]
    ambiguous: dict[Any, list[Observation]]


class State:
    def __init__(self, history: History, known: datetime):
        self.records: dict[str, dict[Any, list[Observation]]] = {}
        self.snapshot_receipts: dict[tuple[str, str, date], list[Receipt]] = {}
        self.publications: dict[tuple[int, tuple[str, str]], Publication] = {}
        self._record_keys: dict[tuple, Observation] = {}
        seen_runs, seen_deliveries = set(), {}
        for receipt in sorted(history.receipts, key=lambda item: (item.recorded_at, item.ordinal)):
            if receipt.recorded_at > known:
                continue
            delivery_key = receipt.source, receipt.delivery_id
            evidence = receipt.content_hash, canonical(receipt.manifest), receipt.status
            if delivery_key in seen_deliveries:
                if seen_deliveries[delivery_key] != evidence:
                    raise EvidenceError("delivery identity reused for changed evidence")
                continue
            seen_deliveries[delivery_key] = evidence
            run_key = receipt.source, receipt.content_hash or receipt.delivery_id
            if run_key in seen_runs:
                continue
            seen_runs.add(run_key)
            if receipt.feed in SNAPSHOTS:
                self._snapshot(receipt)
            elif receipt.file_complete and receipt.status != "FAILED":
                for row in sorted(
                    receipt.rows,
                    key=lambda item: (
                        0 if isinstance(item, dict) and item.get("kind") == "account" else 1
                    ),
                ):
                    if not valid_row(row, receipt.feed, receipt.source):
                        continue
                    # Ignore unrelated models' financial explanations, notes, grants,
                    # transactions and lots: PAS received snapshots own valuation.
                    if row["kind"] not in {
                        "household",
                        "person",
                        "membership",
                        "account",
                        "reporting",
                        "security",
                        "ownership",
                        "price",
                        "fx",
                    }:
                        continue
                    self._accept(receipt, row)

    def _accept(self, receipt: Receipt, row: dict) -> Observation:
        key = _raw_identity(receipt, row)
        if key not in self._record_keys:
            self._record_keys[key] = Observation(row, receipt, receipt.recorded_at)
        original = self._record_keys[key]
        kind = row["kind"]
        if kind == "ownership" and receipt.source == "CRM":
            kind = "crm_ownership"
        if kind in {"position", "cash", "pas_grouping"}:
            return original
        # A raw record has one immutable source identity. Each newly received
        # non-snapshot file separately asserts it at that file's receipt instant.
        # This ledger preserves V1 -> V2 -> V1 and same-file V1/V2 ambiguity.
        observation = Observation(row, original.receipt, receipt.recorded_at)
        entity = {
            "membership": row.get("person_id"),
            "reporting": row.get("account_id"),
            "ownership": external_key(row),
            "crm_ownership": external_key(row),
            "price": ((row["security_id"], business_date(row["price_date"]).isoformat())
                      if kind == "price" else None),
            "fx": row.get("currency"),
        }.get(kind, row.get("id"))
        self.records.setdefault(kind, {}).setdefault(entity, []).append(observation)
        return observation

    def view(self, kind: str, day: date) -> View:
        view = self._raw_view(kind, day)
        if kind not in {"ownership", "crm_ownership"}:
            return view
        resolved, ambiguous = {}, {}
        # External evidence survives missing or subsequently corrected CRM. Only
        # this projection uses the mapping effective at the query's two cutoffs.
        for key in view.values.keys() | view.ambiguous.keys():
            account = self.resolve_account(key, day)
            if account is None:
                continue
            if key in view.values:
                resolved[account] = view.values[key]
            else:
                ambiguous[account] = view.ambiguous[key]
        return View(resolved, ambiguous)

    def _raw_view(self, kind: str, day: date) -> View:
        resolved, ambiguous = {}, {}

        def assertion_state(item: Observation) -> bytes | None:
            if day >= item.end:
                return None
            meaning = semantic(item.row)
            if kind == "fx":
                # Daily FX eligibility belongs to the assertion state, not an
                # incidental representative selected after equal-time collapse.
                return canonical([meaning, item.receipt.day.isoformat()])
            return canonical(meaning)

        for entity, observations in self.records.get(kind, {}).items():
            # End-date corrections supersede their earlier open-ended interval
            # before validity filtering. Otherwise ended memberships resurrect.
            starts: dict[date, list[Observation]] = {}
            for item in observations:
                if item.start > day:
                    continue
                current = starts.setdefault(item.start, [])
                if not current or item.at > current[0].at:
                    starts[item.start] = [item]
                elif item.at == current[0].at:
                    current.append(item)
            # Interpret each tied interval as a set of possible states at the
            # requested date. None means absent; a payload means present. Equal
            # values can agree today despite differing future end dates, whereas
            # {None, value} is unresolved applicability and must stay ambiguous.
            alternatives: list[tuple[list[Observation], set[bytes | None]]] = []
            for group in starts.values():
                possibilities = {assertion_state(item) for item in group}
                if possibilities != {None}:
                    alternatives.append((group, possibilities))
            if not alternatives:
                continue
            latest = max(group[0].at for group, _possibilities in alternatives)
            selected = []
            possible_states: set[bytes | None] = set()
            for group, possibilities in alternatives:
                if group[0].at == latest:
                    selected.extend(group)
                    possible_states.update(possibilities)
            if len(possible_states) > 1:
                ambiguous[entity] = selected
            else:
                resolved[entity] = selected[0]
        return View(resolved, ambiguous)

    def resolve_account(self, external: tuple, day: date) -> str | None:
        view = self.view("account", day)
        candidates = [
            account for account, item in view.values.items() if external_key(item.row) == external
        ]
        conflict = any(
            external_key(item.row) == external
            for group in view.ambiguous.values()
            for item in group
        )
        return candidates[0] if len(candidates) == 1 and not conflict else None

    def _snapshot(self, receipt: Receipt) -> None:
        self.snapshot_receipts.setdefault((receipt.feed, receipt.source, receipt.day), []).append(
            receipt
        )
        for expected in receipt.manifest["accounts"]:
            group = [
                row
                for row in receipt.rows
                if isinstance(row, dict) and external_key(row) == external_key(expected)
            ]
            reason = None
            if (
                not receipt.header_valid
                or receipt.status == "FAILED"
                or len(group) != expected["record_count"]
                or account_digest(group) != expected["digest"]
            ):
                reason = "ACCOUNT_MANIFEST_MISMATCH"
            elif any(not valid_row(row, receipt.feed, receipt.source) for row in group):
                reason = "INVALID_ACCOUNT_ROW"
            elif any(
                not business_date(row["valid_from"]) <= receipt.day < business_date(row["valid_to"])
                for row in group
            ):
                reason = "SNAPSHOT_VALIDITY_MISMATCH"
            else:
                keys = [
                    (
                        row["kind"],
                        row.get("security_id")
                        if row["kind"] == "position"
                        else row.get("currency")
                        if row["kind"] == "cash"
                        else "grouping",
                    )
                    for row in group
                ]
                if len(keys) != len(set(keys)):
                    reason = "DUPLICATE_ACCOUNT_COMPONENT"
            records = () if reason else tuple(self._accept(receipt, row) for row in group)
            key = external_key(expected)
            self.publications[receipt.ordinal, key] = Publication(
                key,
                expected["account_id"],
                receipt,
                reason is None,
                reason,
                records,
            )

    def snapshot(self, feed: str, account: str, day: date) -> tuple[Publication | None, str | None]:
        current_account = self.view("account", day).values.get(account)
        if current_account is None:
            return None, "ACCOUNT_CROSSWALK_UNAVAILABLE"
        key = external_key(current_account.row)
        if self.resolve_account(key, day) != account:
            return None, "ACCOUNT_CROSSWALK_AMBIGUOUS"
        source = ("PAS-" if feed == "pas_snapshots" else "CUSTODY-") + key[0]
        candidates = self.snapshot_receipts.get((feed, source, day), [])
        if not candidates:
            return None, "NO_CURRENT_PUBLICATION"
        latest = max(item.recorded_at for item in candidates)
        # A newer source file omitting this external account supersedes the old
        # publication, even if CRM learned the account after either file arrived.
        current = [
            self.publications.get((item.ordinal, key))
            for item in candidates
            if item.recorded_at == latest
        ]
        signatures = {
            (
                item.available,
                tuple(sorted(_raw_identity(row.receipt, row.row) for row in item.records)),
            )
            if item
            else (None, ())
            for item in current
        }
        if len(signatures) > 1:
            return None, "AMBIGUOUS_PUBLICATION"
        item = current[0]
        if item is not None and not item.available and len({
            publication.reason for publication in current if publication is not None
        }) > 1:
            item = replace(item, reason="MULTIPLE_UNAVAILABLE_PUBLICATIONS")
        return (item, item.reason) if item else (None, "EXPECTED_ACCOUNT_MISSING")

    def price(self, security: str, day: date) -> tuple[Observation | None, str | None]:
        view = self.view("price", day)
        keys = [
            key
            for key in view.values.keys() | view.ambiguous.keys()
            if key[0] == security and business_date(key[1]) <= day
        ]
        if not keys:
            return None, "MISSING_PRICE"
        selected = max(keys, key=lambda key: business_date(key[1]))
        return view.values.get(selected), "AMBIGUOUS_PRICE" if selected in view.ambiguous else None

    def fx(self, currency: str, day: date) -> tuple[Observation | None, str | None]:
        view = self.view("fx", day)
        if currency in view.ambiguous:
            return None, "AMBIGUOUS_FX"
        item = view.values.get(currency)
        if item is None or item.receipt.day != day:
            return None, "MISSING_CURRENT_FIX"
        return item, None
