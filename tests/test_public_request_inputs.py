"""Knowledge-time request inputs are explicit; evaluation expectations stay private."""

import json
from datetime import UTC, datetime
from pathlib import Path

import pytest

from wealthcontext.evaluation import load_cases
from wealthcontext.evaluation_runtime import public_question

ROOT = Path(__file__).resolve().parents[1]
MARKER = "\nPublic request inputs (data):\n"
F01_RECORDED_QUESTION = (
    "What assets are included for the clean household at quarter-end?"
    + MARKER
    + '{"additional_known_as_of": ["2026-10-31T16:00:00Z"], '
    '"household_ids": ["WC-HH-0001"], "known_as_of": "2026-10-31T16:00:00+00:00", '
    '"setup_bindings": {}, "valuation_date": "2026-09-30"}'
)


def case_named(name):
    return next(case for case in load_cases(ROOT / "acceptance") if case.id == name)


def inputs_for(case, bindings=None):
    question = public_question(case, bindings or {})
    heading, body = question.split(MARKER)
    assert heading == case.question
    return json.loads(body)


def test_real_comparison_inputs_omit_the_unrequested_default_without_changing_criteria():
    case = case_named("F11")
    before = case.model_dump_json()
    inputs = inputs_for(case)
    assert inputs["requested_known_as_of"] == [
        "2026-10-06T13:59:59.999999+00:00", "2026-10-06T14:00:00+00:00",
    ]
    assert "known_as_of" not in inputs
    assert "additional_known_as_of" not in inputs
    assert case.known_as_of.isoformat() not in public_question(case, {})
    assert "complete set" in inputs["knowledge_time_scope"]
    assert inputs["valuation_date"] == "2026-09-30"
    assert case.model_dump_json() == before
    assert case.request_time == datetime(2026, 11, 1, 12, tzinfo=UTC)


def test_a_single_explicit_override_replaces_the_general_default():
    case = case_named("F01")
    case.requests[0].arguments["known_as_of"] = "2026-10-01T12:00:00Z"
    inputs = inputs_for(case)
    assert inputs["requested_known_as_of"] == ["2026-10-01T12:00:00+00:00"]
    assert "known_as_of" not in inputs
    assert "additional_known_as_of" not in inputs


def test_matching_single_default_preserves_actual_recorded_f01_bytes():
    assert public_question(case_named("F01"), {}) == F01_RECORDED_QUESTION


def test_request_cutoffs_are_deduplicated_and_sorted_by_instant_not_offset_text():
    case = case_named("F11")
    case.requests[0].arguments["known_as_of"] = "2026-10-06T10:00:00-04:00"
    case.requests[1].arguments["known_as_of"] = "2026-10-06T14:00:00Z"
    case.requests.append(case.requests[0].model_copy(deep=True, update={"alias": "earlier"}))
    case.requests[-1].arguments["known_as_of"] = "2026-10-06T15:00:00+02:00"
    assert inputs_for(case)["requested_known_as_of"] == [
        "2026-10-06T13:00:00+00:00", "2026-10-06T14:00:00+00:00",
    ]


def test_equivalent_default_offset_does_not_create_a_comparison_scope():
    case = case_named("F01")
    case.requests[0].arguments["known_as_of"] = "2026-10-31T12:00:00-04:00"
    inputs = inputs_for(case)
    assert "requested_known_as_of" not in inputs
    assert inputs["known_as_of"] == "2026-10-31T16:00:00+00:00"
    assert inputs["additional_known_as_of"] == ["2026-10-31T12:00:00-04:00"]


def test_expected_values_rubric_aliases_and_expected_status_never_enter_inputs():
    case = case_named("F11")
    original = public_question(case, {"received_reference": "handle_public"})
    case.required_facts[0].value = "EXPECTED_VALUE_SENTINEL"
    case.manual_checks = ["RUBRIC_SENTINEL"]
    case.requests[0].alias = "ALIAS_SENTINEL"
    case.required_facts[0].request = "ALIAS_SENTINEL"
    case.requests[0].status = "ERROR"
    case.requests[0].metric_refs = ["EXPECTED_METRIC_SENTINEL"]
    case.setup = ["SETUP_PROSE_SENTINEL"]
    assert public_question(case, {"received_reference": "handle_public"}) == original
    assert inputs_for(case, {"received_reference": "handle_public"})["setup_bindings"] == {
        "received_reference": "handle_public",
    }


def test_request_without_explicit_cutoffs_keeps_the_existing_default_shape():
    case = case_named("Q02")
    inputs = inputs_for(case)
    assert inputs["known_as_of"] == case.known_as_of.isoformat()
    assert inputs["additional_known_as_of"] == []
    assert "requested_known_as_of" not in inputs


def test_naive_explicit_cutoff_is_not_interpreted_in_the_host_timezone():
    case = case_named("F01")
    case.requests[0].arguments["known_as_of"] = "2026-10-01T12:00:00"
    with pytest.raises(ValueError, match="must include a timezone"):
        public_question(case, {})


def test_mixed_explicit_and_default_temporal_requests_keep_each_requested_cutoff():
    case = case_named("F11")
    case.requests[1].arguments.pop("known_as_of")
    inputs = inputs_for(case)
    assert inputs["requested_known_as_of"] == [
        # The request without a cutoff executes at the captured request time, not at
        # the case's general default (SOL-P5-R1-RT-001).
        "2026-10-06T13:59:59.999999+00:00", "2026-11-01T12:00:00+00:00",
    ]
    assert "known_as_of" not in inputs
    assert inputs["valuation_date"] == "2026-09-30"


@pytest.mark.parametrize(("tool", "arguments"), [
    ("get_metric_definition", {"metric_id": "HOUSEHOLD_ASSETS"}),
    ("get_data_provenance", {"reference": "h_public_reference"}),
])
def test_nontemporal_calls_do_not_add_a_default_to_explicit_temporal_scope(tool, arguments):
    case = case_named("F01")
    case.requests[0].arguments["known_as_of"] = "2026-10-01T12:00:00Z"
    case.requests.append(case.requests[0].model_copy(deep=True, update={
        "alias": "nontemporal", "tool": tool, "arguments": arguments,
    }))
    assert inputs_for(case)["requested_known_as_of"] == ["2026-10-01T12:00:00+00:00"]


def test_metric_only_request_keeps_the_legacy_default_input_shape():
    case = case_named("F01")
    case.requests = [case.requests[0].model_copy(deep=True, update={
        "tool": "get_metric_definition", "arguments": {"metric_id": "HOUSEHOLD_ASSETS"},
    })]
    inputs = inputs_for(case)
    assert inputs["known_as_of"] == case.known_as_of.isoformat()
    assert inputs["additional_known_as_of"] == []
    assert "requested_known_as_of" not in inputs
