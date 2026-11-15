"""Project stored source evidence using already-authorized recorded-result scope."""

import json
import re
from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from typing import Annotated, Any, Literal, Self, cast

from pydantic import StringConstraints, model_validator

from wealthcontext.contracts import Contract, Identifier
from wealthcontext.projections import Projection, project_no_pii
from wealthcontext.storage import canonical, digest

REDACTED = "[REDACTED]"
# ':' and '-' can be identifier characters as well as apparent delimiters.
# Opaque metadata has no schema that proves a split, so inspect the maximal token.
_DOMAIN_ID = re.compile(r"WC-(?:HH|AC)-[A-Za-z0-9_.:@-]+")
_METADATA = (
    "id", "source_system", "source_record_id", "business_date", "recorded_at", "imported_at",
    "payload_hash", "feed_run_id", "source_asserted_at", "record_type",
)


class OriginAccount(Contract):
    household_id: Identifier
    account_id: Identifier
    custodian: Literal["A", "B"] | None = None
    account_number: Annotated[str, StringConstraints(min_length=1, max_length=128)] | None = None

    @model_validator(mode="after")
    def paired_external_key(self) -> Self:
        if (self.custodian is None) != (self.account_number is None):
            raise ValueError("origin account external key must be complete")
        return self


class OriginAccountScope(Contract):
    schema_version: Literal[1] = 1
    result_id: Identifier
    household_ids: list[Identifier]
    accounts: list[OriginAccount]

    @model_validator(mode="after")
    def consistent_ownership(self) -> Self:
        if len(self.household_ids) != len(set(self.household_ids)):
            raise ValueError("duplicate origin households")
        ids = [account.account_id for account in self.accounts]
        if len(ids) != len(set(ids)):
            raise ValueError("duplicate origin accounts")
        if any(account.household_id not in self.household_ids for account in self.accounts):
            raise ValueError("account outside origin households")
        return self


@dataclass(frozen=True)
class SourceScope:
    household_ids: frozenset[str]
    account_ids: frozenset[str]
    external_accounts: frozenset[tuple[str, str, str]]

    @classmethod
    def from_account_scope(
        cls, payload: Mapping[str, Any], household_ids: Sequence[str], result_id: str,
    ) -> "SourceScope":
        """Validated immutable ownership, including evidence absent from visible lists."""
        origin = OriginAccountScope.model_validate(payload)
        if origin.result_id != result_id or set(origin.household_ids) != set(household_ids):
            raise ValueError("origin account metadata does not match result scope")
        return cls(
            frozenset(origin.household_ids),
            frozenset(account.account_id for account in origin.accounts),
            frozenset((account.account_id, account.custodian, account.account_number)
                      for account in origin.accounts
                      if account.custodian is not None and account.account_number is not None),
        )

    @classmethod
    def from_result(cls, result: Mapping[str, Any], household_ids: Sequence[str]) -> "SourceScope":
        """Use the immutable result's accounts; never re-read current business facts."""
        if not isinstance(result, Mapping):
            raise ValueError("recorded result is not an envelope")
        data = result.get("data")
        if not isinstance(data, Mapping):
            raise ValueError("recorded result has no scoped data")
        ids: set[str] = set()
        external: set[tuple[str, str, str]] = set()
        for field in ("accounts", "excluded_accounts"):
            accounts = data.get(field, [])
            if not isinstance(accounts, list):
                raise ValueError("recorded result has invalid account summaries")
            for account in accounts:
                if not isinstance(account, Mapping) or not isinstance(account.get("id"), str):
                    raise ValueError("recorded result has invalid account summary")
                account_id = account["id"]
                ids.add(account_id)
                custodian, number = account.get("custodian"), account.get("account_number")
                if isinstance(custodian, str) and isinstance(number, str):
                    external.add((account_id, custodian, number))
        return cls(frozenset(household_ids), frozenset(ids), frozenset(external))

    def allows_external(self, value: Mapping[str, Any]) -> bool:
        custodian, number = value.get("custodian"), value.get("account_number")
        account_id = value.get("account_id")
        if account_id is None and (value.get("kind") == "account"
                                   or str(value.get("id", "")).startswith("WC-AC-")):
            account_id = value.get("id")
        return any(custodian == item[1] and number == item[2]
                   and (account_id is None or account_id == item[0])
                   for item in self.external_accounts)


def _note_field(key: str) -> bool:
    normalized = re.sub(r"[^a-z]", "", key.casefold())
    return normalized.endswith(("note", "notes"))


def _text(value: str, scope: SourceScope) -> str:
    def replace(match: re.Match[str]) -> str:
        token = match.group()
        allowed = scope.household_ids if token.startswith("WC-HH-") else scope.account_ids
        return token if token in allowed else REDACTED
    return _DOMAIN_ID.sub(replace, value)


def _outside_account(value: Mapping[str, Any], scope: SourceScope) -> bool:
    account_id = value.get("account_id")
    entity_id = value.get("id")
    if account_id is not None and account_id not in scope.account_ids:
        return True
    if ((value.get("kind") == "account" or str(entity_id or "").startswith("WC-AC-"))
            and entity_id not in scope.account_ids):
        return True
    if "custodian" in value and "account_number" in value:
        return not scope.allows_external(value)
    return False


def _manifest(value: Mapping[str, Any], scope: SourceScope) -> dict[str, Any]:
    result = dict(value)
    original = result.get("accounts")
    accounts: list[dict[str, Any]] = []
    if isinstance(original, list):
        for account in original:
            if not isinstance(account, Mapping):
                continue
            mapped = {item[0] for item in scope.external_accounts
                      if (item[1], item[2]) == (account.get("custodian"),
                                                account.get("account_number"))}
            if len(mapped) != 1:
                continue
            account_id = next(iter(mapped))
            retained = dict(account)
            if retained.get("account_id") != account_id:
                # A corrected CRM mapping can differ from the upstream manifest's
                # original label. Keep that distinction explicit in the projection.
                original_id = retained.get("account_id")
                retained["received_account_id"] = (original_id if original_id in scope.account_ids
                                                   else REDACTED)
                retained["account_id_scope"] = "origin_result_mapping"
                retained["account_id"] = account_id
            accounts.append(retained)
    result["accounts"] = accounts
    result["expected_account_ids"] = [account["account_id"] for account in accounts]
    counts = [account.get("record_count") for account in accounts]
    snapshot = result.get("feed") in {"pas_snapshots", "custody_snapshots"}
    if snapshot and isinstance(original, list) and all(type(count) is int and count >= 0
                                                      for count in counts):
        result["record_count"] = sum(cast(int, count) for count in counts)
        count_scope = "retained_account_expectations"
    else:
        result.pop("record_count", None)
        count_scope = "omitted"
    # These totals describe a whole delivery and cannot be attributed to the
    # retained account expectations without exposing other households' activity.
    for field in ("accepted_records", "rejected_records", "account_count", "item_count"):
        result.pop(field, None)
    result["projection"] = {
        "scope": "origin_result_accounts", "record_count_scope": count_scope,
        "checksum_scope": "original_received_file",
        "account_id_scope": "origin_result_mapping",
    }
    return result


def _project(value: Any, scope: SourceScope) -> Any:
    if isinstance(value, Mapping):
        if _outside_account(value, scope):
            return REDACTED
        entity_id = value.get("id")
        if ((value.get("kind") == "household" or str(entity_id or "").startswith("WC-HH-"))
                and entity_id not in scope.household_ids):
            return REDACTED
        result: dict[str, Any] = {}
        for key, item in value.items():
            if not isinstance(key, str) or _note_field(key):
                continue
            if "manifest" in value and key in {
                "record_count", "accepted_records", "rejected_records",
                "account_count", "item_count",
            }:
                continue
            if _text(key, scope) != key:
                continue
            if key in {"raw_payload", "raw_content", "unstructured_payload"}:
                result[key] = REDACTED
            elif key == "manifest" and isinstance(item, Mapping):
                result[key] = _project(_manifest(item, scope), scope)
            elif key.endswith("household_id"):
                result[key] = item if item in scope.household_ids else REDACTED
            elif key.endswith("household_ids") and isinstance(item, list):
                result[key] = [entry for entry in item if entry in scope.household_ids]
            elif key.endswith("account_ids") and isinstance(item, list):
                result[key] = [entry for entry in item if entry in scope.account_ids]
            elif key == "account_number" and "custodian" not in value:
                result[key] = REDACTED
            else:
                result[key] = _project(item, scope)
        return result
    if isinstance(value, list):
        return [_project(item, scope) for item in value]
    if isinstance(value, str):
        return _text(value, scope)
    return value


def _foreign_record(value: Any, scope: SourceScope) -> bool:
    """A compound cited record cannot conceal an unowned account in a nested field."""
    if isinstance(value, Mapping):
        if _outside_account(value, scope):
            return True
        return any(_foreign_record(item, scope) for key, item in value.items()
                   if key != "manifest" and isinstance(key, str) and not _note_field(key))
    return isinstance(value, list) and any(_foreign_record(item, scope) for item in value)


def project_source_record(
    record: Mapping[str, Any], scope: SourceScope, *, projection: Projection = "full",
) -> dict[str, Any]:
    """Keep stored hashes labelled as such; projected payloads have a separate hash."""
    stored = record.get("payload")
    if not isinstance(stored, str) or digest(stored) != record.get("payload_hash"):
        raise ValueError("stored source evidence hash does not match")
    original = json.loads(stored)
    if projection not in {"full", "no_pii"}:
        raise ValueError("unknown projection")
    payload = (REDACTED if not isinstance(original, Mapping) or _foreign_record(original, scope)
               else _project(original, scope))
    if projection == "no_pii":
        payload = project_no_pii(payload)
    metadata = _project({key: record[key] for key in _METADATA if key in record}, scope)
    if (payload == REDACTED or projection == "no_pii") and "source_record_id" in metadata:
        metadata["source_record_id"] = REDACTED
    return {
        **metadata, "payload": payload, "payload_hash_scope": "stored_received_record",
        "projection": {
            "version": "1.0", "payload_form": "authorized_projection",
            "redacted": payload != original,
            "projected_payload_hash": digest(canonical(payload)),
        },
    }
