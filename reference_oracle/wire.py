"""Version-1 export reader implemented directly from the wire contract."""

import hashlib
import json
import re
from dataclasses import dataclass
from datetime import UTC, date, datetime, timedelta
from decimal import Decimal
from math import floor, isfinite
from pathlib import Path
from typing import Any


class EvidenceError(ValueError):
    """Export evidence is structurally invalid or changed after export."""


SOURCES = {
    "crm": {"CRM"},
    "securities": {"SECMASTER"},
    "fx": {"FX"},
    "custody_accounts": {"CUSTODY-A", "CUSTODY-B"},
    "pas_snapshots": {"PAS-A", "PAS-B"},
    "custody_snapshots": {"CUSTODY-A", "CUSTODY-B"},
    "prices": {"CUSTODY-A", "CUSTODY-B"},
    "transactions": {"PAS-A", "PAS-B"},
    "lots": {"PAS-A", "PAS-B"},
}
SNAPSHOTS = {"pas_snapshots", "custody_snapshots"}
CURRENCIES = {"USD", "EUR", "CAD", "GBP"}
KINDS = {
    "crm": {
        "household",
        "person",
        "membership",
        "account",
        "reporting",
        "ownership",
        "advisor",
        "team",
        "advisor_member",
        "advisor_covers",
        "team_covers",
    },
    "securities": {"security"},
    "custody_accounts": {"ownership"},
    "pas_snapshots": {"position", "cash", "pas_grouping"},
    "custody_snapshots": {"position", "cash"},
    "prices": {"price"},
    "fx": {"fx"},
    "transactions": {"transaction"},
    "lots": {"lot"},
}


def canonical(value: Any) -> bytes:
    return json.dumps(
        value, sort_keys=True, separators=(",", ":"), ensure_ascii=False, allow_nan=False
    ).encode("utf-8")


def sha256(value: bytes) -> str:
    return hashlib.sha256(value).hexdigest()


def account_digest(rows: list[dict]) -> str:
    return sha256(b"[" + b",".join(sorted(canonical(row) for row in rows)) + b"]")


def _object(pairs: list[tuple[str, Any]]) -> dict:
    result = {}
    for key, value in pairs:
        if key in result:
            raise EvidenceError("duplicate JSON key")
        result[key] = value
    return result


def _not_finite(value: str) -> None:
    raise EvidenceError(f"non-finite JSON number: {value}")


def json_value(raw: bytes) -> Any:
    def finite_real(token: str) -> float:
        value = float(token)
        if not isfinite(value):
            raise EvidenceError("non-finite JSON number")
        return value

    try:
        # Numeric JSON timestamps are permitted. Financial fields independently
        # require canonical decimal strings in valid_row()/number().
        return json.loads(
            raw.decode("utf-8"), object_pairs_hook=_object,
            parse_float=finite_real, parse_constant=_not_finite,
        )
    except (UnicodeError, ValueError) as exc:
        raise EvidenceError("invalid JSON evidence") from exc


def business_date(value: Any) -> date:
    """Read a calendar date, including accepted midnight datetime spellings."""
    try:
        if isinstance(value, str) and re.fullmatch(r"[0-9]{4}-[0-9]{2}-[0-9]{2}", value):
            return date.fromisoformat(value)
        parsed = _received_datetime(value, require_timezone=False)
        if (parsed.hour, parsed.minute, parsed.second, parsed.microsecond) != (0, 0, 0, 0):
            raise EvidenceError("business date timestamp must be exactly midnight")
        # Date validation uses the reported calendar date, without UTC conversion.
        return parsed.date()
    except (ValueError, TypeError, OverflowError) as exc:
        raise EvidenceError("invalid received business date") from exc


def _received_datetime(
    value: str | int | float | datetime, *, require_timezone: bool = True,
) -> datetime:
    """Read the received timestamp language without importing the production parser.

    Numeric timestamps use seconds through +/-20 billion and milliseconds outside
    that range. Decimal strings use binary64 timestamp precision, rounded to the
    nearest microsecond (ties toward the future); financial values use Decimal.
    ISO spellings retain their reported offset and truncate sub-microsecond digits.
    """
    if isinstance(value, datetime):
        parsed = value
    elif (isinstance(value, int | float) and not isinstance(value, bool)) or (
        isinstance(value, str) and re.fullmatch(
            r"[+-]?(?:[0-9]+|(?:[0-9]+\.[0-9]*|\.[0-9]+)(?:[eE][+-]?[0-9]+)?)", value,
        )
    ):
        if isinstance(value, str):
            try:
                numeric = float(value) if "." in value else int(value)
            except (ValueError, OverflowError) as exc:
                raise EvidenceError("invalid received timestamp") from exc
        else:
            assert isinstance(value, int | float)
            numeric = value
        if isinstance(numeric, float) and not isfinite(numeric):
            raise EvidenceError("timestamp must be finite")
        unit = 1000 if abs(numeric) > 20_000_000_000 else 1
        # Split before scaling to keep whole milliseconds exact. Dividing a large
        # floating timestamp first can move its rounded microsecond across a cutoff.
        whole = floor(numeric)
        seconds, remainder = divmod(whole, unit)
        micros_per_unit = 1_000_000 // unit
        micros = remainder * micros_per_unit + floor(
            (numeric - whole) * micros_per_unit + 0.5
        )
        try:
            parsed = datetime(1970, 1, 1, tzinfo=UTC) + timedelta(
                seconds=seconds, microseconds=micros
            )
        except (ValueError, OverflowError) as exc:
            raise EvidenceError("timestamp is outside the supported calendar") from exc
    elif isinstance(value, str) and re.fullmatch(
        r"[0-9]{4}-[0-9]{2}-[0-9]{2}[Tt _][0-9]{2}:[0-9]{2}"
        r"(?::[0-9]{2}(?:[.,][0-9]+)?)?"
        r"(?:[Zz]|[+−-](?:[01][0-9]|2[0-3]):?[0-5][0-9])?", value,
    ):
        spelling = value.replace("−", "-")
        spelling = spelling[:-1] + "Z" if spelling.endswith("z") else spelling
        try:
            parsed = datetime.fromisoformat(spelling)
        except ValueError as exc:
            raise EvidenceError("invalid received timestamp") from exc
    else:
        raise EvidenceError("invalid received timestamp")
    if require_timezone and (parsed.tzinfo is None or parsed.utcoffset() is None):
        raise EvidenceError("an aware timestamp is required")
    return parsed


def aware_datetime(value: str | int | float | datetime) -> datetime:
    return _received_datetime(value)


def instant(value: str | int | float | datetime) -> datetime:
    parsed = aware_datetime(value)
    try:
        return parsed.astimezone(UTC)
    except (ValueError, OverflowError) as exc:
        raise EvidenceError("received timestamp is outside the UTC range") from exc


def timestamp_assertion(value: str | int | float | datetime) -> str:
    """Canonical spelling of timestamp evidence, without changing its offset."""
    encoded = aware_datetime(value).isoformat()
    return encoded[:-6] + "Z" if encoded.endswith("+00:00") else encoded


def number(value: Any, *, nonnegative: bool = True) -> Decimal:
    grammar = r"(?:0|[1-9][0-9]*)(?:\.[0-9]+)?"
    if not nonnegative:
        grammar = "-?" + grammar
    if not isinstance(value, str) or not re.fullmatch(grammar, value):
        raise EvidenceError("financial values must be plain decimal strings")
    if sum(character.isdigit() for character in value) > 28:
        raise EvidenceError("financial value exceeds 28 digits")
    parsed = Decimal(value)
    if nonnegative and parsed < 0:
        raise EvidenceError("negative financial component")
    return parsed


def external_key(row: dict) -> tuple[Any, Any]:
    return row.get("custodian"), row.get("account_number")


def valid_row(row: Any, feed: str, source: str) -> bool:
    """Validate finance-relevant wire fields without application model imports.

    This reader deliberately does not duplicate unrelated identifier/title or
    transaction/lot validators; those are outside its financial observation scope.
    """
    try:
        if not isinstance(row, dict) or row.get("kind") not in KINDS[feed]:
            return False
        if not isinstance(row.get("source_record_id"), str) or not row["source_record_id"]:
            return False
        if business_date(row["valid_from"]) >= business_date(row["valid_to"]):
            return False
        if row.get("source_asserted_at") is not None:
            instant(row["source_asserted_at"])
        kind = row["kind"]
        if "pas_price" in row:
            if kind != "position":
                return False
            observation = row["pas_price"]
            if observation is not None:
                if (
                    feed != "pas_snapshots"
                    or not isinstance(observation, dict)
                    or set(observation) != {"price", "price_date", "currency"}
                ):
                    return False
                number(observation["price"])
                business_date(observation["price_date"])
                if observation["currency"] not in CURRENCIES:
                    return False
        if "custodian" in row:
            if row["custodian"] not in {"A", "B"} or not isinstance(row.get("account_number"), str):
                return False
            if source != "CRM" and row["custodian"] != source.rsplit("-", 1)[-1]:
                return False
        required = {
            "household": ("id", "name"),
            "person": ("id", "name", "external_person_key"),
            "membership": ("person_id", "household_id"),
            "account": ("id", "custodian", "account_number"),
            "reporting": ("account_id", "household_id"),
            "security": ("id", "issuer_id", "currency", "listing_country"),
            "position": ("custodian", "account_number", "security_id"),
            "cash": ("custodian", "account_number", "currency"),
            "pas_grouping": ("custodian", "account_number", "household_id"),
            "price": ("security_id", "currency"),
            "fx": ("currency",),
            "ownership": ("custodian", "account_number", "status"),
        }.get(kind, ())
        if any(not isinstance(row.get(key), str) or not row[key] for key in required):
            return False
        if kind == "account" and type(row.get("is_managed")) is not bool:
            return False
        if kind == "ownership" and row["status"] not in {"OPEN", "CLOSED"}:
            return False
        if kind == "security" and row.get("type") not in {
            "COMMON_STOCK",
            "ETF",
            "MUTUAL_FUND",
            "MMF",
        }:
            return False
        if kind == "security" and not re.fullmatch(r"[A-Z]{2}", row["listing_country"]):
            return False
        if kind in {"security", "cash", "price", "fx"} and row["currency"] not in CURRENCIES:
            return False
        if kind == "position":
            number(row["quantity"])
        if kind == "cash":
            number(row["amount"])
        if kind == "price":
            number(row["price"])
            business_date(row["price_date"])
        if kind == "fx":
            rate = number(row["usd_rate"])
            if rate <= 0 or (row["currency"] == "USD" and rate != 1):
                return False
            instant(row["rate_at"])
        return True
    except (KeyError, TypeError, ValueError):
        return False


@dataclass(frozen=True)
class Receipt:
    ordinal: int
    manifest: dict
    content: bytes | None
    content_hash: str | None
    status: str
    delivery_id: str
    recorded_at: datetime
    rows: tuple[Any, ...]
    header_valid: bool
    content_name: str | None

    @property
    def day(self) -> date:
        return business_date(self.manifest["business_date"])

    @property
    def source(self) -> str:
        return self.manifest["source_system"]

    @property
    def feed(self) -> str:
        return self.manifest["feed"]

    @property
    def file_complete(self) -> bool:
        return (
            self.header_valid
            and self.content_hash == self.manifest["checksum"]
            and len(self.rows) == self.manifest["record_count"]
        )

    def evidence(self, row: dict | None = None) -> dict:
        return {
            "source_system": self.source,
            "feed": self.feed,
            "business_date": self.day.isoformat(),
            "recorded_at": self.recorded_at.isoformat(),
            "content_sha256": self.content_hash,
            "manifest_sha256": sha256(canonical(self.manifest)),
            "source_record_id": row.get("source_record_id") if row else None,
        }


@dataclass(frozen=True)
class History:
    receipts: tuple[Receipt, ...]


def _checked_file(directory: Path, name: Any, expected_hash: Any) -> bytes:
    if not isinstance(name, str) or Path(name).name != name or name in {".", ".."}:
        raise EvidenceError("export entries must use plain filenames")
    path = directory / name
    if not path.resolve().is_relative_to(directory.resolve()):
        raise EvidenceError("export entry escapes its directory")
    raw = path.read_bytes()
    if sha256(raw) != expected_hash:
        raise EvidenceError("exported evidence hash mismatch")
    return raw


def _manifest(raw: bytes) -> dict:
    manifest = json_value(raw)
    if not isinstance(manifest, dict):
        raise EvidenceError("manifest must be an object")
    feed, source = manifest.get("feed"), manifest.get("source_system")
    if (not isinstance(feed, str) or not isinstance(source, str)
            or feed not in SOURCES or source not in SOURCES[feed]):
        raise EvidenceError("invalid source/feed pairing")
    business_date(manifest.get("business_date"))
    if type(manifest.get("record_count")) is not int or manifest["record_count"] < 0:
        raise EvidenceError("invalid manifest record count")
    if not isinstance(manifest.get("checksum"), str) or not re.fullmatch(
        "[a-f0-9]{64}", manifest["checksum"]
    ):
        raise EvidenceError("invalid manifest checksum")
    entries = manifest.get("accounts")
    ids = manifest.get("expected_account_ids")
    if (
        not isinstance(entries, list)
        or not isinstance(ids, list)
        or any(not isinstance(value, str) for value in ids)
    ):
        raise EvidenceError("invalid account expectations")
    seen_ids, seen_keys = set(), set()
    for entry in entries:
        if not isinstance(entry, dict):
            raise EvidenceError("invalid account expectation")
        account, key = entry.get("account_id"), external_key(entry)
        if (
            not isinstance(account, str)
            or not isinstance(key[1], str)
            or key[0] != source.rsplit("-", 1)[-1]
            or account in seen_ids
            or key in seen_keys
            or type(entry.get("record_count")) is not int
            or entry["record_count"] < 0
            or not isinstance(entry.get("digest"), str)
            or not re.fullmatch("[a-f0-9]{64}", entry["digest"])
        ):
            raise EvidenceError("invalid or duplicate account expectation")
        seen_ids.add(account)
        seen_keys.add(key)
    if seen_ids != set(ids) or len(ids) != len(set(ids)) or (feed not in SNAPSHOTS and ids):
        raise EvidenceError("manifest account sets disagree")
    return manifest


def load_received_history(directory: str | Path) -> History:
    directory = Path(directory)
    index = json_value((directory / "index.json").read_bytes())
    if (
        not isinstance(index, dict)
        or index.get("version") != 1
        or not isinstance(index.get("events"), list)
    ):
        raise EvidenceError("unsupported export index")
    receipts = []
    for ordinal, entry in enumerate(index["events"]):
        required = {"manifest", "manifest_sha256", "upstream_status", "delivery_id",
                    "content", "content_sha256", "recorded_at"}
        if not isinstance(entry, dict) or not required <= entry.keys():
            raise EvidenceError("invalid export event entry")
        manifest = _manifest(_checked_file(directory, entry["manifest"], entry["manifest_sha256"]))
        status = entry["upstream_status"]
        if not isinstance(status, str) or status not in {"SUCCESS", "PARTIAL", "TIMEOUT", "FAILED"}:
            raise EvidenceError("invalid upstream status")
        if not isinstance(entry["delivery_id"], str) or not entry["delivery_id"]:
            raise EvidenceError("missing delivery identity")
        content = None
        if entry["content"] is not None:
            content = _checked_file(directory, entry["content"], entry["content_sha256"])
        elif status not in {"FAILED", "TIMEOUT"} or entry["content_sha256"] is not None:
            raise EvidenceError("missing content has inconsistent receipt metadata")
        rows: list[Any] = []
        header_valid = False
        if content is not None:
            header, separator, body = content.partition(b"\n")
            try:
                parsed_header = json_value(header)
                header_valid = bool(separator) and parsed_header == {
                    "format": "wealthcontext-feed-v1",
                    "source_system": manifest["source_system"],
                    "feed": manifest["feed"],
                    "business_date": business_date(manifest["business_date"]).isoformat(),
                }
            except EvidenceError:
                pass
            if header_valid:
                lines = body.split(b"\n")
                if lines[-1] == b"":
                    lines.pop()
                for line in lines:
                    try:
                        rows.append(json_value(line))
                    except EvidenceError:
                        rows.append(None)  # Raw bytes remain in Receipt.content.
        receipts.append(
            Receipt(
                ordinal,
                manifest,
                content,
                entry["content_sha256"],
                status,
                entry["delivery_id"],
                instant(entry["recorded_at"]),
                tuple(rows),
                header_valid,
                entry["content"],
            )
        )
    return History(tuple(receipts))
