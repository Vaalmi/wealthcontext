"""Closed wire contracts for portfolio exposure and issuer concentration."""

from datetime import date
from typing import Annotated, Literal, Self

from pydantic import ConfigDict, Field, StrictBool, StrictInt, StringConstraints, model_validator

from wealthcontext.contracts import (
    ENVELOPE_STATUS_RULES,
    Contract,
    DecimalString,
    Envelope,
    Identifier,
    QualityFlag,
)

Count = Annotated[StrictInt, Field(ge=0)]
Rank = Annotated[StrictInt, Field(ge=1)]
Label = Annotated[str, StringConstraints(min_length=1, max_length=160)]
ThresholdPct = Annotated[str, StringConstraints(
    pattern=r"^(?:(?:[1-9]|[1-4][0-9])\.[0-9]{2}|50\.00)$",
)]


class IssuerAmountRow(Contract):
    entity_id: Identifier
    entity_type: Literal["issuer"] = "issuer"
    label: Label
    exposure_kind: Literal["operating_company"] = "operating_company"
    exposure_usd: DecimalString
    rank: Rank


class IssuerPercentageRow(IssuerAmountRow):
    concentration_pct: DecimalString


IssuerRow = IssuerAmountRow | IssuerPercentageRow


class SecurityRowBase(Contract):
    entity_id: Identifier
    entity_type: Literal["security"] = "security"
    label: Label
    exposure_usd: DecimalString
    rank: Rank


class SecurityAmountRow(SecurityRowBase):
    exposure_kind: Literal["operating_company", "fund_vehicle"]


class SecurityPercentageRow(SecurityAmountRow):
    allocation_pct: DecimalString


class FundAmountRow(SecurityRowBase):
    exposure_kind: Literal["fund_vehicle"] = "fund_vehicle"


class FundPercentageRow(FundAmountRow):
    allocation_pct: DecimalString


SecurityRow = SecurityAmountRow | SecurityPercentageRow
FundRow = FundAmountRow | FundPercentageRow


class ListMetadata(Contract):
    item_count: Count
    total_item_count: Count
    truncated: StrictBool
    next_cursor: None = None

    @model_validator(mode="after")
    def consistent_counts(self) -> Self:
        if self.total_item_count < self.item_count:
            raise ValueError("total_item_count cannot be less than item_count")
        if self.truncated != (self.total_item_count > self.item_count):
            raise ValueError("truncated must disclose omitted items")
        return self


class ExposureList[Row](ListMetadata):
    items: list[Row] = Field(max_length=20)
    ordering: Literal["exposure_usd_desc,entity_id_asc"] = "exposure_usd_desc,entity_id_asc"

    @model_validator(mode="after")
    def matching_items(self) -> Self:
        if len(self.items) != self.item_count:
            raise ValueError("item_count must match returned items")
        return self


class IssuerExposureList(ExposureList[IssuerRow]):
    rank_scope: Literal["household_issuers"] = "household_issuers"


class FundVehicleExposureList(ExposureList[FundRow]):
    rank_scope: Literal["household_fund_vehicles"] = "household_fund_vehicles"


class SecurityExposureList(ExposureList[SecurityRow]):
    rank_scope: Literal["household_securities"] = "household_securities"


class EmptyIssuerExposureList(IssuerExposureList):
    items: list[IssuerRow] = Field(max_length=0)
    item_count: Count = Field(default=0, le=0)
    total_item_count: Count = Field(default=0, le=0)
    truncated: StrictBool = Field(default=False, json_schema_extra={"const": False})


class EmptyFundVehicleExposureList(FundVehicleExposureList):
    items: list[FundRow] = Field(max_length=0)
    item_count: Count = Field(default=0, le=0)
    total_item_count: Count = Field(default=0, le=0)
    truncated: StrictBool = Field(default=False, json_schema_extra={"const": False})


class EmptySecurityExposureList(SecurityExposureList):
    items: list[SecurityRow] = Field(max_length=0)
    item_count: Count = Field(default=0, le=0)
    total_item_count: Count = Field(default=0, le=0)
    truncated: StrictBool = Field(default=False, json_schema_extra={"const": False})


class HouseholdExposure(Contract):
    household_id: Identifier
    household_name: Label


class CompleteIssuerExposure(HouseholdExposure):
    group_by: Literal["issuer"] = "issuer"
    assets_usd: DecimalString
    issuer_exposures: IssuerExposureList
    fund_vehicle_exposures: FundVehicleExposureList


class CompleteSecurityExposure(HouseholdExposure):
    group_by: Literal["security"] = "security"
    assets_usd: DecimalString
    security_exposures: SecurityExposureList


class StaleExposureComponent(Contract):
    subject: Identifier
    last_known_value_usd: DecimalString | None
    price_date: date


class IncompleteExposure(HouseholdExposure):
    fresh_subtotal_usd: DecimalString
    missing_components: list[Identifier]
    stale_components: list[StaleExposureComponent]


class IncompleteIssuerExposure(IncompleteExposure):
    group_by: Literal["issuer"] = "issuer"
    issuer_exposures: EmptyIssuerExposureList
    fund_vehicle_exposures: EmptyFundVehicleExposureList


class IncompleteSecurityExposure(IncompleteExposure):
    group_by: Literal["security"] = "security"
    security_exposures: EmptySecurityExposureList


ExposurePayload = (
    CompleteIssuerExposure | CompleteSecurityExposure
    | IncompleteIssuerExposure | IncompleteSecurityExposure
)


def _status_schema(schema: dict, complete_count: int, incomplete_count: int) -> None:
    """Reuse Pydantic-generated refs in standalone JSON Schema and FastAPI OpenAPI."""
    # Before Pydantic finalizes its schema, refs have internal names. Preserve
    # refs from the declared union order instead of inventing final ref paths.
    variants = [item for item in schema["properties"]["data"]["anyOf"] if "$ref" in item]
    if len(variants) != complete_count + incomplete_count:
        raise ValueError("status schema could not locate every payload variant")
    rules = list(ENVELOPE_STATUS_RULES)
    for status, selected in (("COMPLETE", variants[:complete_count]),
                             ("INCOMPLETE", variants[complete_count:])):
        rules.append({
            "if": {"properties": {"status": {"const": status}}},
            "then": {"properties": {"data": {"anyOf": selected}}},
        })
    schema["allOf"] = rules


def exposure_status_schema(schema: dict) -> None:
    _status_schema(schema, 2, 2)


class ExposureEnvelope(Envelope[ExposurePayload]):
    model_config = ConfigDict(json_schema_extra=exposure_status_schema)

    @model_validator(mode="after")
    def matching_exposure_status(self) -> Self:
        if self.status == "COMPLETE" and not isinstance(
            self.data, CompleteIssuerExposure | CompleteSecurityExposure,
        ):
            raise ValueError("COMPLETE requires complete exposure")
        if self.status == "INCOMPLETE" and not isinstance(
            self.data, IncompleteIssuerExposure | IncompleteSecurityExposure,
        ):
            raise ValueError("INCOMPLETE requires incomplete exposure")
        return self


class ConcentrationBreach(Contract):
    household_id: Identifier
    issuer_id: Identifier
    label: Label
    exposure_usd: DecimalString
    assets_usd: DecimalString
    concentration_pct: DecimalString
    book_rank: Rank
    issuer_rank: Rank
    meets_threshold: Literal[True] = True


class IndeterminateHousehold(Contract):
    household_id: Identifier
    reason: Literal["INCOMPLETE_ASSETS", "NON_POSITIVE_ASSETS"]
    fresh_subtotal_usd: DecimalString
    stale_component_count: Count
    missing_account_ids: list[Identifier]
    blocking_flags: list[QualityFlag]


class BreachMetadata(ListMetadata):
    ordering: Literal[
        "concentration_pct_desc,household_id_asc,issuer_id_asc"
    ] = "concentration_pct_desc,household_id_asc,issuer_id_asc"
    rank_scope: Literal["book_concentration_breaches"] = "book_concentration_breaches"


class IndeterminateMetadata(ListMetadata):
    ordering: Literal["household_id_asc"] = "household_id_asc"
    truncated: StrictBool = Field(default=False, json_schema_extra={"const": False})

    @model_validator(mode="after")
    def every_indeterminate_is_returned(self) -> Self:
        if self.truncated or self.item_count != self.total_item_count:
            raise ValueError("indeterminate households cannot be truncated")
        return self


class ConcentrationBase(Contract):
    applied_threshold_pct: ThresholdPct
    comparator: Literal[">="] = ">="
    evaluated_household_count: Count
    determinate_household_count: Count
    indeterminate_household_count: Count
    breaching_household_count: Count
    breaches: list[ConcentrationBreach] = Field(max_length=20)
    indeterminate: list[IndeterminateHousehold]
    breach_metadata: BreachMetadata
    indeterminate_metadata: IndeterminateMetadata

    @model_validator(mode="after")
    def matching_concentration_counts(self) -> Self:
        if self.evaluated_household_count != (
            self.determinate_household_count + self.indeterminate_household_count
        ):
            raise ValueError("evaluated household count must include every outcome")
        if self.indeterminate_household_count != len(self.indeterminate):
            raise ValueError("every indeterminate household must be retained")
        if len(self.indeterminate) != self.indeterminate_metadata.item_count:
            raise ValueError("indeterminate metadata must match returned households")
        if len(self.breaches) != self.breach_metadata.item_count:
            raise ValueError("breach metadata must match returned breaches")
        shown_households = len({row.household_id for row in self.breaches})
        if not (shown_households <= self.breaching_household_count
                <= self.determinate_household_count):
            raise ValueError("breaching households must be distinct determinate households")
        if (not self.breach_metadata.truncated
                and shown_households != self.breaching_household_count):
            raise ValueError("untruncated breach household count must be exact")
        if len({row.household_id for row in self.indeterminate}) != len(self.indeterminate):
            raise ValueError("indeterminate households must be unique")
        if {row.household_id for row in self.indeterminate}.intersection(
            row.household_id for row in self.breaches
        ):
            raise ValueError("indeterminate households cannot emit concentration breaches")
        return self


class CompleteConcentration(ConcentrationBase):
    indeterminate: list[IndeterminateHousehold] = Field(max_length=0)
    indeterminate_household_count: Count = Field(default=0, le=0)


class IncompleteConcentration(ConcentrationBase):
    indeterminate: list[IndeterminateHousehold] = Field(min_length=1)
    indeterminate_household_count: Count = Field(ge=1)


ConcentrationPayload = CompleteConcentration | IncompleteConcentration


def concentration_status_schema(schema: dict) -> None:
    _status_schema(schema, 1, 1)


class ConcentrationEnvelope(Envelope[ConcentrationPayload]):
    model_config = ConfigDict(json_schema_extra=concentration_status_schema)

    @model_validator(mode="after")
    def matching_concentration_status(self) -> Self:
        if self.status == "COMPLETE" and not isinstance(self.data, CompleteConcentration):
            raise ValueError("COMPLETE requires no indeterminate households")
        if self.status == "INCOMPLETE" and not isinstance(self.data, IncompleteConcentration):
            raise ValueError("INCOMPLETE requires indeterminate households")
        return self
