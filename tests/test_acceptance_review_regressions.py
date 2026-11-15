"""Public criteria must reject received-result counterexamples, not merely self-copy."""

from copy import deepcopy

import pytest
from test_acceptance_execution import ACCEPTANCE, resolve
from test_evaluation import trial

from wealthcontext.contracts import Answer
from wealthcontext.evaluation import grade_answer, load_cases, summarize_trials
from wealthcontext.evaluation_contracts import GovernedResult, TrialRecord
from wealthcontext.identity import Principal
from wealthcontext.verifier import verify_answer

pytest_plugins = ("test_acceptance_execution",)


async def actual_case(service, case_id, *, include_info=False):
    case = next(case for case in load_cases(ACCEPTANCE) if case.id == case_id)
    results, governed = {}, []
    for request in case.requests:
        arguments = {key: resolve(value, results) for key, value in request.arguments.items()}
        result = await service.execute(Principal(case.principal.id, case.principal.role),
                                       case.request_time, request.tool, arguments)
        results[request.alias] = result
        governed.append(GovernedResult(tool=request.tool, arguments=arguments, envelope=result))
    claims = [{"id": f"c{n}", "result_id": results[fact.request]["result_id"],
               "entity": {"type": fact.entity_type, "id": resolve(fact.entity_id, results)},
               "field": fact.field, "household_id": fact.household_id,
               "valuation_date": fact.valuation_date.isoformat(), "unit": fact.unit,
               "format": "label_and_value"} for n, fact in enumerate(case.required_facts, 1)]
    predicates = [{"id": f"p{n}", "result_id": results[item.request]["result_id"],
                   "entity": {"type": item.entity_type, "id": resolve(item.entity_id, results)},
                   "field": item.field, "kind": item.kind, "expect": item.expect}
                  for n, item in enumerate(case.required_predicates, 1)]
    if case.required_source_disclosure and not any(
        item["field"] == "total_item_count" for item in claims
    ):
        source = results["sources"]
        claims.append({"id": f"c{len(claims) + 1}", "result_id": source["result_id"],
                       "entity": {"type": "result_summary", "id": source["result_id"]},
                       "field": "total_item_count", "household_id": None,
                       "valuation_date": case.valuation_date.isoformat(), "unit": "count",
                       "format": "label_and_value"})
    cited = {item["result_id"] for item in [*claims, *predicates]}
    caveats = {(flag["code"], flag["subject"]) for result in results.values()
               if result["result_id"] in cited for flag in result["quality_flags"]
               if include_info or flag["severity"] == "warning"}
    candidate = Answer.model_validate({
        "answer_markdown": "Evidence: " + ", ".join("{{" + item["id"] + "}}"
                                                     for item in [*claims, *predicates]) + ". " +
                           " ".join(case.required_mentions),
        "claims": claims, "predicates": predicates,
        "caveats": [{"flag": flag, "subject": subject} for flag, subject in sorted(caveats)],
        "refusal": None,
    })
    rendered = verify_answer(candidate, {
        result["result_id"]: result for result in results.values()
    })
    return case, rendered, governed, results, candidate


@pytest.mark.anyio
async def test_q08_rejects_extra_genuinely_verified_foreign_household_disclosure(service):
    case, _, governed, results, candidate = await actual_case(service, "Q08")
    extra = await service.execute(Principal(case.principal.id, case.principal.role),
                                  case.request_time, "get_household_snapshot", {
                                      "household_id": "WC-HH-0001", "as_of": "2026-09-30",
                                      "known_as_of": "2026-10-31T16:00:00Z",
                                  })
    payload = candidate.model_dump(mode="json")
    payload["claims"].append({"id": "c2", "result_id": extra["result_id"],
                              "entity": {"type": "household", "id": "WC-HH-0001"},
                              "field": "assets_usd", "household_id": "WC-HH-0001",
                              "valuation_date": "2026-09-30", "unit": "USD",
                              "format": "label_and_value"})
    payload["answer_markdown"] += " {{c2}}."
    rendered = verify_answer(Answer.model_validate(payload), {
        **{item["result_id"]: item for item in results.values()}, extra["result_id"]: extra,
    })
    assert any(claim["value"] == "3000.00" for claim in rendered["claims"])
    governed.append(GovernedResult(tool="get_household_snapshot",
                                   arguments={"household_id": "WC-HH-0001"}, envelope=extra))
    rejected = grade_answer(case, rendered, governed)
    assert not rejected.passed
    assert any("scope" in item for item in rejected.diagnostics)


@pytest.mark.anyio
async def test_f19_rejects_an_extra_mmf_breach_with_all_existing_required_facts_retained(service):
    case, rendered, governed, _, _ = await actual_case(service, "F19", include_info=True)
    assert grade_answer(case, rendered, governed).passed
    wrong = deepcopy(governed)
    book = next(item.envelope for item in wrong if item.tool == "get_concentrated_positions")
    forged = deepcopy(book["data"]["breaches"][0])
    forged["issuer_id"] = "WC-ISS-0017"
    book["data"]["breaches"].append(forged)
    book["data"]["breach_metadata"]["item_count"] += 1
    book["data"]["breach_metadata"]["total_item_count"] += 1
    assert not grade_answer(case, rendered, wrong).passed
    merged = deepcopy(governed)
    exposure = next(item.envelope for item in merged if item.tool == "get_portfolio_exposure")
    stock = exposure["data"]["issuer_exposures"]["items"][0]
    stock["exposure_usd"], stock["concentration_pct"] = "250.00", "25.00"
    book = next(item.envelope for item in merged if item.tool == "get_concentrated_positions")
    book["data"]["breaches"][0]["exposure_usd"] = "250.00"
    book["data"]["breaches"][0]["concentration_pct"] = "25.00"
    assert not grade_answer(case, rendered, merged).passed


@pytest.mark.anyio
@pytest.mark.parametrize("case_id", ["F03", "F15", "F16", "F17", "F18", "F19"])
async def test_informational_received_flags_do_not_require_extra_model_warning_caveats(
    service, case_id,
):
    case, rendered, governed, _, _ = await actual_case(service, case_id)
    assert any(flag["severity"] == "info" for item in governed
               for flag in item.envelope["quality_flags"])
    grade = grade_answer(case, rendered, governed)
    assert grade.passed, grade.diagnostics


@pytest.mark.anyio
async def test_f07_unused_provenance_does_not_require_an_unrelated_source_disclosure(service):
    case, rendered, governed, _, _ = await actual_case(service, "F07")
    assert grade_answer(case, rendered, governed).passed
    extra = deepcopy(next(item for item in governed if item.tool == "get_data_provenance"))
    extra.arguments = {"reference": "different-unused-handle"}
    extra.envelope["result_id"] = "r_unused_provenance"
    assert grade_answer(case, rendered, [*governed, extra]).passed


def test_pre_provider_cases_remain_in_full_population_without_impossible_model_grades():
    cases = load_cases(ACCEPTANCE)
    records = []
    for case in cases:
        for number in (1, 2, 3):
            value = trial(case.id, number).model_dump(mode="json")
            if case.id in {"Q04", "Q07"}:
                value.update(attempts=[], actual_model_ids=[], provider_invoked=False,
                             first_pass_valid=False, final_valid=False,
                             failure="AUTH_DENIED_BEFORE_PROVIDER" if case.id == "Q04"
                             else "POLICY_UNAVAILABLE_BEFORE_PROVIDER",
                             first_attempt_grade={"passed": False, "diagnostics": []},
                             final_grade={"passed": False, "diagnostics": []},
                             security_check_passed=True,
                             security_evidence={"actual_boundary": True})
            records.append(TrialRecord.model_validate(value))
    summary = summarize_trials(cases, records)
    assert summary["expected_trial_count"] == 90 and summary["missing_trial_count"] == 0
    assert summary["first_attempt_denominator"] == 84
    assert summary["first_attempt_pass_count"] == 84
    assert summary["failure_count"] == 0
    assert summary["pre_provider_check_pass_count"] == 6
    assert summary["raw_failure_count"] == 6
    assert summary["pre_provider_security_checks_passed"]
    missing = summarize_trials(cases, [item for item in records
                                      if (item.case_id, item.trial) != ("Q04", 3)])
    assert missing["first_attempt_denominator"] == 84
    assert missing["missing_trial_count"] == missing["pre_provider_missing_trial_count"] == 1
    assert not missing["pre_provider_security_checks_passed"]
    failed = [item.model_copy(deep=True) for item in records]
    next(item for item in failed if item.case_id == "Q07").security_check_passed = False
    failure = summarize_trials(cases, failed)
    assert failure["pre_provider_check_fail_count"] == failure["failure_count"] == 1
    assert not failure["pre_provider_security_checks_passed"]


def test_pre_provider_case_cannot_require_a_model_refusal_or_receive_model_credit():
    case = next(case for case in load_cases(ACCEPTANCE) if case.id == "Q04")
    assert case.execution == "pre_provider_security" and case.expected_refusal is None
    assert not case.requests
    grade = grade_answer(case, {"mode": "verified", "refusal": None}, [])
    assert not grade.passed and "pre-provider" in grade.diagnostics[0]


def test_f07_declares_both_source_counts_and_underlying_household_value():
    case = next(case for case in load_cases(ACCEPTANCE) if case.id == "F07")
    assert {(fact.field, str(fact.value)) for fact in case.required_facts} == {
        ("item_count", "20"), ("total_item_count", "29"), ("assets_usd", "3000.00"),
    }
