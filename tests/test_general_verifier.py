"""Claims are references to received values, never model-provided arithmetic."""

import copy

import pytest

from wealthcontext.contracts import Answer, Caveat, Claim
from wealthcontext.verifier import InvalidAnswer, deterministic_fallback, verify_answer

HH = "WC-HH-0001"
ACCOUNT = "WC-AC-0001"
SECURITY = "WC-SEC-0001"
POSITION = f"{ACCOUNT}:{SECURITY}"
ISSUER = "WC-ISS-0001"
DAY = "2026-09-30"


def envelope(data, *, result_id="r_one", status="COMPLETE", flags=None):
    return {"result_id": result_id, "trace_id": "trace_one", "status": status,
            "valuation_date": DAY, "as_of": DAY, "known_as_of": "2026-10-01T12:00:00Z",
            "calculated_at": "2026-11-01T12:00:00Z", "quality_flags": flags or [],
            "source_refs": ["h_one"], "metric_refs": ["HOUSEHOLD_ASSETS@1"],
            "error": None, "data": data}


def snapshot(*, incomplete=False):
    data = {"household_id": HH, "household_name": "Example household", "item_count": 1,
            "accounts": [{"id": ACCOUNT, "title": "Example account"}],
            "positions": [{"id": POSITION, "account_id": ACCOUNT, "security_id": SECURITY,
                           "quantity": "100", "price_usd": "25.00", "market_value_usd": "2500.00",
                           "price_local": "25.00", "listing_currency": "USD", "fx_rate": "1",
                           "price_date": DAY, "price_age_business_days": 0}],
            "cash_balances": [{"id": f"{ACCOUNT}:USD", "account_id": ACCOUNT, "currency": "USD",
                               "amount_local": "500.00", "market_value_usd": "500.00",
                               "fx_rate": "1"}],
            "reconciliation": [{"account_id": ACCOUNT, "rows": [{
                "subject": POSITION, "kind": "quantity", "security_id": SECURITY,
                "authoritative": "100", "settled": "110", "pending_delta": "-10",
                "difference": "-10", "reconciliation_status": "PENDING_SETTLEMENT",
            }, {"subject": f"{ACCOUNT}:USD", "kind": "cash", "currency": "USD",
                "authoritative": "500.00", "settled": "250.00", "pending_delta": "250.00",
                "difference": "250.00", "reconciliation_status": "PENDING_SETTLEMENT"}]}]}
    if incomplete:
        data["fresh_subtotal_usd"] = "500.00"
        data["missing_components"] = [POSITION]
        return envelope(data, status="INCOMPLETE", flags=[{
            "code": "PRICE_STALE", "subject": POSITION, "severity": "warning",
            "detail": {"price_date": DAY, "age_business_days": 3},
        }])
    return envelope({**data, "assets_usd": "3000.00", "cash_usd": "500.00"})


def exposure():
    return envelope({"household_id": HH, "household_name": "Example household",
                     "assets_usd": "10000.00", "group_by": "issuer", "issuer_exposures": {
                         "rank_scope": "household_issuers", "item_count": 1, "total_item_count": 1,
                         "items": [{"entity_type": "issuer", "entity_id": ISSUER,
                                    "label": "Example issuer", "exposure_usd": "999.96",
                                    "concentration_pct": "10.00", "rank": 1}],
                     }, "fund_vehicle_exposures": {
                         "rank_scope": "household_fund_vehicles", "item_count": 1,
                         "total_item_count": 1, "items": [{"entity_type": "security",
                             "entity_id": SECURITY, "label": "Sweep vehicle", "rank": 1,
                             "exposure_usd": "9000.04", "allocation_pct": "90.00"}],
                     }})


def concentration(*, decision=True, second_household=False):
    rows = [{"household_id": HH, "issuer_id": ISSUER, "label": "Example issuer",
             "exposure_usd": "1000.00", "concentration_pct": "10.00", "assets_usd": "10000.00",
             "meets_threshold": decision, "book_rank": 1, "issuer_rank": 1}]
    if second_household:
        rows.append({**rows[0], "household_id": "WC-HH-0002", "book_rank": 2})
    return envelope({"applied_threshold_pct": "10.00", "evaluated_household_count": 2,
                     "determinate_household_count": 2, "indeterminate_household_count": 0,
                     "breaching_household_count": len(rows), "breaches": rows, "indeterminate": []})


def claim(**changes):
    return {"id": "c1", "result_id": "r_one", "entity": {"type": "household", "id": HH},
            "field": "assets_usd", "household_id": HH, "valuation_date": DAY,
            "unit": "USD", "format": "value_only", **changes}


def predicate(kind, expect, *, entity=None, field=None, **changes):
    fields = {"rank": "rank", "threshold": "applied_threshold_pct", "completeness": "status",
              "reconciliation": "reconciliation_status"}
    return {"id": "p1", "result_id": "r_one", "kind": kind, "expect": expect,
            "entity": entity or {"type": "result_summary", "id": "r_one"},
            "field": field or fields[kind], **changes}


def answer(markdown="Household assets: {{c1}}.", *, claims=None, predicates=None,
           caveats=None, refusal=None):
    return Answer.model_validate({"answer_markdown": markdown,
                                  "claims": [claim()] if claims is None else claims,
                                  "predicates": predicates or [], "caveats": caveats or [],
                                  "refusal": refusal})


def verify(candidate, result):
    return verify_answer(candidate, {result["result_id"]: result})


def diagnostic_codes(candidate, result):
    with pytest.raises(InvalidAnswer) as caught:
        verify(candidate, result)
    assert all(set(item) == {"code", "path", "message"} for item in caught.value.diagnostics)
    return {item["code"] for item in caught.value.diagnostics}


def test_server_resolves_exact_values_labels_scopes_and_source_callouts():
    result = snapshot()
    rendered = verify(answer(), result)
    assert "Household assets: USD 3000.00." in rendered["markdown"]
    assert rendered["claims"][0]["value"] == "3000.00"
    assert rendered["claims"][0]["known_as_of"] == result["known_as_of"]
    assert rendered["claims"][0]["metric_refs"] == ["HOUSEHOLD_ASSETS@1"]
    assert rendered["sources"][0]["result_id"] == "r_one"
    assert f"valuation date {DAY}" in rendered["markdown"]
    assert result == snapshot()


@pytest.mark.parametrize("prose", [
    "The account holds no cash or positions.",
    "No quality flags were recorded.",
])
def test_literal_negative_quantity_requires_verified_evidence(prose):
    assert "UNVERIFIED_TOKEN" in diagnostic_codes(
        answer("Household assets: {{c1}}. " + prose), snapshot(),
    )


@pytest.mark.parametrize("changes,code", [
    ({"entity": {"type": "account", "id": HH}}, "UNRESOLVED_REFERENCE"),
    ({"household_id": "WC-HH-0002"}, "UNRESOLVED_REFERENCE"),
    ({"household_id": None}, "UNRESOLVED_REFERENCE"),
    ({"valuation_date": "2026-10-01"}, "VALUATION_DATE_MISMATCH"),
    ({"unit": "pct"}, "UNIT_MISMATCH"),
    ({"field": "invented_total"}, "UNRESOLVED_REFERENCE"),
    ({"field": "valuation_date", "unit": "USD"}, "UNRESOLVED_REFERENCE"),
    ({"result_id": "r_elsewhere"}, "RESULT_UNAVAILABLE"),
])
def test_entity_field_household_date_unit_and_result_must_match(changes, code):
    assert code in diagnostic_codes(answer(claims=[claim(**changes)]), snapshot())


def test_position_inputs_and_cash_reconciliation_are_direct_scoped_values():
    specs = [("quantity", "shares"), ("price_usd", "USD"), ("fx_rate", "fx_rate"),
             ("market_value_usd", "USD"), ("price_age_business_days", "days"),
             ("pending_delta", "shares")]
    claims = [claim(id=f"c{index}", entity={"type": "position", "id": POSITION},
                    field=field, unit=unit) for index, (field, unit) in enumerate(specs, 1)]
    claims.append(claim(id="c7", entity={"type": "account", "id": ACCOUNT},
                        field="cash.USD.pending_delta"))
    candidate = answer("Inputs: {{c1}}, {{c2}}, {{c3}}, {{c4}}, {{c5}}, {{c6}}, {{c7}}.",
                       claims=claims)
    assert [item["value"] for item in verify(candidate, snapshot())["claims"]] == [
        "100", "25.00", "1", "2500.00", 0, "-10", "250.00",
    ]


def test_foreign_local_price_cannot_be_labelled_usd():
    result = snapshot()
    result["data"]["positions"][0]["listing_currency"] = "CAD"
    candidate = answer(claims=[claim(entity={"type": "position", "id": POSITION},
                                     field="price_local")])
    assert "UNRESOLVED_REFERENCE" in diagnostic_codes(candidate, result)


def test_incomplete_result_withholds_total_and_requires_warning_callouts():
    result = snapshot(incomplete=True)
    assert "UNRESOLVED_REFERENCE" in diagnostic_codes(answer(), result)
    candidate = answer("Fresh subtotal: {{c1}}. Result status: {{p1}}.",
                       claims=[claim(field="fresh_subtotal_usd")],
                       predicates=[predicate("completeness", "INCOMPLETE")])
    assert "MISSING_CAVEAT" in diagnostic_codes(candidate, result)
    candidate.caveats = [Caveat(flag="PRICE_STALE", subject=POSITION)]
    rendered = verify(candidate, result)
    assert "incomplete" in rendered["markdown"]
    assert f"Warning: PRICE\\_STALE — {POSITION}" in rendered["markdown"]
    assert rendered["warnings"][0]["detail"]["age_business_days"] == 3


def test_rank_is_household_list_specific_and_fund_never_becomes_an_issuer():
    candidate = answer("Exposure: {{c1}}; {{p1}}.", claims=[claim(
        entity={"type": "issuer", "id": ISSUER}, field="exposure_usd", format="label_and_value",
    )], predicates=[predicate("rank", 1, entity={"type": "issuer", "id": ISSUER})])
    rendered = verify(candidate, exposure())
    assert "Example issuer: USD 999.96" in rendered["markdown"]
    assert rendered["predicates"][0]["rank_scope"] == "household_issuers"
    assert "largest among household issuers" in rendered["markdown"]
    wrong = answer(claims=[claim(entity={"type": "issuer", "id": SECURITY}, field="exposure_usd")])
    assert "UNRESOLVED_REFERENCE" in diagnostic_codes(wrong, exposure())
    fund = answer(claims=[claim(entity={"type": "security", "id": SECURITY},
                               field="allocation_pct", unit="pct")])
    assert verify(fund, exposure())["claims"][0]["value"] == "90.00"


@pytest.mark.parametrize("decision,expect", [(True, "AT_OR_ABOVE"), (False, "BELOW")])
def test_threshold_uses_explicit_unrounded_engine_decision_not_display(decision, expect):
    # Both branches deliberately display 10.00%; only engine evidence decides.
    candidate = answer("Exposure is {{p1}}.", claims=[], predicates=[predicate(
        "threshold", expect, entity={"type": "issuer", "id": ISSUER},
    )])
    assert verify(candidate, concentration(decision=decision))["predicates"][0]["value"] is decision
    assert "PREDICATE_MISMATCH" in diagnostic_codes(candidate, concentration(decision=not decision))


def test_threshold_absence_and_rounded_exposure_never_establish_below():
    candidate = answer("Exposure is {{p1}}.", claims=[], predicates=[predicate(
        "threshold", "BELOW", entity={"type": "issuer", "id": ISSUER},
    )])
    assert "UNRESOLVED_REFERENCE" in diagnostic_codes(candidate, exposure())
    result = concentration()
    result["data"]["breaches"] = []
    assert "UNRESOLVED_REFERENCE" in diagnostic_codes(candidate, result)


def test_book_issuer_predicate_ambiguity_fails_but_claim_has_explicit_household():
    result = concentration(second_household=True)
    candidate = answer("Exposure is {{p1}}.", claims=[], predicates=[predicate(
        "threshold", "AT_OR_ABOVE", entity={"type": "issuer", "id": ISSUER},
    )])
    assert "AMBIGUOUS_REFERENCE" in diagnostic_codes(candidate, result)
    scoped = answer(claims=[claim(entity={"type": "issuer", "id": ISSUER}, field="exposure_usd")])
    assert verify(scoped, result)["claims"][0]["household_id"] == HH
    rank = answer("Exposure is {{p1}}.", claims=[], predicates=[predicate(
        "rank", 1, entity={"type": "issuer", "id": ISSUER},
    )])
    assert "UNRESOLVED_REFERENCE" in diagnostic_codes(rank, result)


@pytest.mark.parametrize("expected", ["MATCHED", "BREAK", "RECONCILIATION_UNAVAILABLE"])
def test_reconciliation_predicate_must_equal_explicit_row_status(expected):
    candidate = answer("Reconciliation is {{p1}}.", claims=[], predicates=[predicate(
        "reconciliation", expected, entity={"type": "position", "id": POSITION},
    )])
    assert "PREDICATE_MISMATCH" in diagnostic_codes(candidate, snapshot())


def test_pending_predicate_is_server_wording_and_info_flag_is_optional():
    result = snapshot()
    result["quality_flags"] = [{"code": "PENDING_SETTLEMENT", "subject": POSITION,
                                "severity": "info", "detail": {}}]
    candidate = answer("Reconciliation is {{p1}}.", claims=[], predicates=[predicate(
        "reconciliation", "PENDING_SETTLEMENT", entity={"type": "position", "id": POSITION},
    )])
    assert "pending settlement" in verify(candidate, result)["markdown"]


@pytest.mark.parametrize("text,code", [
    ("{{c1}} and {{c1}}", "DUPLICATE_PLACEHOLDER"),
    ("Value unavailable", "UNUSED_REFERENCE"),
    ("{{c1}} {{c2}}", "UNKNOWN_PLACEHOLDER"),
    ("{{ c1 }}", "MALFORMED_PLACEHOLDER"),
    ("{{c1}} {{c0}}", "MALFORMED_PLACEHOLDER"),
    ("{{c1}} is 9999 dollars", "UNVERIFIED_NUMBER"),
    ("{{c1}} exceeds the 10% threshold", "UNVERIFIED_NUMBER"),
    ("{{c1}} for 2026-10-15", "UNVERIFIED_NUMBER"),
    ("{{c1}} from HOUSEHOLD_ASSETS@2", "UNVERIFIED_NUMBER"),
    ("{{c1}} is largest", "UNVERIFIED_TOKEN"),
    ("{{c1}} is twenty-one percent", "UNVERIFIED_TOKEN"),
    ("{{c1}} covers all accounts", "UNVERIFIED_TOKEN"),
    ("{{c1}} covers both accounts", "UNVERIFIED_TOKEN"),
    ("{{c1}} covers two accounts", "UNVERIFIED_TOKEN"),
    ("{{c1}} is １０ percent", "UNVERIFIED_NUMBER"),
    ("{{c1}} is la\u200brgest", "UNVERIFIED_TOKEN"),
])
def test_unexpanded_markdown_rejects_uncited_literals_and_lint_tokens(text, code):
    assert code in diagnostic_codes(answer(text), snapshot())


@pytest.mark.parametrize("text,code", [
    ("{{c1}} has th**ree** accounts", "UNVERIFIED_TOKEN"),
    ("{{c1}} is lar**gest**", "UNVERIFIED_TOKEN"),
    ("{{c1}} covers al**l** accounts", "UNVERIFIED_TOKEN"),
    ("{{c1}} is lar`gest`", "UNVERIFIED_TOKEN"),
    ("{{c1}} is lar~~g~~est", "UNVERIFIED_TOKEN"),
    ("{{c1}} is lar_gest_", "UNVERIFIED_TOKEN"),
    ("{{c1}} is lar<b>gest</b>", "UNVERIFIED_TOKEN"),
    ("{{c1}} is lar&#103;est", "UNVERIFIED_TOKEN"),
    ("{{c1}} is t**w**o accounts", "UNVERIFIED_TOKEN"),
    ("{{c1}} is 1**0** percent", "UNVERIFIED_NUMBER"),
    ("{{c1}} is &#49;&#48; percent", "UNVERIFIED_NUMBER"),
])
def test_formatting_inside_a_word_does_not_hide_lint_tokens_or_numbers(text, code):
    # SOL-P5-R1-VER-001 / CHAIR-C-VER-BOLD-001: the workbench renders ** and ` as
    # formatting, so "lar**gest**" displays "largest" and must be linted as such.
    assert code in diagnostic_codes(answer(text), snapshot())


@pytest.mark.parametrize("text", [
    "**Assets:** {{c1}}.", "`Assets`: {{c1}}.", "Assets: **{{c1}}**.",
    "*Household assets*: {{c1}}.", "Household <assets>: {{c1}}.",
])
def test_formatting_around_neutral_labels_and_placeholders_stays_valid(text):
    assert verify(answer(text), snapshot())["mode"] == "verified"


@pytest.mark.parametrize("text", [
    "Reconciliation is pending settlement.", "Status: pending\nsettlement.",
    "A reconciliation break exists.", "Reconciliation breaks were recorded.",
    "The reconciliation is broken.", "Reconciliation is unavailable.",
    "Reconciliation status unavailable.", "Account is unavailable for reconciliation.",
    "Status: PENDING_SETTLEMENT.", "Status: BREAK.", "Status: RECONCILIATION_UNAVAILABLE.",
    "Reconciliation is pending **settlement**.",
])
@pytest.mark.parametrize("with_evidence", [False, True])
def test_reconciliation_status_prose_requires_a_predicate(text, with_evidence):
    # SOL-P5-R1-VER-002: status wording outside a predicate placeholder is rejected,
    # with no evidence and with a cited result whose rows record a different status.
    candidate = answer(text.rstrip(".") + " {{c1}}.") if with_evidence else answer(
        text, claims=[])
    results = {"r_one": snapshot()} if with_evidence else {}
    with pytest.raises(InvalidAnswer) as caught:
        verify_answer(candidate, results)
    assert "UNVERIFIED_TOKEN" in {item["code"] for item in caught.value.diagnostics}


@pytest.mark.parametrize("status,wording", [
    ("MATCHED", "matched"), ("PENDING_SETTLEMENT", "pending settlement"),
    ("BREAK", "a reconciliation break"),
    ("RECONCILIATION_UNAVAILABLE", "reconciliation unavailable"),
])
def test_each_reconciliation_status_is_rendered_from_its_predicate(status, wording):
    result = snapshot()
    result["data"]["reconciliation"][0]["rows"][0]["reconciliation_status"] = status
    candidate = answer("Reconciliation: {{p1}}.", claims=[], predicates=[predicate(
        "reconciliation", status, entity={"type": "position", "id": POSITION},
    )])
    assert verify(candidate, result)["markdown"].startswith(f"Reconciliation: {wording}.")


def test_neutral_unavailable_fallback_and_refusal_wording_stay_valid():
    error = {"result_id": "r_err", "trace_id": "t_err", "status": "ERROR",
             "error": {"code": "NOT_FOUND"}, "quality_flags": [], "source_refs": []}
    refusal = deterministic_fallback({"r_err": error})
    assert verify_answer(refusal, {"r_err": error})["refusal"]["message"] == (
        "The requested data is unavailable.")
    assert verify_answer(answer("Verified data is unavailable.", claims=[]), {})["mode"] == (
        "verified")


def test_only_actual_ids_dates_metrics_and_list_markers_are_numeric_exemptions():
    candidate = answer(f"1. Household {HH}, {DAY}, HOUSEHOLD_ASSETS@1: {{{{c1}}}}.")
    assert verify(candidate, snapshot())["claims"][0]["value"] == "3000.00"
    unknown = answer("Household WC-HH-9999: {{c1}}.")
    assert "UNVERIFIED_NUMBER" in diagnostic_codes(unknown, snapshot())


@pytest.mark.parametrize("with_position", [True, False])
def test_returned_security_reference_is_literal_identifier_even_without_security_binding(
    with_position,
):
    result = snapshot()
    if not with_position:
        result["data"]["positions"] = []  # Reconciliation still references the security.
    candidate = answer(f"Household {HH}; security {SECURITY}: {{{{c1}}}}.")
    assert verify(candidate, result)["claims"][0]["value"] == "3000.00"
    # Allowing a descriptive ID does not create an entity/field binding.
    unsupported = answer(claims=[claim(entity={"type": "security", "id": SECURITY})])
    assert "UNRESOLVED_REFERENCE" in diagnostic_codes(unsupported, result)


@pytest.mark.parametrize("literal", [
    "0001", "WC-SEC-9999", "WC-SEC-0001-extra", "prefix-WC-SEC-0001",
    "100", "2500.00", "WC-SEC-8888",
])
def test_security_identifier_exception_rejects_fragments_values_and_untrusted_nested_ids(literal):
    result = snapshot()
    result["data"]["positions"][0]["note"] = {"security_id": "WC-SEC-8888"}
    assert "UNVERIFIED_NUMBER" in diagnostic_codes(answer(f"{literal}: {{{{c1}}}}."), result)


def test_security_identifier_in_uncited_result_is_not_a_literal_exemption():
    other = snapshot()
    other["result_id"] = "r_uncited"
    other["data"]["positions"][0]["security_id"] = "WC-SEC-7777"
    candidate = answer("Security WC-SEC-7777: {{c1}}.")
    with pytest.raises(InvalidAnswer) as caught:
        verify_answer(candidate, {"r_one": snapshot(), "r_uncited": other})
    assert "UNVERIFIED_NUMBER" in {item["code"] for item in caught.value.diagnostics}


def test_matching_row_statuses_never_create_an_account_wide_reconciliation_predicate():
    result = snapshot()
    candidate = answer("Reconciliation: {{p1}}.", claims=[], predicates=[predicate(
        "reconciliation", "PENDING_SETTLEMENT", entity={"type": "account", "id": ACCOUNT},
    )])
    assert "UNRESOLVED_REFERENCE" in diagnostic_codes(candidate, result)


def test_labels_with_numbers_or_lint_words_expand_after_verification_and_are_escaped():
    result = exposure()
    result["data"]["issuer_exposures"]["items"][0]["label"] = "One Top 123 [link](https://example)"
    candidate = answer(claims=[claim(entity={"type": "issuer", "id": ISSUER}, field="exposure_usd",
                                     format="label_and_value")])
    rendered = verify(candidate, result)
    assert "One Top 123 \\[link\\]\\(https://example\\)" in rendered["markdown"]


def test_duplicate_semantic_claims_and_duplicate_or_unfounded_caveats_are_rejected():
    candidate = answer("{{c1}} {{c2}}", claims=[claim(), claim(id="c2")])
    assert "DUPLICATE_REFERENCE" in diagnostic_codes(candidate, snapshot())
    warning = {"flag": "PRICE_STALE", "subject": POSITION}
    candidate = answer(claims=[claim(field="fresh_subtotal_usd")], caveats=[warning, warning])
    assert "DUPLICATE_CAVEAT" in diagnostic_codes(candidate, snapshot(incomplete=True))
    assert "UNKNOWN_CAVEAT" in diagnostic_codes(answer(caveats=[warning]), snapshot())


def test_every_cited_results_warnings_are_required_but_unreferenced_results_are_not():
    clean, stale = snapshot(), snapshot(incomplete=True)
    stale["result_id"] = "r_stale"
    results = {"r_one": clean, "r_stale": stale}
    assert verify_answer(answer(), results)["warnings"] == []
    candidate = answer("{{c1}} {{c2}}", claims=[claim(), claim(
        id="c2", result_id="r_stale", field="fresh_subtotal_usd",
    )])
    with pytest.raises(InvalidAnswer) as caught:
        verify_answer(candidate, results)
    assert "MISSING_CAVEAT" in {item["code"] for item in caught.value.diagnostics}


def test_refusal_requires_actual_error_and_uses_neutral_fixed_server_wording():
    result = envelope(None, status="ERROR")
    result["error"] = {"code": "NOT_AUTHORIZED", "message": "Caller is not authorized"}
    candidate = answer("The household does not exist.", claims=[],
                       refusal={"code": "NOT_AUTHORIZED", "result_id": "r_one"})
    assert verify(candidate, result)["markdown"].startswith("Access denied.")
    assert "REFUSAL_MISMATCH" in diagnostic_codes(candidate, snapshot())


def test_provenance_callouts_render_received_metadata_with_exact_record_hashes():
    result = envelope({"item_count": 1, "total_item_count": 1, "items": [{
        "source_record_id": "cash-001", "source_system": "PAS-A", "business_date": DAY,
        "payload_hash": "a" * 64, "payload_hash_scope": "stored_received_record",
        "payload": {"amount": "500.00"},
    }]})
    candidate = answer("Returned records: {{c1}}.", claims=[claim(
        entity={"type": "result_summary", "id": "r_one"}, field="item_count",
        household_id=None, unit="count",
    )])
    rendered = verify(candidate, result)
    record = rendered["sources"][0]["records"][0]
    assert record == {"source_record_id": "cash-001", "source_system": "PAS-A",
                      "business_date": DAY, "payload_hash": "a" * 64}
    assert "cash-001" in rendered["markdown"] and "a" * 64 in rendered["markdown"]
    assert "500.00" not in rendered["markdown"]


@pytest.mark.parametrize("result", [snapshot(), snapshot(incomplete=True), exposure(),
                                    concentration()])
def test_deterministic_fallback_is_an_answer_validated_by_the_same_verifier(result):
    before = copy.deepcopy(result)
    candidate = deterministic_fallback({"r_one": result})
    rendered = verify(candidate, result)
    assert rendered["predicates"][0]["value"] == result["status"]
    if result["status"] == "INCOMPLETE":
        assert not any(item["field"] == "assets_usd" for item in rendered["claims"])
        assert rendered["warnings"]
    assert result == before


def test_empty_fallback_does_not_invent_a_result_or_refusal():
    candidate = deterministic_fallback({})
    assert candidate.refusal is None and candidate.claims == []
    assert verify_answer(candidate, {})["markdown"] == "Verified data is unavailable."


@pytest.mark.parametrize("value", [True, 3000.0, "NaN", "Infinity", "3e3", "03000.00"])
def test_noncanonical_recorded_amounts_cannot_be_rendered_as_verified_values(value):
    result = snapshot()
    result["data"]["assets_usd"] = value
    assert "INVALID_RECORDED_VALUE" in diagnostic_codes(answer(), result)


def test_mutating_claim_list_cannot_bypass_duplicate_id_validation():
    candidate = answer()
    candidate.claims.append(Claim.model_validate(claim(field="cash_usd")))
    assert "DUPLICATE_REFERENCE_ID" in diagnostic_codes(candidate, snapshot())


def test_distinct_knowledge_cutoffs_are_preserved_for_same_entity_and_business_date():
    before, after = snapshot(), snapshot()
    after.update(result_id="r_after", known_as_of="2026-10-02T12:00:00Z")
    after["data"]["assets_usd"] = "3100.00"
    candidate = answer("Recorded views: {{c1}}; {{c2}}.",
                       claims=[claim(), claim(id="c2", result_id="r_after")])
    rendered = verify_answer(candidate, {"r_one": before, "r_after": after})
    assert [item["value"] for item in rendered["claims"]] == ["3000.00", "3100.00"]
    assert [item["known_as_of"] for item in rendered["claims"]] == [
        before["known_as_of"], after["known_as_of"],
    ]


def test_nested_metadata_counts_resolve_explicit_fields_not_returned_list_length():
    result = exposure()
    result["data"]["issuer_exposures"]["total_item_count"] = 25
    candidate = answer("Issuer count: {{c1}}.", claims=[claim(
        entity={"type": "result_summary", "id": "r_one"},
        field="issuer_exposures.total_item_count", unit="count",
    )])
    assert verify(candidate, result)["claims"][0]["value"] == 25


def test_source_callout_always_discloses_incomplete_without_model_status_prose():
    candidate = answer("Fresh subtotal: {{c1}}.", claims=[claim(field="fresh_subtotal_usd")],
                       caveats=[{"flag": "PRICE_STALE", "subject": POSITION}])
    rendered = verify(candidate, snapshot(incomplete=True))
    assert "status INCOMPLETE" in rendered["markdown"]
    assert rendered["sources"][0]["status"] == "INCOMPLETE"
