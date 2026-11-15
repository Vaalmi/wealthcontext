from datetime import UTC, datetime, timedelta
from pathlib import Path

import jwt
import pytest

from wealthcontext.identity import AUDIENCE, ISSUER, Identity, Principal, Unauthenticated

NOW = datetime(2035, 4, 3, 15, tzinfo=UTC)


def test_key_persists_and_tokens_follow_injected_clock(tmp_path: Path) -> None:
    identity = Identity.load_or_create(tmp_path)
    token = identity.mint("ADV-01", NOW)
    reloaded = Identity.load_or_create(tmp_path)
    assert reloaded.authenticate(f"Bearer {token}", NOW) == Principal("ADV-01", "advisor")
    assert (tmp_path / "demo-signing.pem").stat().st_mode & 0o777 == 0o600
    assert reloaded.authenticate(f"Bearer {token}", NOW + timedelta(days=30, seconds=-1))
    with pytest.raises(Unauthenticated):
        reloaded.authenticate(f"Bearer {token}", NOW + timedelta(days=30))
    with pytest.raises(Unauthenticated):
        reloaded.authenticate(f"Bearer {token}", NOW - timedelta(seconds=1))
    with pytest.raises(Unauthenticated):
        reloaded.authenticate(f"Bearer {identity.mint('ADV-01', NOW, expired=True)}", NOW)


@pytest.mark.parametrize("change", [
    {"iss": "other"}, {"aud": "other"}, {"role": "compliance"}, {"sub": "unknown"},
    {"exp": None}, {"exp": True}, {"exp": "9999999999"}, {"nbf": 9999999999},
])
def test_signed_invalid_claims_are_rejected(tmp_path: Path, change: dict) -> None:
    identity = Identity.load_or_create(tmp_path)
    claims = {"iss": ISSUER, "aud": AUDIENCE, "sub": "ADV-01", "role": "advisor",
              "iat": int(NOW.timestamp()), "nbf": int(NOW.timestamp()),
              "exp": int((NOW + timedelta(days=30)).timestamp()), **change}
    token = jwt.encode(claims, (tmp_path / "demo-signing.pem").read_bytes(), algorithm="RS256")
    with pytest.raises(Unauthenticated):
        identity.authenticate(f"Bearer {token}", NOW)


def test_wrong_signature_and_missing_bearer_are_rejected(tmp_path: Path) -> None:
    identity = Identity.load_or_create(tmp_path / "one")
    wrong = Identity.load_or_create(tmp_path / "two").mint("ADV-01", NOW)
    for header in [None, "", "Basic value", "Bearer malformed", f"Bearer {wrong}"]:
        with pytest.raises(Unauthenticated):
            identity.authenticate(header, NOW)
