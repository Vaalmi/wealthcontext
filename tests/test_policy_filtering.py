"""Book subsets are allowed only by the explicit filtered decision path."""

from datetime import date

import httpx2
import pytest

from wealthcontext.identity import Principal
from wealthcontext.policy import PolicyClient, PolicyUnavailable

REVISION = "sha256:" + "a" * 64
CANDIDATES = ["WC-HH-1", "WC-HH-2"]


@pytest.mark.anyio
@pytest.mark.parametrize("ids", [[], ["WC-HH-2"], CANDIDATES])
async def test_filtered_book_accepts_verified_subset_and_empty_book(monkeypatch, ids):
    result = {
        "allow": True, "allowed_ids": ids, "projection": "full", "reason": "CURRENT_GRANT",
        "policy_revision": REVISION, "entitlement_revision": 7,
    }
    original = httpx2.AsyncClient
    transport = httpx2.MockTransport(lambda _: httpx2.Response(200, json={"result": result}))
    monkeypatch.setattr(httpx2, "AsyncClient", lambda **kw: original(transport=transport, **kw))
    decision = await PolicyClient("http://opa.invalid", REVISION).filter_candidates(
        Principal("ADV-1", "advisor"), CANDIDATES, [], 7, date(2026, 11, 1),
    )
    assert decision.allowed_ids == sorted(ids)
    if set(ids) != set(CANDIDATES):
        with pytest.raises(PolicyUnavailable):
            await PolicyClient("http://opa.invalid", REVISION).decide(
                Principal("ADV-1", "advisor"), "provenance", CANDIDATES, [], 7,
                date(2026, 11, 1),
            )


@pytest.mark.anyio
@pytest.mark.parametrize("change", [
    {"allowed_ids": ["WC-HH-other"]}, {"allowed_ids": ["WC-HH-1", "WC-HH-1"]},
    {"allow": False, "allowed_ids": ["WC-HH-1"]}, {"allow": "true"},
    {"policy_revision": "sha256:" + "b" * 64}, {"entitlement_revision": 8},
    {"projection": "unrecognized"}, {"unexpected": "field"},
])
async def test_filtered_policy_malformed_decision_is_unavailable(monkeypatch, change):
    result = {
        "allow": True, "allowed_ids": ["WC-HH-1"], "projection": "full",
        "reason": "CURRENT_GRANT", "policy_revision": REVISION, "entitlement_revision": 7,
        **change,
    }
    original = httpx2.AsyncClient
    transport = httpx2.MockTransport(lambda _: httpx2.Response(200, json={"result": result}))
    monkeypatch.setattr(httpx2, "AsyncClient", lambda **kw: original(transport=transport, **kw))
    with pytest.raises(PolicyUnavailable):
        await PolicyClient("http://opa.invalid", REVISION).filter_candidates(
            Principal("ADV-1", "advisor"), CANDIDATES, [], 7, date(2026, 11, 1),
        )


@pytest.mark.anyio
async def test_duplicate_candidates_never_contact_opa():
    with pytest.raises(PolicyUnavailable):
        await PolicyClient("http://must-not-be-contacted.invalid", REVISION).filter_candidates(
            Principal("ADV-1", "advisor"), ["WC-HH-1", "WC-HH-1"], [], 7, date(2026, 11, 1),
        )
