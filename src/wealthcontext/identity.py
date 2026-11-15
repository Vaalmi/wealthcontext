"""Local demo identity. Tokens contain no household grants or historical authority."""

import os
import tempfile
from dataclasses import dataclass
from datetime import datetime, timedelta
from pathlib import Path
from typing import Literal

import jwt
from cryptography.hazmat.primitives import serialization
from cryptography.hazmat.primitives.asymmetric import rsa

Role = Literal["advisor", "ops", "compliance", "engineer"]
PERSONAS: dict[str, Role] = {
    **{f"ADV-{n:02}": "advisor" for n in range(1, 6)},
    "OPS-01": "ops", "COMP-01": "compliance", "ENG-01": "engineer",
}
ISSUER = "wealthcontext-local-demo"
AUDIENCE = "wealthcontext"


class Unauthenticated(ValueError):
    pass


@dataclass(frozen=True)
class Principal:
    id: str
    role: Role


class Identity:
    def __init__(self, private_key: rsa.RSAPrivateKey):
        self._private_key = private_key
        self._public_key = private_key.public_key()

    @classmethod
    def load_or_create(cls, directory: Path) -> "Identity":
        directory.mkdir(parents=True, exist_ok=True)
        path = directory / "demo-signing.pem"
        if not path.exists():
            key = rsa.generate_private_key(public_exponent=65537, key_size=2048)
            data = key.private_bytes(
                serialization.Encoding.PEM, serialization.PrivateFormat.PKCS8,
                serialization.NoEncryption(),
            )
            # Publish a fully written file without overwriting a concurrent creator's key.
            with tempfile.NamedTemporaryFile(dir=directory, delete=False) as pending:
                temporary = Path(pending.name)
                pending.write(data)
            try:
                try:
                    os.link(temporary, path)
                except FileExistsError:
                    pass
            finally:
                temporary.unlink()
        key = serialization.load_pem_private_key(path.read_bytes(), password=None)
        if not isinstance(key, rsa.RSAPrivateKey):
            raise ValueError("demo signing key must be RSA")
        return cls(key)

    def mint(self, persona: str, now: datetime, *, expired: bool = False) -> str:
        if persona not in PERSONAS or now.tzinfo is None:
            raise ValueError("known persona and aware clock required")
        issued = now - timedelta(days=31) if expired else now
        return jwt.encode({
            "iss": ISSUER, "aud": AUDIENCE, "sub": persona, "role": PERSONAS[persona],
            "iat": int(issued.timestamp()), "nbf": int(issued.timestamp()),
            "exp": int((issued + timedelta(days=30)).timestamp()),
        }, self._private_key, algorithm="RS256")

    def authenticate(self, authorization: str | None, now: datetime) -> Principal:
        if now.tzinfo is None:
            raise ValueError("aware request clock required")
        scheme, _, token = (authorization or "").partition(" ")
        if scheme.lower() != "bearer" or not token or len(token) > 8192:
            raise Unauthenticated("Valid bearer token required")
        try:
            claims = jwt.decode(
                token, self._public_key, algorithms=["RS256"], issuer=ISSUER, audience=AUDIENCE,
                options={"require": ["iss", "aud", "sub", "role", "iat", "nbf", "exp"],
                         "verify_exp": False, "verify_iat": False, "verify_nbf": False,
                         "strict_aud": True},
            )
            instants = [claims[k] for k in ("iat", "nbf", "exp")]
            if any(type(value) is not int for value in instants):
                raise Unauthenticated("Invalid token clock")
            issued, starts, expires = instants
            if issued > now.timestamp() or starts > now.timestamp() or expires <= now.timestamp():
                raise Unauthenticated("Token is not current")
            persona = claims["sub"]
            if persona not in PERSONAS or claims["role"] != PERSONAS[persona]:
                raise Unauthenticated("Unknown persona")
            return Principal(persona, PERSONAS[persona])
        except (jwt.InvalidTokenError, KeyError, TypeError) as exc:
            raise Unauthenticated("Valid bearer token required") from exc
