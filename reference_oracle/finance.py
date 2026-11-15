"""Independent Decimal reference calculations over accepted received evidence."""

from datetime import date, datetime, timedelta
from decimal import ROUND_HALF_EVEN, ROUND_HALF_UP, Decimal, localcontext

from .history import Observation, State
from .wire import EvidenceError, History, business_date, canonical, instant, number

START, END = date(2026, 9, 14), date(2026, 10, 16)


def valuation_date(as_of: date) -> date:
    if type(as_of) is not date:
        raise ValueError("as_of must be a business-date value")
    day = as_of
    while day.weekday() >= 5:
        day -= timedelta(days=1)
    if not START <= day <= END:
        raise ValueError("AS_OF_OUT_OF_RANGE")
    return day


def _age(close: date, day: date) -> int:
    # The fixed MVP window contains 25 weekdays and no US market holiday.
    return sum(
        (close + timedelta(days=offset)).weekday() < 5
        for offset in range(1, (day - close).days + 1)
    )


def _money(value: Decimal) -> str:
    with localcontext() as output:
        output.prec = max(28, value.adjusted() + 4)
        return format(value.quantize(Decimal("0.01"), rounding=ROUND_HALF_UP), ".2f")


def _unique(values: list[dict]) -> list[dict]:
    return [value for _key, value in sorted({canonical(value): value for value in values}.items())]


def _rank(
    values: dict[str, Decimal],
    total: Decimal,
    *,
    field: str,
    percentage_field: str,
) -> list[dict]:
    ordered = sorted(values.items(), key=lambda pair: (-pair[1], pair[0]))
    result = []
    for index, (key, value) in enumerate(ordered, 1):
        item = {
            field: key,
            "market_value_usd": _money(value),
            "rank": index,
        }
        if total > 0:
            item[percentage_field] = _money(value / total * 100)
        result.append(item)
    return result


def _list_output(
    items: list[dict], *, rank_scope: str | None, ordering: list[str], limit: int | None = 20
) -> dict:
    displayed = items if limit is None else items[:limit]
    return {
        "items": displayed,
        "rank_scope": rank_scope,
        "item_count": len(displayed),
        "total_item_count": len(items),
        "ordering": ordering,
        "truncated": len(displayed) < len(items),
        "next_cursor": None,
    }


def _household(
    state: State, household_id: str, day: date, known: datetime, threshold: Decimal
) -> tuple[dict, list[tuple[Decimal, dict]], dict | None]:
    views = {
        kind: state.view(kind, day)
        for kind in (
            "household",
            "person",
            "membership",
            "account",
            "reporting",
            "security",
            "ownership",
        )
    }
    evidence, flags, blocking = [], [], []
    missing, missing_accounts = set(), set()
    included, excluded, stale, positions, cash_components, publications = [], [], [], [], [], []
    issuer_values, fund_values, security_values = {}, {}, {}
    position_amounts: list[tuple[str, str, str, str, Decimal]] = []
    cash_amounts: list[tuple[str, Decimal]] = []

    def flag(
        code: str,
        subject: str,
        *,
        reason: str | None = None,
        block: bool = False,
        severity: str = "warning",
        detail: dict | None = None,
    ) -> None:
        item: dict = {"code": code, "subject": subject, "severity": severity}
        if reason:
            item["reason"] = reason
        if detail is not None:
            item["detail"] = detail
        flags.append(item)
        if block:
            blocking.append(item)
            missing.add(subject)

    def use(item: Observation) -> None:
        evidence.append(item.evidence())

    household = views["household"].values.get(household_id)
    if household_id in views["household"].ambiguous:
        flag("AMBIGUOUS_ENTITY", household_id, reason="HOUSEHOLD", block=True)
    elif household is None:
        raise LookupError(f"Household unavailable at cutoffs: {household_id}")
    else:
        use(household)
    external_people: dict[str, set[str]] = {}
    people = views["person"]
    for item in [
        *people.values.values(),
        *(item for group in people.ambiguous.values() for item in group),
    ]:
        external_people.setdefault(item.row["external_person_key"], set()).add(item.row["id"])
    duplicates = {key for group in external_people.values() if len(group) > 1 for key in group}
    for person, assertions in views["membership"].ambiguous.items():
        if any(item.row["household_id"] == household_id for item in assertions):
            flag("AMBIGUOUS_ENTITY", person, reason="MEMBERSHIP", block=True)
    for person, member in views["membership"].values.items():
        if member.row["household_id"] != household_id:
            continue
        use(member)
        if person in people.ambiguous or person in duplicates:
            flag("AMBIGUOUS_ENTITY", person, reason="PERSON", block=True)
        elif person not in people.values:
            flag("SOURCE_UNAVAILABLE", person, reason="PERSON", block=True)
        else:
            use(people.values[person])

    def fx(currency: str) -> tuple[Decimal | None, dict | None]:
        if currency == "USD":
            return Decimal("1"), {
                "currency": "USD",
                "usd_rate": "1",
                "rate_at": None,
                "source": "USD_IDENTITY",
            }
        fix, _reason = state.fx(currency, day)
        if fix is None:
            return None, None
        rate_at = instant(fix.row["rate_at"])
        if rate_at.date() != day or rate_at > known:
            return None, None
        use(fix)
        return number(fix.row["usd_rate"]), {
            "currency": currency,
            "usd_rate": fix.row["usd_rate"],
            "rate_at": rate_at.isoformat(),
            "evidence": fix.evidence(),
        }

    reporting = views["reporting"]
    for account, assertions in reporting.ambiguous.items():
        if any(item.row["household_id"] == household_id for item in assertions):
            flag("AMBIGUOUS_ENTITY", account, reason="REPORTING_HOUSEHOLD", block=True)
            missing_accounts.add(account)
    for account, assignment in sorted(reporting.values.items()):
        if assignment.row["household_id"] != household_id:
            continue
        use(assignment)
        account_record = views["account"].values.get(account)
        if account in views["account"].ambiguous or account_record is None:
            flag(
                "AMBIGUOUS_ENTITY"
                if account in views["account"].ambiguous
                else "SOURCE_UNAVAILABLE",
                account,
                reason="ACCOUNT",
                block=True,
            )
            missing_accounts.add(account)
            continue
        use(account_record)
        if not account_record.row["is_managed"]:
            excluded.append(account)
            flag("HELD_AWAY_EXCLUDED", account, severity="info")
            continue
        included.append(account)
        pas, reason = state.snapshot("pas_snapshots", account, day)
        custody, secondary_reason = state.snapshot("custody_snapshots", account, day)
        if pas is not None:
            evidence.append(pas.receipt.evidence())
            publications.append(
                {
                    "account_id": account,
                    "custodian": pas.external[0],
                    "account_number": pas.external[1],
                    "available": pas.available,
                    "as_observed_manifest_account_id": pas.upstream_account_id,
                    "evidence": pas.receipt.evidence(),
                }
            )
        if custody is not None:
            evidence.append(custody.receipt.evidence())
        if custody is None or not custody.available:
            flag("RECONCILIATION_UNAVAILABLE", account, reason=secondary_reason)
        if pas is None or not pas.available:
            flag("SOURCE_UNAVAILABLE", account, reason=reason, block=True)
            missing_accounts.add(account)
            continue
        ownership = views["ownership"].values.get(account)
        if ownership is not None:
            use(ownership)
            if ownership.row["status"] == "CLOSED" and any(
                row.row["kind"] in {"position", "cash"}
                and number(
                    row.row["quantity"] if row.row["kind"] == "position" else row.row["amount"]
                )
                > 0
                for row in pas.records
            ):
                flag("CLOSED_ACCOUNT_WITH_BALANCE", account)
        for observation in pas.records:
            row = observation.row
            use(observation)
            if row["kind"] == "pas_grouping":
                if row["household_id"] != household_id:
                    flag("REPORTING_HOUSEHOLD_CONFLICT", account, reason="CRM_AUTHORITATIVE")
                continue
            if row["kind"] == "cash":
                subject = f"{account}:CASH:{row['currency']}"
                rate, fix = fx(row["currency"])
                if rate is None:
                    flag("FX_UNAVAILABLE", subject, block=True)
                    continue
                value = number(row["amount"]) * rate
                cash_amounts.append((subject, value))
                cash_components.append(
                    {
                        "account_id": account,
                        "currency": row["currency"],
                        "amount_local": row["amount"],
                        "value_usd": _money(value),
                        "fx": fix,
                        "evidence": observation.evidence(),
                    }
                )
                continue
            security = row["security_id"]
            subject = f"{account}:{security}"
            instrument = views["security"].values.get(security)
            if instrument is None:
                ambiguous = views["security"].ambiguous.get(security, [])
                for competing in ambiguous:
                    use(competing)
                flag("AMBIGUOUS" if ambiguous else "SOURCE_UNAVAILABLE", subject,
                     reason="SECURITY", block=True)
            else:
                use(instrument)
            # These are independent authoritative inputs. A missing security
            # definition must not hide a concurrent price ambiguity or absence.
            price, price_reason = state.price(security, day)
            if price is None:
                flag(
                    "AMBIGUOUS" if price_reason == "AMBIGUOUS_PRICE" else "SOURCE_UNAVAILABLE",
                    subject,
                    reason=price_reason,
                    block=True,
                )
            else:
                use(price)
            if instrument is None or price is None:
                continue
            definition = instrument.row
            if price.row["currency"] != definition["currency"]:
                flag("SOURCE_UNAVAILABLE", subject, reason="PRICE_CURRENCY", block=True)
                continue
            pas_price = row.get("pas_price")
            if pas_price is not None:
                reasons = []
                if pas_price["currency"] != price.row["currency"]:
                    reasons.append("CURRENCY")
                if business_date(pas_price["price_date"]) != business_date(price.row["price_date"]):
                    reasons.append("CLOSE_DATE")
                if not reasons and number(pas_price["price"]) != number(price.row["price"]):
                    reasons.append("PRICE")
                if reasons:
                    flag(
                        "PRICE_SOURCE_MISMATCH",
                        subject,
                        severity="info",
                        detail={
                            "reasons": reasons,
                            "authoritative_source": "CUSTODY",
                            "pas_price": {**pas_price, "price_date":
                                          business_date(pas_price["price_date"]).isoformat()},
                            "custody_price": {
                                **{key: price.row[key] for key in ("price", "currency")},
                                "price_date": business_date(price.row["price_date"]).isoformat(),
                            },
                            "pas_evidence": observation.evidence(),
                            "custody_evidence": price.evidence(),
                        },
                    )
            age = _age(business_date(price.row["price_date"]), day)
            tolerance = 0 if definition["listing_country"] == "US" else 1
            rate, fix = fx(definition["currency"])
            value = (
                None
                if rate is None
                else number(row["quantity"]) * number(price.row["price"]) * rate
            )
            if rate is None:
                flag("FX_UNAVAILABLE", subject, block=True)
            if age > tolerance:
                # Stale is separately enumerated, not duplicated as missing input.
                flag("PRICE_STALE", subject)
                blocking.append(flags[-1])
                stale.append(
                    {
                        "subject": subject,
                        "account_id": account,
                        "security_id": security,
                        "price_date": business_date(price.row["price_date"]).isoformat(),
                        "age_business_days": age,
                        "last_known_value_usd": _money(value) if value is not None else None,
                    }
                )
                continue
            if value is None:
                continue
            position_amounts.append(
                (subject, security, definition["type"], definition["issuer_id"], value)
            )
            positions.append(
                {
                    "account_id": account,
                    "security_id": security,
                    "quantity": row["quantity"],
                    "price": price.row["price"],
                    "price_date": business_date(price.row["price_date"]).isoformat(),
                    "price_age_business_days": age,
                    "market_value_usd": _money(value),
                    "fx": fix,
                    "evidence": observation.evidence(),
                }
            )
    # Metric stages are explicit because finite-precision addition is not
    # associative. Never derive totals from the output-rounded component strings.
    position_total = cash_total = Decimal("0")
    for _subject, security, kind, issuer, value in sorted(position_amounts):
        position_total += value
        security_values[security] = security_values.get(security, Decimal("0")) + value
        if kind == "COMMON_STOCK":
            issuer_values[issuer] = issuer_values.get(issuer, Decimal("0")) + value
        else:
            fund_values[security] = fund_values.get(security, Decimal("0")) + value
    for _subject, value in sorted(cash_amounts):
        cash_total += value
    total = position_total + cash_total
    complete = not blocking
    result = {
        "household_id": household_id,
        "status": "COMPLETE" if complete else "INCOMPLETE",
        "valuation_date": day.isoformat(),
        "known_as_of": known.isoformat(),
        "included_account_ids": included,
        "excluded_account_ids": excluded,
        "pas_publications": publications,
        "positions": sorted(positions, key=lambda item: (item["account_id"], item["security_id"])),
        "cash_components": sorted(
            cash_components, key=lambda item: (item["account_id"], item["currency"])
        ),
        "quality_flags": _unique(flags),
        "blocking_flags": _unique(blocking),
        "stale_components": sorted(stale, key=lambda item: item["subject"]),
        "missing_components": sorted(missing),
        "missing_account_ids": sorted(missing_accounts),
        "evidence": _unique(evidence),
    }
    breaches = []
    if complete:
        result.update(assets_usd=_money(total), cash_usd=_money(cash_total))
        issuer_items = _rank(
            issuer_values, total, field="issuer_id", percentage_field="concentration_pct"
        )
        for label, field, scope, items in (
            ("issuer_exposures", "issuer_id", "household_issuers", issuer_items),
            (
                "fund_vehicle_exposures",
                "security_id",
                "household_fund_vehicles",
                _rank(fund_values, total, field="security_id", percentage_field="allocation_pct"),
            ),
            (
                "security_exposures",
                "security_id",
                "household_securities",
                _rank(
                    security_values, total, field="security_id", percentage_field="allocation_pct"
                ),
            ),
        ):
            result[label] = _list_output(
                items,
                rank_scope=scope,
                ordering=["unrounded_market_value_usd DESC", f"{field} ASC"],
            )
        if total > 0:
            # Compare the full issuer population, independently of display caps.
            for item in issuer_items:
                value = issuer_values[item["issuer_id"]]
                concentration = value / total * 100
                if concentration >= threshold:
                    breaches.append(
                        (
                            concentration,
                            {
                                "household_id": household_id,
                                **{key: field for key, field in item.items() if key != "rank"},
                                "issuer_rank": item["rank"],
                                "applied_threshold_pct": _money(threshold),
                            },
                        )
                    )
    else:
        result.update(fresh_subtotal_usd=_money(total), fresh_cash_subtotal_usd=_money(cash_total))
    indeterminate = None
    if not complete or total <= 0:
        indeterminate = {
            "household_id": household_id,
            "reason": "INCOMPLETE_ASSETS" if not complete else "NONPOSITIVE_ASSETS",
            "fresh_subtotal_usd": _money(total),
            "stale_component_count": len(stale),
            "missing_account_ids": sorted(missing_accounts),
            "blocking_flags": _unique(blocking),
        }
    return result, breaches, indeterminate


def evaluate(
    history: History,
    *,
    household_ids: list[str],
    as_of: date,
    known_as_of: datetime,
    threshold_pct: Decimal = Decimal("10.00"),
) -> dict:
    """Evaluate explicitly supplied household scope; never authorize that scope.

    The only timestamps used are the request cutoff and exported receipt/source
    times. Host wall time and bootstrap time are not inputs to this package.
    """
    day, known = valuation_date(as_of), instant(known_as_of)
    exponent = threshold_pct.as_tuple().exponent if isinstance(threshold_pct, Decimal) else None
    if (
        not isinstance(threshold_pct, Decimal)
        or not threshold_pct.is_finite()
        or not Decimal("1") <= threshold_pct <= Decimal("50")
        or not isinstance(exponent, int)
        or exponent < -2
    ):
        raise ValueError("threshold_pct must be Decimal in [1.00, 50.00], at most two decimals")
    if not isinstance(household_ids, list) or any(
        not isinstance(value, str) for value in household_ids
    ):
        raise EvidenceError("household_ids must be explicit string identifiers")
    state = State(history, known)
    households, breaches, indeterminate = [], [], []
    with localcontext() as arithmetic:
        arithmetic.prec, arithmetic.rounding = 28, ROUND_HALF_EVEN
        for household in sorted(set(household_ids)):
            facts, found, unavailable = _household(state, household, day, known, threshold_pct)
            households.append(facts)
            breaches.extend(found)
            if unavailable:
                indeterminate.append(unavailable)
    ordered_breaches = sorted(
        breaches,
        key=lambda pair: (
            pair[0].copy_negate(),
            pair[1]["household_id"],
            pair[1]["issuer_id"],
        ),
    )
    breach_page = _list_output(
        [{**item, "book_rank": rank} for rank, (_ratio, item) in enumerate(ordered_breaches, 1)],
        rank_scope="book_concentration_breaches",
        ordering=["unrounded_concentration_pct DESC", "household_id ASC", "issuer_id ASC"],
    )
    indeterminate_page = _list_output(
        indeterminate, rank_scope=None, ordering=["household_id ASC"], limit=None
    )
    return {
        "oracle_version": "received-finance-1",
        "valuation_date": day.isoformat(),
        "known_as_of": known.isoformat(),
        "applied_threshold_pct": _money(threshold_pct),
        "comparator": ">=",
        "households": households,
        "item_count": len(households),
        "evaluated_household_count": len(households),
        "determinate_household_count": len(households) - len(indeterminate),
        "indeterminate_household_count": len(indeterminate),
        "breaching_household_count": len(
            {item["household_id"] for _ratio, item in ordered_breaches}
        ),
        "breaches": breach_page.pop("items"),
        "breach_metadata": breach_page,
        "indeterminate": indeterminate,
        "indeterminate_metadata": {
            key: value for key, value in indeterminate_page.items() if key != "items"
        },
    }
