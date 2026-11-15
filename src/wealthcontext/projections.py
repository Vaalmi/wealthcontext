"""PII projections: closed output objects and conservative untrusted evidence."""

import re
from collections.abc import Mapping
from typing import Any, Literal, get_args

from wealthcontext.contracts import QualityCode
from wealthcontext.storage import canonical

REDACTED = "[REDACTED]"
Projection = Literal["full", "no_pii"]


def _normalized(key: str) -> str:
    return re.sub(r"[^a-z]", "", key.casefold())


def _note(key: str) -> bool:
    return _normalized(key).endswith(("note", "notes"))


def _pii(key: str) -> bool:
    return _normalized(key).endswith(("name", "names", "title", "titles"))


# These fields have bounded machine meanings. Unknown keys are omitted, rather
# than returning attacker-controlled key names from received JSON.
_CONTAINERS = {
    "manifest", "accounts", "owners", "detail", "details", "projection", "records",
    "expected_account_ids", "pending_transaction_ids", "legal_owner_ids", "member_ids",
    "household_ids", "account_ids", "source_ids", "missing_components", "pas_price",
    "fields",
}
_IDS = {
    "id", "account_id", "household_id", "person_id", "security_id", "issuer_id", "entity_id",
    "current_household_id", "counterpart_household_id", "reporting_household_id",
    "received_account_id", "subject", "transaction_id", "lot_id", "components",
    "missing_components",
}
_DECIMALS = {
    "quantity", "quantity_delta", "amount", "price", "price_local", "rate", "fx_rate",
    "cash_delta_local", "cost_basis_local", "settled", "authoritative", "pending_delta",
    "difference", "cash_amount", "market_value", "last_known_value_usd", "custody_price",
    "usd_rate",
}
_DATES = {
    "business_date", "recorded_at", "imported_at", "source_asserted_at", "valid_from",
    "valid_to", "price_date", "trade_date", "settle_date", "rate_at", "valuation_date",
    "custody_price_date",
}
_ENUMS = {
    "kind", "feed", "source_system", "currency", "custodian", "status", "upstream_status",
    "registration", "role", "entity_type", "security_type", "reconciliation_status",
    "reason", "code", "severity", "account_id_scope", "record_count_scope", "checksum_scope",
    "source", "authority", "reasons", "custody_currency",
    "authoritative_source", "membership_source", "ownership_source",
}
_VALUES = {
    "household", "person", "membership", "account", "reporting", "ownership",
    "crm_ownership", "security", "position", "cash", "price", "fx", "transaction", "lot",
    "pas_grouping", "crm", "custody_accounts", "securities", "pas_snapshots",
    "custody_snapshots", "prices", "transactions", "lots", "CRM", "PAS", "CUSTODY",
    "PAS-A", "PAS-B", "CUSTODY-A", "CUSTODY-B", "FX", "SECMASTER", "USD", "CAD", "EUR",
    "GBP", "A", "B", "SUCCESS", "PARTIAL", "FAILED", "TIMEOUT", "ACTIVE", "CANCELLED",
    "OPEN", "CLOSED", "INDIVIDUAL", "JTWROS", "TRADITIONAL_IRA", "ROTH_IRA",
    "REVOCABLE_TRUST", "owner", "joint_owner", "trustee", "trust", "COMMON_STOCK", "ETF",
    "MUTUAL_FUND", "MMF", "MATCHED", "PENDING_SETTLEMENT", "BREAK",
    "RECONCILIATION_UNAVAILABLE", "info", "warning", "origin_result_mapping",
    "retained_account_expectations", "original_received_file", "omitted",
    "FIX_UNAVAILABLE", "FIX_OUTSIDE_CUTOFFS", "AMBIGUOUS_FIX",
    "HOUSEHOLD_UNAVAILABLE_AT_CUTOFFS", "ACCOUNT_CROSSWALK_UNAVAILABLE",
    "AMBIGUOUS_SNAPSHOT", "EXPECTED_ACCOUNT_MISSING_FROM_MANIFEST", "MISSING_RUN",
    "MULTIPLE_UNAVAILABLE_PUBLICATIONS", "ACCOUNT_MANIFEST_MISMATCH",
    "SNAPSHOT_VALIDITY_MISMATCH", "DUPLICATE_ACCOUNT_COMPONENT", "INVALID_ACCOUNT_ROW",
    "CURRENCY", "CLOSE_DATE", "PRICE",
    "LEGAL_OWNER_NOT_CURRENT_MEMBER", "CRM_CUSTODY_DISAGREEMENT",
    "AMBIGUOUS_CRM_OWNERSHIP", "AMBIGUOUS_CUSTODY_OWNERSHIP",
    "MISSING_CUSTODY_OWNERSHIP", "AMBIGUOUS_TRANSACTIONS", "reporting_household",
} | set(get_args(QualityCode))
_OWNERSHIP_FIELDS = {"title", "registration", "status", "owners"}
_SCALARS = {
    "record_count", "account_count", "accepted_records", "rejected_records", "item_count",
    "is_managed", "file_received", "affects_valuation", "age_business_days",
    "price_age_business_days", "allowed_age_business_days", "position_count", "cash_count",
    "tolerance_business_days",
}
_PII_FIELDS = {"name", "title", "household_name", "person_name", "account_title",
               "issuer_name", "security_name", "first_name", "last_name"}


def project_no_pii(value: Any, *, field: str = "") -> Any:
    """Project received evidence, never treating arbitrary strings as safe records.

    Unknown properties and source identifiers can carry names even when syntactically
    valid JSON. Preserve only semantic fields with verifiable machine representations.
    """
    if isinstance(value, Mapping):
        result = {}
        for key, item in value.items():
            if not isinstance(key, str) or _note(key):
                continue
            if key in _PII_FIELDS:
                result[key] = REDACTED
            elif key in _CONTAINERS | _IDS | _DECIMALS | _DATES | _ENUMS | _SCALARS:
                result[key] = project_no_pii(item, field=key)
            elif key in {"checksum", "digest", "content_hash", "payload_hash"}:
                result[key] = (item if isinstance(item, str)
                               and re.fullmatch(r"[a-f0-9]{64}", item) else REDACTED)
        return result
    if isinstance(value, list):
        return [project_no_pii(item, field=field) for item in value]
    if value is None or type(value) in {bool, int}:
        return value
    if not isinstance(value, str):
        return REDACTED
    if value == REDACTED:
        return value
    if field == "fields":
        return value if value in _OWNERSHIP_FIELDS else REDACTED
    if ((field in _IDS or field.endswith("_ids"))
            and re.fullmatch(r"WC-[A-Za-z0-9_.:@-]{1,125}", value)):
        return value
    if field in _DECIMALS and re.fullmatch(r"-?(0|[1-9][0-9]*)(\.[0-9]+)?", value):
        return value
    if field in _DATES and re.fullmatch(r"\d{4}-\d\d-\d\d(?:T[0-9:.]+(?:Z|[+-]\d\d:\d\d))?",
                                      value):
        return value
    if field in _ENUMS and value in _VALUES:
        return value
    return REDACTED


def _closed(value: Any, projection: Projection) -> Any:
    if isinstance(value, dict):
        result = {}
        for key, item in value.items():
            if _note(key):
                continue
            if key in {"raw_payload", "raw_content", "unstructured_payload"}:
                result[key] = REDACTED
            elif projection == "no_pii" and _pii(key):
                result[key] = REDACTED
            elif projection == "no_pii" and key == "detail":
                result[key] = project_no_pii(item)
            else:
                result[key] = _closed(item, projection)
        if (projection == "no_pii" and isinstance(result.get("quality_flags"), list)
                and all(isinstance(flag, dict) for flag in result["quality_flags"])):
            # Projection can collapse distinct received details. Stable order and
            # deduplication must describe the released diagnostics, not raw inputs.
            unique = {canonical(flag): flag for flag in result["quality_flags"]}
            result["quality_flags"] = sorted(
                unique.values(),
                key=lambda flag: (str(flag.get("subject", "")), str(flag.get("code", "")),
                                  canonical(flag.get("detail", {}))),
            )
        return result
    if isinstance(value, list):
        return [_closed(item, projection) for item in value]
    return value


def project_envelope(envelope: dict, projection: Projection) -> dict:
    """Redact closed, validated response contracts without mutating durable inputs."""
    if projection not in {"full", "no_pii"}:
        raise ValueError("unknown projection")
    return _closed(envelope, projection)
