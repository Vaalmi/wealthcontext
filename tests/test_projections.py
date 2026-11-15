"""No-PII evidence cannot hide free text in arbitrary JSON fields or fragments."""

import copy

import pytest

from wealthcontext.projections import REDACTED, project_envelope, project_no_pii
from wealthcontext.source_projection import SourceScope, project_source_record
from wealthcontext.storage import canonical, digest


def record(payload):
    text = canonical(payload)
    return {"id": "SRC-generated", "source_system": "CRM",
            "source_record_id": "Private-Person-Name", "payload": text,
            "payload_hash": digest(text)}


@pytest.mark.parametrize("payload", [
    "Private person in a truncated fragment", ["Private household in array"], 123,
])
def test_unstructured_received_payload_is_entirely_redacted_for_every_projection(payload):
    for projection in ("full", "no_pii"):
        projected = project_source_record(record(payload), SourceScope(frozenset(), frozenset(),
                                                                        frozenset()),
                                          projection=projection)
        assert projected["payload"] == REDACTED
        assert projected["source_record_id"] == REDACTED


def test_no_pii_source_projection_preserves_machine_values_and_hashes_without_names():
    payload = {"kind": "ownership", "account_id": "WC-AC-1", "title": "Private title",
               "name": "Private person", "household_name": "Private household",
               "quantity": "100.125", "currency": "USD", "note": "SECRET-NOTE",
               "external_person_key": "PrivatePerson", "opaque": "Private household",
               "Private household field": "Private title", "Private Person_name": "hidden",
               "Private Person_title": "hidden", "details": {"name": "Nested person"}}
    stored = record(payload)
    before = copy.deepcopy(stored)
    projected = project_source_record(stored, SourceScope(frozenset({"WC-HH-1"}),
                                                         frozenset({"WC-AC-1"}), frozenset()),
                                      projection="no_pii")
    serialized = canonical(projected)
    for marker in ("Private", "Nested person", "SECRET-NOTE", "opaque", "external_person_key"):
        assert marker not in serialized
    assert projected["payload"]["quantity"] == "100.125"
    assert projected["payload"]["account_id"] == "WC-AC-1"
    assert projected["payload_hash"] == stored["payload_hash"]
    expected_hash = digest(canonical(projected["payload"]))
    assert projected["projection"]["projected_payload_hash"] == expected_hash
    assert projected["projection"]["redacted"] is True and stored == before


def test_closed_envelope_redacts_nested_names_titles_notes_and_rejects_without_mutation():
    original = {"data": {"household_name": "Private household", "items": [
        {"name": "Private person", "title": "Private title", "note": "SECRET-NOTE",
         "raw_payload": {"unrecognised": "Private person"}, "count": 3},
    ]}, "quality_flags": [{"detail": {"name": "Private name", "unknown": "Private person"}}]}
    before = copy.deepcopy(original)
    result = project_envelope(original, "no_pii")
    assert "Private" not in canonical(result) and "SECRET-NOTE" not in canonical(result)
    assert result["data"]["items"][0]["count"] == 3
    assert original == before
    assert project_no_pii({"unexpected": {"name": "Hidden"}}) == {}
