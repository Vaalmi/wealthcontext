"""Import deterministic received history once, then rely on ingestion idempotency."""

from pathlib import Path

from wealthcontext.clock import Clock
from wealthcontext.generator import load_history, write_history
from wealthcontext.ingestion import import_fixture_history, migrate_feeds


def bootstrap_history(database: Path, directory: Path, clock: Clock) -> None:
    migrate_feeds(database)
    events = (load_history(directory) if (directory / "index.json").exists()
              else write_history(directory))
    import_fixture_history(database, events, clock)
