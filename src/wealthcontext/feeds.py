"""Typed received feeds and raw-byte manifests; no publication or database access."""

import hashlib
import json
import math
import unicodedata
from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from datetime import UTC, date, datetime
from decimal import Decimal
from typing import Annotated, Literal, Self

from pydantic import (
    AfterValidator,
    AwareDatetime,
    Field,
    JsonValue,
    StrictBool,
    StrictInt,
    StringConstraints,
    TypeAdapter,
    field_validator,
    model_validator,
)

from wealthcontext.contracts import Contract, Identifier

FeedName = Literal[
    "crm", "custody_accounts", "securities", "pas_snapshots", "custody_snapshots",
    "prices", "transactions", "lots", "fx",
]
SourceSystem = Literal["CRM", "PAS-A", "PAS-B", "CUSTODY-A", "CUSTODY-B", "FX", "SECMASTER"]
Custodian = Literal["A", "B"]
Currency = Literal["USD", "EUR", "CAD", "GBP"]
RunStatus = Literal["SUCCESS", "PARTIAL", "FAILED", "TIMEOUT"]
CanonicalId = Annotated[str, StringConstraints(
    strict=True, pattern=r"^WC-[A-Z0-9][A-Z0-9-]{0,119}$",
)]
AccountNumber = Annotated[str, StringConstraints(
    strict=True, min_length=1, max_length=32, pattern=r"^[A-Za-z0-9][A-Za-z0-9.-]*$",
)]
Checksum = Annotated[str, StringConstraints(pattern=r"^[a-f0-9]{64}$")]


def _text(value: str) -> str:
    if not value.strip() or any(
        unicodedata.category(character).startswith("C") for character in value
    ):
        raise ValueError("text must be visible and contain no control or format characters")
    return value


DisplayName = Annotated[str, StringConstraints(strict=True, min_length=1, max_length=160),
                        AfterValidator(_text)]
AccountTitle = Annotated[str, StringConstraints(strict=True, min_length=1, max_length=240),
                         AfterValidator(_text)]


def _decimal(value: str) -> str:
    if sum(character.isdigit() for character in value) > 28:
        raise ValueError("financial values may contain at most 28 decimal digits")
    return value


SignedDecimal = Annotated[str, StringConstraints(
    strict=True, max_length=64, pattern=r"^-?(0|[1-9][0-9]*)(\.[0-9]+)?$",
), AfterValidator(_decimal)]
NonnegativeDecimal = Annotated[str, StringConstraints(
    strict=True, max_length=64, pattern=r"^(0|[1-9][0-9]*)(\.[0-9]+)?$",
), AfterValidator(_decimal)]


def _utc_representable(value: datetime) -> datetime:
    try:
        value.astimezone(UTC)
    except OverflowError as error:
        raise ValueError("UTC instant is outside years 1 through 9999") from error
    return value


ReceivedTimestamp = Annotated[AwareDatetime, AfterValidator(_utc_representable)]


class FeedRecord(Contract):
    source_record_id: Identifier
    source_asserted_at: ReceivedTimestamp | None = None
    valid_from: date
    valid_to: date

    @model_validator(mode="after")
    def valid_interval(self) -> Self:
        if self.valid_from >= self.valid_to:
            raise ValueError("valid_from must precede valid_to")
        return self


class HouseholdRow(FeedRecord):
    kind: Literal["household"]
    id: CanonicalId
    name: DisplayName


class PersonRow(FeedRecord):
    kind: Literal["person"]
    id: CanonicalId
    external_person_key: Identifier
    name: DisplayName
    note: Annotated[str, StringConstraints(strict=True, max_length=2000)] | None = None


class MembershipRow(FeedRecord):
    kind: Literal["membership"]
    person_id: CanonicalId
    household_id: CanonicalId


class AccountRow(FeedRecord):
    kind: Literal["account"]
    id: CanonicalId
    custodian: Custodian
    account_number: AccountNumber
    is_managed: StrictBool


class ReportingRow(FeedRecord):
    kind: Literal["reporting"]
    account_id: CanonicalId
    household_id: CanonicalId


class AdvisorRow(FeedRecord):
    kind: Literal["advisor"]
    id: Identifier
    name: DisplayName


class TeamRow(FeedRecord):
    kind: Literal["team"]
    id: Identifier
    name: DisplayName


class AdvisorMemberRow(FeedRecord):
    kind: Literal["advisor_member"]
    advisor_id: Identifier
    team_id: Identifier


class TeamCoversRow(FeedRecord):
    kind: Literal["team_covers"]
    team_id: Identifier
    household_id: CanonicalId


class AdvisorCoversRow(FeedRecord):
    kind: Literal["advisor_covers"]
    advisor_id: Identifier
    household_id: CanonicalId


class Owner(Contract):
    entity_id: CanonicalId
    entity_type: Literal["person", "trust"]
    role: Literal["owner", "joint_owner", "trustee"]


class ExternalAccountRow(FeedRecord):
    custodian: Custodian
    account_number: AccountNumber


class OwnershipRow(ExternalAccountRow):
    kind: Literal["ownership"]
    title: AccountTitle
    registration: Literal[
        "INDIVIDUAL", "JTWROS", "TRADITIONAL_IRA", "ROTH_IRA", "REVOCABLE_TRUST",
    ]
    status: Literal["OPEN", "CLOSED"]
    owners: list[Owner] = Field(min_length=1)

    @model_validator(mode="after")
    def registration_owners(self) -> Self:
        if len({owner.entity_id for owner in self.owners}) != len(self.owners):
            raise ValueError("legal owner and trustee entities must be distinct")
        if self.registration == "REVOCABLE_TRUST":
            trusts = [owner for owner in self.owners
                      if owner.entity_type == "trust" and owner.role == "owner"]
            trustees = [owner for owner in self.owners
                        if owner.entity_type == "person" and owner.role == "trustee"]
            if len(trusts) != 1 or not trustees or len(trusts) + len(trustees) != len(self.owners):
                raise ValueError("revocable trusts need one trust entity and at least one trustee")
        elif (any(owner.entity_type != "person" or owner.role not in {"owner", "joint_owner"}
                  for owner in self.owners)
              or (self.registration == "JTWROS" and len(self.owners) < 2)
              or (self.registration != "JTWROS" and (len(self.owners) != 1
                  or self.owners[0].role != "owner"))):
            raise ValueError("registration has an invalid number or type of owners")
        return self


def cusip_check_digit(body: str) -> str:
    """Calculate the CUSIP check digit; allocation restrictions are checked separately."""
    if len(body) != 8 or any(character not in "0123456789ABCDEFGHIJKLMNOPQRSTUVWXYZ*@#"
                             for character in body):
        raise ValueError("CUSIP body must be eight uppercase CUSIP characters")
    total = 0
    for index, character in enumerate(body):
        value = (int(character) if character.isdigit() else ord(character) - ord("A") + 10
                 if character.isalpha() else {"*": 36, "@": 37, "#": 38}[character])
        product = value * (2 if index % 2 else 1)
        total += product // 10 + product % 10
    return str((10 - total % 10) % 10)


def _validate_cusip(value: str) -> str:
    if (len(value) != 9 or not value[:6].isdigit() or not 990000 <= int(value[:6]) <= 999999
            or not value[-1].isdigit() or cusip_check_digit(value[:8]) != value[-1]):
        raise ValueError("CUSIP must have a valid check digit in the synthetic internal-use range")
    return value


def make_us_isin(cusip: str) -> str:
    """Build a synthetic US ISIN from a validated internal-use CUSIP."""
    _validate_cusip(cusip)
    body = "US" + cusip
    digits = "".join(str(ord(character) - ord("A") + 10) if character.isalpha() else character
                     for character in body)
    if not digits.isdigit():
        raise ValueError("US ISIN construction requires an alphanumeric CUSIP")
    total = 0
    for index, digit in enumerate(reversed(digits + "0")):
        product = int(digit) * (2 if index % 2 else 1)
        total += product // 10 + product % 10
    return body + str((10 - total % 10) % 10)


class SecurityRow(FeedRecord):
    kind: Literal["security"]
    id: CanonicalId
    issuer_id: CanonicalId
    issuer_name: DisplayName
    name: DisplayName
    ticker: Annotated[str, StringConstraints(strict=True, pattern=r"^[A-Z][A-Z0-9.]{0,11}$")]
    type: Literal["COMMON_STOCK", "ETF", "MUTUAL_FUND", "MMF"]
    currency: Currency
    cusip: Annotated[str, AfterValidator(_validate_cusip)]
    identifier_status: Literal["synthetic_unallocated"]
    isin: str | None = None
    listing_country: Annotated[str, StringConstraints(strict=True, pattern=r"^[A-Z]{2}$")]

    @model_validator(mode="after")
    def synthetic_identifiers(self) -> Self:
        if self.isin is not None and (self.listing_country != "US"
                                      or self.isin != make_us_isin(self.cusip)):
            raise ValueError("only US listings may carry the corresponding synthetic US ISIN")
        return self


class PASPrice(Contract):
    """Optional secondary price observation carried by a received PAS position."""
    price: NonnegativeDecimal
    price_date: date
    currency: Currency


class PositionRow(ExternalAccountRow):
    kind: Literal["position"]
    security_id: CanonicalId
    quantity: NonnegativeDecimal
    pas_price: PASPrice | None = None


class CashRow(ExternalAccountRow):
    kind: Literal["cash"]
    currency: Currency
    amount: NonnegativeDecimal


class PASGroupingRow(ExternalAccountRow):
    kind: Literal["pas_grouping"]
    household_id: CanonicalId


class TransactionRow(ExternalAccountRow):
    kind: Literal["transaction"]
    id: CanonicalId
    security_id: CanonicalId | None = None
    txn_type: Literal[
        "BUY", "SELL", "DEPOSIT", "WITHDRAWAL", "DIVIDEND", "INTEREST", "FEE", "TRANSFER",
    ]
    trade_date: date
    settle_date: date
    quantity_delta: SignedDecimal
    cash_delta_local: SignedDecimal
    currency: Currency
    status: Literal["ACTIVE", "CANCELLED"]

    @model_validator(mode="after")
    def trade_semantics(self) -> Self:
        if self.settle_date < self.trade_date:
            raise ValueError("settle_date must not precede trade_date")
        quantity, cash = Decimal(self.quantity_delta), Decimal(self.cash_delta_local)
        if self.txn_type in {"BUY", "SELL"}:
            if self.security_id is None:
                raise ValueError("security trades require security_id")
            if (self.txn_type == "BUY" and (quantity <= 0 or cash > 0)
                    or self.txn_type == "SELL" and (quantity >= 0 or cash < 0)):
                raise ValueError("buy/sell quantity and cash signs disagree")
        elif quantity != 0:
            raise ValueError("cash transactions cannot change security quantity")
        return self


class LotRow(ExternalAccountRow):
    kind: Literal["lot"]
    id: CanonicalId
    transaction_id: CanonicalId | None = None
    security_id: CanonicalId
    open_date: date
    quantity: NonnegativeDecimal
    cost_basis_local: NonnegativeDecimal
    currency: Currency


class PriceRow(FeedRecord):
    kind: Literal["price"]
    security_id: CanonicalId
    currency: Currency
    price: NonnegativeDecimal
    price_date: date


class FXRow(FeedRecord):
    kind: Literal["fx"]
    currency: Currency
    usd_rate: NonnegativeDecimal
    rate_at: ReceivedTimestamp

    @model_validator(mode="after")
    def positive_rate(self) -> Self:
        if Decimal(self.usd_rate) <= 0 or (self.currency == "USD" and Decimal(self.usd_rate) != 1):
            raise ValueError("FX rates must be positive and USD identity rates must equal one")
        return self


FeedRow = Annotated[
    HouseholdRow | PersonRow | MembershipRow | AccountRow | ReportingRow | AdvisorRow | TeamRow
    | AdvisorMemberRow | TeamCoversRow | AdvisorCoversRow | OwnershipRow | SecurityRow
    | PositionRow | CashRow | PASGroupingRow | TransactionRow | LotRow | PriceRow | FXRow,
    Field(discriminator="kind"),
]
ROW_ADAPTER = TypeAdapter(FeedRow)
SNAPSHOT_FEEDS = {"pas_snapshots", "custody_snapshots"}
FEED_KINDS: dict[str, set[str]] = {
    "crm": {"household", "person", "membership", "account", "reporting", "advisor", "team",
            "advisor_member", "team_covers", "advisor_covers", "ownership"},
    "custody_accounts": {"ownership"}, "securities": {"security"},
    "pas_snapshots": {"position", "cash", "pas_grouping"},
    "custody_snapshots": {"position", "cash"}, "prices": {"price"},
    "transactions": {"transaction"}, "lots": {"lot"}, "fx": {"fx"},
}
FEED_SOURCES: dict[str, set[str]] = {
    "crm": {"CRM"}, "custody_accounts": {"CUSTODY-A", "CUSTODY-B"},
    "securities": {"SECMASTER"}, "pas_snapshots": {"PAS-A", "PAS-B"},
    "custody_snapshots": {"CUSTODY-A", "CUSTODY-B"},
    "prices": {"CUSTODY-A", "CUSTODY-B"}, "transactions": {"PAS-A", "PAS-B"},
    "lots": {"PAS-A", "PAS-B"}, "fx": {"FX"},
}


def validate_feed_source(feed: FeedName, source_system: SourceSystem) -> None:
    if source_system not in FEED_SOURCES[feed]:
        raise ValueError("source_system cannot supply this feed")


def parse_row(feed: FeedName, raw: object, *, source_system: SourceSystem | None = None) -> FeedRow:
    row = ROW_ADAPTER.validate_python(raw)
    if row.kind not in FEED_KINDS[feed]:
        raise ValueError("record kind is not permitted in this feed")
    if isinstance(row, PositionRow) and row.pas_price is not None and feed != "pas_snapshots":
        raise ValueError("secondary PAS prices belong only to PAS position snapshots")
    if source_system is not None:
        validate_feed_source(feed, source_system)
        if isinstance(row, ExternalAccountRow | AccountRow) and source_system != "CRM":
            if row.custodian != source_system.rsplit("-", 1)[-1]:
                raise ValueError("record custodian does not match its source_system")
    return row


def canonical_payload(row: FeedRow) -> dict[str, JsonValue]:
    """Notes remain in received source bytes, but never enter canonical context facts."""
    return row.model_dump(mode="json", exclude={"note"} if isinstance(row, PersonRow) else set())


def canonical_json(value: object) -> bytes:
    return json.dumps(value, sort_keys=True, separators=(",", ":"), ensure_ascii=False,
                      allow_nan=False).encode("utf-8")


def content_hash(content: bytes) -> str:
    return hashlib.sha256(content).hexdigest()


def records_digest(records: Sequence[JsonValue]) -> str:
    # Count and duplicate rows are preserved; row ordering does not change the
    # account digest. The enclosing file checksum still binds exact byte order.
    rows = sorted(canonical_json(row) for row in records)
    return content_hash(b"[" + b",".join(rows) + b"]")


class FeedContent(Contract):
    source_system: SourceSystem
    feed: FeedName
    business_date: date
    records: list[JsonValue]

    @model_validator(mode="after")
    def matching_source(self) -> Self:
        validate_feed_source(self.feed, self.source_system)
        return self


class FeedHeader(Contract):
    source_system: SourceSystem
    feed: FeedName
    business_date: date
    format: Literal["wealthcontext-feed-v1"]

    @field_validator("business_date", mode="before")
    @classmethod
    def exact_business_date(cls, value: object) -> date:
        if type(value) is date:
            return value
        if not isinstance(value, str) or date.fromisoformat(value).isoformat() != value:
            raise ValueError("feed business_date must be YYYY-MM-DD")
        return date.fromisoformat(value)

    @model_validator(mode="after")
    def matching_source(self) -> Self:
        validate_feed_source(self.feed, self.source_system)
        return self


def encode_content(
    source_system: SourceSystem, feed: FeedName, business_date: date, records: Sequence[JsonValue],
) -> bytes:
    # Deliberately validate only the framing and JSON values. Invalid business
    # rows must reach ingestion intact for account-atomic quarantine. A complete
    # line can be independently recovered after a later line is truncated.
    envelope = FeedContent(source_system=source_system, feed=feed,
                           business_date=business_date, records=list(records))
    header = FeedHeader(source_system=source_system, feed=feed, business_date=business_date,
                        format="wealthcontext-feed-v1")
    return (canonical_json(header.model_dump(mode="json")) + b"\n"
            + b"".join(canonical_json(row) + b"\n" for row in envelope.records))


def _unique_object(pairs: list[tuple[str, JsonValue]]) -> dict[str, JsonValue]:
    result: dict[str, JsonValue] = {}
    for key, value in pairs:
        if key in result:
            raise ValueError("duplicate JSON object field")
        result[key] = value
    return result


def _invalid_constant(_value: str) -> None:
    raise ValueError("non-finite JSON numeric literal")


def _finite_float(value: str) -> float:
    parsed = float(value)
    if not math.isfinite(parsed):
        raise ValueError("non-finite JSON numeric literal")
    return parsed


def _decode_line(line: bytes) -> JsonValue:
    return json.loads(line.decode("utf-8"), object_pairs_hook=_unique_object,
                      parse_constant=_invalid_constant, parse_float=_finite_float)


def decode_content(content: bytes, *, allow_partial: bool = False) -> FeedContent:
    """Decode newline-framed received bytes without guessing incomplete records.

    Header validation is unconditional. When partial recovery is authorized for
    snapshot feeds, malformed row lines become inert non-object quarantine values;
    account manifests still decide which complete groups can publish. Original
    bytes belong to the durable feed receipt and are never replaced by this view.
    """
    header_line, separator, body = content.partition(b"\n")
    if not separator:
        raise ValueError("feed content requires a complete newline-terminated header")
    header = FeedHeader.model_validate(_decode_line(header_line))
    lines = body.split(b"\n")
    if lines[-1] == b"":
        lines.pop()  # Encoder's final line terminator is not a blank record.
    records: list[JsonValue] = []
    for line in lines:
        try:
            records.append(_decode_line(line))
        except ValueError:
            if not allow_partial:
                raise
            records.append(line.decode("utf-8", errors="replace"))
    return FeedContent(source_system=header.source_system, feed=header.feed,
                       business_date=header.business_date, records=records)


class AccountReference(Contract):
    account_id: CanonicalId
    custodian: Custodian
    account_number: AccountNumber


class AccountExpectation(AccountReference):
    record_count: StrictInt = Field(ge=0)
    digest: Checksum


class Manifest(Contract):
    source_system: SourceSystem
    feed: FeedName
    business_date: date
    record_count: StrictInt = Field(ge=0)
    checksum: Checksum
    expected_account_ids: list[CanonicalId] = Field(default_factory=list)
    accounts: list[AccountExpectation] = Field(default_factory=list)

    @model_validator(mode="after")
    def matching_accounts(self) -> Self:
        validate_feed_source(self.feed, self.source_system)
        ids = [account.account_id for account in self.accounts]
        keys = [(account.custodian, account.account_number) for account in self.accounts]
        if (len(set(ids)) != len(ids) or len(set(keys)) != len(keys)
                or len(set(self.expected_account_ids)) != len(self.expected_account_ids)
                or set(ids) != set(self.expected_account_ids)):
            raise ValueError("manifest canonical IDs and external account keys must be unique")
        if self.feed not in SNAPSHOT_FEEDS and (ids or self.expected_account_ids):
            raise ValueError("per-account expectations are only supported for snapshot feeds")
        if any(account.custodian != self.source_system.rsplit("-", 1)[-1]
               for account in self.accounts):
            raise ValueError("manifest account custodian does not match source_system")
        return self


def account_key(raw: object) -> tuple[str, str] | None:
    if isinstance(raw, Mapping):
        custodian, number = raw.get("custodian"), raw.get("account_number")
        if isinstance(custodian, str) and isinstance(number, str):
            return custodian, number
    return None


def make_manifest(
    *, source_system: SourceSystem, feed: FeedName, business_date: date,
    records: Sequence[JsonValue],
    expected_accounts: Sequence[AccountReference | dict] = (),
) -> Manifest:
    accounts = sorted((AccountReference.model_validate(account) for account in expected_accounts),
                      key=lambda account: account.account_id)
    expectations = []
    for account in accounts:
        group = [row for row in records
                 if account_key(row) == (account.custodian, account.account_number)]
        reference = account.model_dump(include={"account_id", "custodian", "account_number"})
        expectations.append(AccountExpectation(**reference, record_count=len(group),
                                               digest=records_digest(group)))
    return Manifest(source_system=source_system, feed=feed, business_date=business_date,
                    record_count=len(records), checksum=content_hash(encode_content(
                        source_system, feed, business_date, records,
                    )), expected_account_ids=[account.account_id for account in accounts],
                    accounts=expectations)


@dataclass(frozen=True)
class Delivery:
    manifest: Manifest
    content: bytes | None
    upstream_status: RunStatus
    delivery_id: str

    def __post_init__(self) -> None:
        if self.content is None and self.upstream_status not in {"FAILED", "TIMEOUT"}:
            raise ValueError("missing received content requires a FAILED or TIMEOUT fetch")
        if self.upstream_status not in {"SUCCESS", "PARTIAL", "FAILED", "TIMEOUT"}:
            raise ValueError("invalid upstream status")
        TypeAdapter(Identifier).validate_python(self.delivery_id)


def build_delivery(
    *, source_system: SourceSystem, feed: FeedName, business_date: date,
    records: Sequence[JsonValue] | None,
    intended_records: Sequence[JsonValue] | None = None,
    expected_accounts: Sequence[AccountReference | dict] = (),
    upstream_status: RunStatus = "SUCCESS", delivery_id: str | None = None,
) -> Delivery:
    expected = intended_records if intended_records is not None else records or []
    manifest = make_manifest(source_system=source_system, feed=feed, business_date=business_date,
                             records=expected, expected_accounts=expected_accounts)
    content = (encode_content(source_system, feed, business_date, records)
               if records is not None else None)
    fingerprint = content_hash(content) if content is not None else manifest.checksum
    delivery_id = delivery_id or (
        f"delivery-{feed}-{source_system}-{business_date}-{upstream_status}-{fingerprint[:12]}"
    )
    return Delivery(manifest=manifest, content=content, upstream_status=upstream_status,
                    delivery_id=delivery_id)
