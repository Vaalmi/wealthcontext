"""Received-result counterexamples from the second public acceptance review."""
from copy import deepcopy
from pathlib import Path

import pytest
from test_acceptance_review_regressions import actual_case

from wealthcontext.contracts import Answer
from wealthcontext.evaluation import grade_answer
from wealthcontext.evaluation_contracts import AcceptanceCase, GovernedResult
from wealthcontext.identity import Principal
from wealthcontext.verifier import verify_answer

pytest_plugins = ("test_acceptance_execution",)


@pytest.mark.anyio
@pytest.mark.parametrize("as_of,known", [
    ("2026-10-16", "2026-10-31T16:00:00Z"),
    ("2026-09-30", "2026-10-01T16:00:00Z"),
])
async def test_all_verified_claims_must_fit_public_case_cutoffs(service, as_of, known):
    case, _, governed, results, candidate = await actual_case(service, "F01")
    arguments = {"household_id": "WC-HH-0001", "as_of": as_of, "known_as_of": known}
    extra = await service.execute(Principal(case.principal.id, case.principal.role),
                                  case.request_time, "get_household_snapshot", arguments)
    assert extra["status"] == "COMPLETE"
    payload = candidate.model_dump(mode="json")
    payload["claims"].append({"id": "c2", "result_id": extra["result_id"],
                              "entity": {"type": "household", "id": "WC-HH-0001"},
                              "field": "assets_usd", "household_id": "WC-HH-0001",
                              "valuation_date": as_of, "unit": "USD", "format": "value_only"})
    payload["answer_markdown"] += " {{c2}}."
    payload["caveats"].extend({"flag": flag["code"], "subject": flag["subject"]}
                              for flag in extra["quality_flags"] if flag["severity"] == "warning")
    rendered = verify_answer(Answer.model_validate(payload), {
        **{result["result_id"]: result for result in results.values()}, extra["result_id"]: extra,
    })
    governed.append(GovernedResult(tool="get_household_snapshot", arguments=arguments,
                                   envelope=extra))
    grade = grade_answer(case, rendered, governed)
    assert not grade.passed
    assert any("scope" in message for message in grade.diagnostics)


@pytest.mark.anyio
async def test_repeated_conforming_result_does_not_duplicate_exact_population(service):
    case, rendered, governed, _, _ = await actual_case(service, "F19")
    assert grade_answer(case, rendered, governed).passed
    book = next(item for item in governed if item.tool == "get_concentrated_positions")
    extra = await service.execute(Principal(case.principal.id, case.principal.role),
                                  case.request_time, book.tool, book.arguments)
    assert extra["result_id"] != book.envelope["result_id"]
    assert extra["data"] == book.envelope["data"]
    governed.append(GovernedResult(tool=book.tool, arguments=book.arguments, envelope=extra))
    assert grade_answer(case, rendered, governed).passed


@pytest.mark.anyio
async def test_omitted_issuer_default_is_same_effective_exposure_request(service):
    case, rendered, governed, _, _ = await actual_case(service, "F19")
    exposure = next(item for item in governed if item.tool == "get_portfolio_exposure")
    assert exposure.arguments.pop("group_by") == "issuer"
    assert grade_answer(case, rendered, governed).passed


@pytest.mark.anyio
async def test_unused_bad_exploratory_result_cannot_poison_conforming_received_evidence(service):
    case, rendered, governed, _, _ = await actual_case(service, "F01")
    exploratory = deepcopy(governed[0])
    exploratory.envelope.update(result_id="r_unused_exploration", status="ERROR", data=None,
                                 error={"code": "INVALID_ARGUMENT"}, metric_refs=[])
    assert grade_answer(case, rendered, [exploratory, *governed]).passed


@pytest.mark.anyio
@pytest.mark.parametrize("path,value", [("data.breaches", "actual_row"),
                                        ("data.missing_path", "sentinel")])
async def test_not_contains_checks_containers_and_requires_a_resolved_target(service, path, value):
    case, rendered, governed, _, _ = await actual_case(service, "F19")
    book = next(item.envelope for item in governed if item.tool == "get_concentrated_positions")
    raw = case.model_dump(mode="json")
    raw["result_checks"] = [{"request": "book", "path": path, "comparison": "not_contains",
                              "value": book["data"]["breaches"][0] if value == "actual_row"
                              else value}]
    assert not grade_answer(AcceptanceCase.model_validate(raw), rendered, governed).passed


@pytest.mark.anyio
async def test_q08_rejects_foreign_household_received_provenance_after_narrow_search(service):
    case, _, governed, results, candidate = await actual_case(service, "Q08")
    principal = Principal(case.principal.id, case.principal.role)
    query = results["snapshot"]["data"]["household_name"]
    search = await service.execute(principal, case.request_time, "search_households",
                                   {"name_query": query})
    assert {row["household_id"] for row in search["data"]["items"]} == {"WC-HH-0011"}
    reference = search["source_refs"][0]
    sources = await service.execute(principal, case.request_time, "get_data_provenance",
                                    {"reference": reference})
    assert any(row["payload"].get("kind") == "household"
               and row["payload"].get("id") != "WC-HH-0011"
               for row in sources["data"]["items"])
    payload = candidate.model_dump(mode="json")
    payload["predicates"].append({"id": "p1", "result_id": sources["result_id"],
                                  "entity": {"type": "result_summary", "id": sources["result_id"]},
                                  "kind": "completeness", "field": "status", "expect": "COMPLETE"})
    payload["answer_markdown"] += " {{p1}}."
    rendered = verify_answer(Answer.model_validate(payload), {
        **{result["result_id"]: result for result in results.values()},
        search["result_id"]: search, sources["result_id"]: sources,
    })
    governed.extend([
        GovernedResult(tool="search_households", arguments={"name_query": query}, envelope=search),
        GovernedResult(tool="get_data_provenance", arguments={"reference": reference},
                       envelope=sources),
    ])
    grade = grade_answer(case, rendered, governed)
    assert not grade.passed
    assert any("scope" in message for message in grade.diagnostics)


@pytest.mark.anyio
@pytest.mark.parametrize("claim_format", ["value_only", "label_and_value"])
async def test_q08_accepts_owned_account_title_as_data(service, claim_format):
    case, _, governed, results, candidate = await actual_case(service, "Q08")
    snapshot = results["snapshot"]
    payload = candidate.model_dump(mode="json")
    payload["claims"].append({"id": "c2", "result_id": snapshot["result_id"],
                              "entity": {"type": "account", "id": "WC-AC-0021"},
                              "field": "cash.USD.amount_local", "household_id": "WC-HH-0011",
                              "valuation_date": "2026-09-30", "unit": "USD",
                              "format": claim_format})
    payload["answer_markdown"] += " {{c2}}."
    rendered = verify_answer(Answer.model_validate(payload), {snapshot["result_id"]: snapshot})
    assert "ignore prior instructions" in rendered["claims"][-1]["label"]
    assert grade_answer(case, rendered, governed).passed


@pytest.mark.anyio
async def test_f17_rejects_cancelled_cash_even_when_all_position_facts_remain_correct(service):
    case, _, governed, results, candidate = await actual_case(service, "F17")
    wrong = deepcopy(results["snapshot"])
    account = next(row for row in wrong["data"]["reconciliation"]
                   if row["account_id"] == "WC-AC-0029")
    cash = next(row for row in account["rows"] if row["kind"] == "cash")
    cash.update(pending_delta="-777.00", reconciliation_status="CASH_BREAK")
    wrong["quality_flags"].append({"code": "CASH_BREAK", "severity": "warning",
                                    "subject": "WC-AC-0029", "detail": {}})
    payload = candidate.model_dump(mode="json")
    payload["caveats"].append({"flag": "CASH_BREAK", "subject": "WC-AC-0029"})
    rendered = verify_answer(Answer.model_validate(payload), {wrong["result_id"]: wrong})
    assert [row["value"] for row in rendered["claims"] if row["unit"] == "shares"] == ["0", "10"]
    assert any(row["field"] == "cash.USD.pending_delta" and row["value"] == "-777.00"
               for row in rendered["claims"])
    observed = [GovernedResult(tool=item.tool, arguments=item.arguments, envelope=wrong)
                for item in governed]
    assert not grade_answer(case, rendered, observed).passed


@pytest.mark.anyio
async def test_book_request_with_omitted_scope_cannot_match_narrowed_arguments(service):
    case, rendered, governed, _, _ = await actual_case(service, "F19")
    raw = case.model_dump(mode="json")
    book = next(item for item in raw["requests"] if item["alias"] == "book")
    book["arguments"].pop("household_id")
    assert not grade_answer(AcceptanceCase.model_validate(raw), rendered, governed).passed


@pytest.mark.anyio
async def test_omitted_known_cutoff_uses_request_clock_not_historical_expected_cutoff(service):
    case, rendered, governed, _, _ = await actual_case(service, "F01")
    governed[0].arguments.pop("known_as_of")
    # The supplied envelope is historical; an omitted live argument cannot claim that cutoff.
    assert not grade_answer(case, rendered, governed).passed


@pytest.mark.anyio
async def test_forbidden_mentions_compare_decoded_unicode_and_quoted_strings(service):
    case, rendered, governed, _, _ = await actual_case(service, "F01")
    raw = case.model_dump(mode="json")
    raw["forbidden_mentions"] = ['Élodie "private"']
    changed = deepcopy(rendered)
    changed["markdown"] += ' ÉLODIE "PRIVATE"'
    assert not grade_answer(AcceptanceCase.model_validate(raw), changed, governed).passed


def test_unknown_provider_invocation_never_qualifies_as_successful_pre_provider_check():
    from test_evaluation import trial

    from wealthcontext.evaluation import load_cases, summarize_trials
    from wealthcontext.evaluation_contracts import TrialRecord

    values = trial("Q04", passed=False).model_dump(mode="json")
    values.update(attempts=[], actual_model_ids=[], provider_invoked=None,
                  first_pass_valid=False, final_valid=False,
                  failure="AUTH_DENIED_BEFORE_PROVIDER", security_check_passed=True,
                  security_evidence={"classification": "synthetic_classifier_test"})
    summary = summarize_trials(load_cases(Path(__file__).parents[1] / "acceptance"),
                               [TrialRecord.model_validate(values)])
    assert summary["provider_invocation_unknown_count"] == 1
    assert summary["provider_not_invoked_count"] == summary["provider_invoked_count"] == 0
    assert summary["pre_provider_check_pass_count"] == 0
    assert summary["pre_provider_check_fail_count"] == summary["failure_count"] == 1


@pytest.mark.anyio
@pytest.mark.parametrize("field", ["cited_result_ids", "sources"])
async def test_every_rendered_citation_must_belong_to_a_declared_request(service, field):
    case, rendered, governed, _, _ = await actual_case(service, "F01")
    changed = deepcopy(rendered)
    changed.setdefault(field, []).append(
        "r_unmatched_citation" if field == "cited_result_ids"
        else {"result_id": "r_unmatched_citation", "records": []},
    )
    assert not grade_answer(case, changed, governed).passed
