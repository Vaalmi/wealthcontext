"""SQLite bootstrap. Only the app process opens this database."""

import sqlite3
import threading
from collections.abc import Iterator
from contextlib import contextmanager
from pathlib import Path

WRITE_LOCK = threading.Lock()
WRITE_STATE = threading.local()


def connect(path: Path) -> sqlite3.Connection:
    connection = sqlite3.connect(path, timeout=5, isolation_level=None)
    try:
        connection.row_factory = sqlite3.Row
        connection.execute("PRAGMA journal_mode=WAL")
        connection.execute("PRAGMA busy_timeout=5000")
        connection.execute("PRAGMA foreign_keys=ON")
    except BaseException:
        connection.close()
        raise
    return connection


@contextmanager
def write_transaction(path: Path) -> Iterator[sqlite3.Connection]:
    if getattr(WRITE_STATE, "active", False):
        raise RuntimeError("nested write transactions are not supported")
    with WRITE_LOCK:
        connection = connect(path)
        WRITE_STATE.active = True
        try:
            connection.execute("BEGIN IMMEDIATE")
            yield connection
            connection.commit()
        except BaseException:
            connection.rollback()
            raise
        finally:
            connection.close()
            WRITE_STATE.active = False


def migrate(path: Path) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    with write_transaction(path) as connection:
        connection.execute(
            "CREATE TABLE IF NOT EXISTS schema_version (version INTEGER PRIMARY KEY)"
        )
        connection.execute("INSERT OR IGNORE INTO schema_version VALUES (1)")
