import sqlite3
from pathlib import Path
from unittest.mock import Mock

import httpx2
import pytest

from wealthcontext.app import create_app
from wealthcontext.database import connect, migrate, write_transaction
from wealthcontext.policy import PolicyClient, PolicyUnavailable
from wealthcontext.settings import Settings


def test_database_pragmas_migrations_and_rollback(tmp_path: Path) -> None:
    path = tmp_path / "db.sqlite"
    migrate(path)
    migrate(path)
    connection = connect(path)
    assert connection.execute("PRAGMA journal_mode").fetchone()[0] == "wal"
    assert connection.execute("PRAGMA foreign_keys").fetchone()[0] == 1
    assert connection.execute("PRAGMA busy_timeout").fetchone()[0] == 5000
    assert connection.execute("SELECT COUNT(*) FROM schema_version").fetchone()[0] == 1
    connection.close()
    with pytest.raises(RuntimeError), write_transaction(path) as connection:
        connection.execute("INSERT INTO schema_version VALUES (2)")
        raise RuntimeError("abort")
    with sqlite3.connect(path) as connection:
        assert connection.execute("SELECT COUNT(*) FROM schema_version").fetchone()[0] == 1


@pytest.mark.parametrize("value", ["wrong", "sha256:" + "A" * 64,
                                  "sha256:+" + "a" * 63, "sha256:" + "1_" * 32])
def test_settings_reject_bad_revision(tmp_path: Path, monkeypatch, value: str) -> None:
    monkeypatch.setenv("WEALTHCONTEXT_BUNDLE_DIR", str(tmp_path))
    settings = Settings.from_env()
    (tmp_path / "revision.txt").write_text(value)
    with pytest.raises(ValueError):
        _ = settings.expected_revision


def test_nested_write_fails_promptly_without_poisoning_the_next_write(tmp_path: Path) -> None:
    path = tmp_path / "db.sqlite"
    with write_transaction(path), pytest.raises(RuntimeError, match="nested"):
        with write_transaction(path):
            pytest.fail("nested write opened a second connection")
    with write_transaction(path) as connection:
        assert connection.execute("SELECT 1").fetchone()[0] == 1


def test_connect_closes_connection_if_pragma_fails(tmp_path: Path, monkeypatch) -> None:
    connection = Mock()
    connection.execute.side_effect = sqlite3.OperationalError("locked")
    monkeypatch.setattr(sqlite3, "connect", lambda *args, **kwargs: connection)
    with pytest.raises(sqlite3.OperationalError):
        connect(tmp_path / "db.sqlite")
    connection.close.assert_called_once()


@pytest.mark.anyio
@pytest.mark.parametrize("status,payload,passes", [
    (200, {"result": {"bundle": {"manifest": {"revision": "sha256:expected"}}}}, True),
    (200, {"result": {"bundle": {"manifest": {"revision": "sha256:other"}}}}, False),
    (200, {"result": {"one": {"manifest": {"revision": "sha256:expected"}},
                      "two": {"manifest": {"revision": "sha256:expected"}}}}, False),
    (200, {"result": {}}, False), (200, {}, False), (503, {}, False),
])
async def test_policy_revision_fails_closed(monkeypatch, status, payload, passes) -> None:
    client_type = httpx2.AsyncClient
    transport = httpx2.MockTransport(lambda _: httpx2.Response(status, json=payload))
    monkeypatch.setattr(httpx2, "AsyncClient", lambda **kw: client_type(transport=transport, **kw))
    policy = PolicyClient("http://opa:8181", "sha256:expected")
    if passes:
        assert await policy.verify_revision() == "sha256:expected"
    else:
        with pytest.raises(PolicyUnavailable):
            await policy.verify_revision()


@pytest.mark.anyio
async def test_app_readiness_checks_policy_after_startup(tmp_path: Path, monkeypatch) -> None:
    revision = "sha256:" + "b" * 64
    (tmp_path / "revision.txt").write_text(revision)
    settings = Settings(tmp_path, tmp_path, "http://opa:8181")
    available = True

    async def verify(self) -> str:
        if not available:
            raise PolicyUnavailable("unavailable")
        return revision

    monkeypatch.setattr(PolicyClient, "verify_revision", verify)
    app = create_app(settings)
    async with app.router.lifespan_context(app):
        async with httpx2.AsyncClient(
            transport=httpx2.ASGITransport(app=app), base_url="http://localhost:8000"
        ) as client:
            assert (await client.get("/health")).json()["policy_revision"] == revision
            landing = await client.get("/")
            assert landing.status_code == 200
            assert landing.headers["content-type"].startswith("text/html")
            assert "WealthContext" in landing.text and "Enter workspace" in landing.text
            available = False
            assert (await client.get("/health")).status_code == 503


@pytest.mark.anyio
async def test_app_does_not_start_with_unverified_policy(tmp_path: Path, monkeypatch) -> None:
    (tmp_path / "revision.txt").write_text("sha256:" + "a" * 64)

    async def unavailable(self) -> str:
        raise PolicyUnavailable("mismatch")

    monkeypatch.setattr(PolicyClient, "verify_revision", unavailable)
    app = create_app(Settings(tmp_path, tmp_path, "http://opa:8181"))
    with pytest.raises(PolicyUnavailable):
        async with app.router.lifespan_context(app):
            pytest.fail("app started without a verified bundle")
    assert not (tmp_path / "wealthcontext.sqlite").exists()
