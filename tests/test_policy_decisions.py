from datetime import date

import httpx2
import pytest

from wealthcontext.identity import Principal
from wealthcontext.policy import PolicyClient, PolicyUnavailable

REVISION = "sha256:" + "a" * 64


@pytest.mark.anyio
@pytest.mark.parametrize("change", [
    {"policy_revision": "sha256:" + "b" * 64}, {"entitlement_revision": 2},
    {"projection": "invented"}, {"allowed_ids": ["WC-HH-0002"]}, {"allow": "yes"},
    {"allowed_ids": []}, {"unknown": "extra"}, {"allow": False},
])
async def test_malformed_decisions_fail_closed(monkeypatch, change: dict) -> None:
    result = {"allow": True, "allowed_ids": ["WC-HH-0001"], "projection": "full",
              "reason": "CURRENT_GRANT", "policy_revision": REVISION,
              "entitlement_revision": 1, **change}
    transport = httpx2.MockTransport(lambda _: httpx2.Response(200, json={"result": result}))
    original = httpx2.AsyncClient
    monkeypatch.setattr(httpx2, "AsyncClient", lambda **kw: original(transport=transport, **kw))
    with pytest.raises(PolicyUnavailable):
        await PolicyClient("http://opa:8181", REVISION).decide(
            Principal("ADV-01", "advisor"), "snapshot", ["WC-HH-0001"], [], 1, date(2026, 11, 1),
        )


@pytest.mark.anyio
@pytest.mark.parametrize("status,payload", [(503, {}), (200, {}), (200, {"result": None})])
async def test_unavailable_or_missing_policy_result_fails_closed(
    monkeypatch, status, payload,
) -> None:
    transport = httpx2.MockTransport(lambda _: httpx2.Response(status, json=payload))
    original = httpx2.AsyncClient
    monkeypatch.setattr(httpx2, "AsyncClient", lambda **kw: original(transport=transport, **kw))
    with pytest.raises(PolicyUnavailable):
        await PolicyClient("http://opa:8181", REVISION).decide(
            Principal("ADV-01", "advisor"), "snapshot", ["WC-HH-0001"], [], 1, date(2026, 11, 1),
        )
