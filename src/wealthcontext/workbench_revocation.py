"""A7: bounded, isolated, per-session seeded stores for the seeded revocation (S04, Q02).

The demo database already holds the reassignment of WC-HH-0004 from ADV-03 to ADV-05,
and entitlements apply at request time, so a conversation from before it cannot happen
there. The evaluation runtime stages this case in a store of its own (decision 3), and
the workbench reuses that staging: once per process it builds one store from the
received history before the reassignment, runs Q02's staging on it, and keeps it as a
template. Each session that opens A7 gets its own copy, so nothing one visitor does
reaches another, and the main demo database is never opened.

Stores are capped, and the cap counts every copy whose files exist. A store is deleted
when its session ends: replaced by a new context, expired or evicted from the session
table. A request that is still using a store holds a lease on it, and a released store
is deleted when its last lease ends, so no request can write to a store's file after it
is gone. Opening one more than the cap evicts the least recently used store that no
request holds; when every copy is held, nothing is opened and the page says to try
again. Closing ends the folder's generation: a staging or copy still under way is never
registered, and the folder is removed once no lease or staging in it remains. A failed
staging or copy leaves nothing behind, and the next request tries again.
"""

import secrets
import shutil
import sqlite3
import tempfile
from collections import OrderedDict
from collections.abc import Callable, Iterator
from contextlib import contextmanager
from dataclasses import dataclass
from functools import partial
from pathlib import Path

import anyio
from opentelemetry.trace import Tracer

from wealthcontext.clock import Clock
from wealthcontext.context_service import ContextService
from wealthcontext.conversation import ConversationManager
from wealthcontext.database import migrate
from wealthcontext.generator import write_history
from wealthcontext.identity import Principal
from wealthcontext.ingestion import import_fixture_history, migrate_feeds
from wealthcontext.policy import PolicyClient
from wealthcontext.storage import migrate_business

# Q02's principal, household and cutoffs (acceptance/Q02.yaml); a test keeps them equal.
SEEDED_PRINCIPAL = Principal("ADV-03", "advisor")
SEEDED_ARGUMENTS = {"household_id": "WC-HH-0004", "as_of": "2026-09-30",
                    "known_as_of": "2026-10-31T16:00:00+00:00"}
# Each store is a full copy of the synthetic history, about 19 MB.
SEEDED_STORE_LIMIT = 4


@dataclass(frozen=True)
class Seed:
    """The staged template: identifiers only, the same in every copy."""

    database: Path
    conversation_id: str
    # The earlier turn's results, which the reassignment revoked.
    earlier_result_id: str
    earlier_handles: tuple[str, ...]
    earlier_answer_id: str


@dataclass(eq=False)
class _Folder:
    """One generation of stores: a temporary folder and the template staged in it.
    close() ends a generation; its folder is removed once nothing in it is in use."""

    directory: tempfile.TemporaryDirectory[str]
    seed: Seed | None = None
    # Released stores a request still holds, and stagings or copies under way, in it.
    users: int = 0
    closed: bool = False

    @property
    def path(self) -> Path:
        return Path(self.directory.name)


@dataclass(eq=False)
class SeededStore:
    """One session's copy. It holds identifiers and run metadata, never a payload."""

    database: Path
    service: ContextService
    seed: Seed
    folder: _Folder
    followup: dict | None = None
    busy: bool = False
    # Requests using this store now; a released store is deleted when this reaches zero.
    leases: int = 0
    released: bool = False

    def held(self, result_id: str, handle: str) -> bool:
        """Was this pair issued by the earlier turn of this store's conversation?"""
        return result_id == self.seed.earlier_result_id and handle in self.seed.earlier_handles


def _stage(directory: Path, policy: PolicyClient, tracer: Tracer, clock: Clock) -> Seed:
    """Build the template, as the evaluation builds Q02's trial store, then stage it."""
    # Imported here: the evaluation runtime imports the app, which installs the workbench.
    from wealthcontext.evaluation_runtime import before_reassignment, stage_revocation

    history = write_history(directory / "received")
    database = directory / "template.sqlite"
    migrate(database)
    migrate_business(database)
    migrate_feeds(database)
    import_fixture_history(database, before_reassignment(history), clock)
    service = ContextService(database, policy, tracer)
    stage = anyio.run(partial(
        stage_revocation, service, ConversationManager(service), history, clock,
        SEEDED_PRINCIPAL, dict(SEEDED_ARGUMENTS)))
    assert stage.setup.conversation_id is not None
    return Seed(database, stage.setup.conversation_id, stage.earlier_result["result_id"],
                tuple(stage.earlier_result["source_refs"]), stage.earlier_answer["result_id"])


def _copy(source: Path, target: Path) -> None:
    """A consistent copy through SQLite's backup API, never a raw file copy."""
    origin, copy = sqlite3.connect(source), sqlite3.connect(target)
    try:
        origin.backup(copy)
    finally:
        copy.close()
        origin.close()


def _remove(database: Path) -> None:
    """A store's database, its write-ahead log, shared memory and rollback journal."""
    for suffix in ("", "-wal", "-shm", "-journal"):
        Path(str(database) + suffix).unlink(missing_ok=True)


def _delete(store: SeededStore) -> None:
    _remove(store.database)


class SeededStoreUnavailable(Exception):
    """No store can be opened now: every copy the cap allows is in use, or the stores
    were closed while this one was being prepared. Nothing was left behind."""


class SeededStores:
    def __init__(self, *, limit: int = SEEDED_STORE_LIMIT, root: Path | None = None,
                 stage: Callable[[Path, PolicyClient, Tracer, Clock], Seed] = _stage):
        if limit < 1:
            raise ValueError("at least one seeded store")
        self.limit, self._root, self._stage = limit, root, stage
        self._folder: _Folder | None = None
        self._lock: anyio.Lock | None = None
        self._stores: OrderedDict[str, SeededStore] = OrderedDict()
        # Released stores a request still holds. Their files still exist, so they count
        # against the cap with the registered ones.
        self._held: set[SeededStore] = set()

    @property
    def directory(self) -> Path:
        return self._current().path

    def _current(self) -> _Folder:
        if self._folder is None:
            # Removed with this object or at exit; never inside the demo's data directory.
            self._folder = _Folder(tempfile.TemporaryDirectory(
                prefix="wealthcontext-a7-", dir=self._root, ignore_cleanup_errors=True))
        return self._folder

    def get(self, session_id: str) -> SeededStore | None:
        return self._stores.get(session_id)

    def __len__(self) -> int:
        return len(self._stores)

    @property
    def copies(self) -> int:
        """Session copies whose files exist: registered, and released but still held."""
        return len(self._stores) + len(self._held)

    async def open(self, session_id: str, policy: PolicyClient, tracer: Tracer,
                   clock: Clock) -> SeededStore:
        """This session's store, staged on first use. At the cap the least recently used
        store no request holds is evicted; if every copy is in use, nothing is opened."""
        self._lock = self._lock or anyio.Lock()
        async with self._lock:
            store = self._stores.get(session_id)
            if store is not None:
                self._stores.move_to_end(session_id)
                return store
            # The copy made below counts too, so the cap holds while it is made.
            for oldest in [key for key, item in self._stores.items() if item.leases == 0]:
                if self.copies < self.limit:
                    break
                self.release(oldest)
            if self.copies >= self.limit:
                raise SeededStoreUnavailable(
                    "Every seeded revocation session is in use. Try again in a moment.")
            folder = self._current()
            # A staging or copy under way keeps its folder: close() removes it afterwards.
            folder.users += 1
            try:
                if folder.seed is None:
                    seed = await self._staged(folder, policy, tracer, clock)
                    if not folder.closed:
                        folder.seed = seed
                if folder.closed:
                    raise SeededStoreUnavailable("The seeded stores were closed.")
                assert folder.seed is not None
                database = folder.path / f"session-{secrets.token_hex(16)}.sqlite"
                try:
                    await anyio.to_thread.run_sync(_copy, folder.seed.database, database)
                    if folder.closed:
                        raise SeededStoreUnavailable("The seeded stores were closed.")
                except BaseException:
                    # A failed or abandoned copy is never registered, so nothing else
                    # would find it.
                    _remove(database)
                    raise
            finally:
                folder.users -= 1
                self._cleanup(folder)
            store = SeededStore(database, ContextService(database, policy, tracer),
                                folder.seed, folder)
            self._stores[session_id] = store
            return store

    async def _staged(self, folder: _Folder, policy: PolicyClient, tracer: Tracer,
                      clock: Clock) -> Seed:
        # A fresh folder per attempt: a failed staging is removed, and the next request
        # stages again instead of finding the old attempt's folder.
        seeding = folder.path / f"seed-{secrets.token_hex(8)}"
        seeding.mkdir()
        try:
            # Staging imports the whole history; it runs off the event loop.
            return await anyio.to_thread.run_sync(self._stage, seeding, policy, tracer, clock)
        except BaseException:
            shutil.rmtree(seeding, ignore_errors=True)
            raise

    @contextmanager
    def lease(self, store: SeededStore) -> Iterator[SeededStore]:
        """Hold a store while a request uses it. Take the lease before the first await."""
        store.leases += 1
        try:
            yield store
        finally:
            store.leases -= 1
            if store.released and store.leases == 0:
                _delete(store)
                self._held.discard(store)
                store.folder.users -= 1
                self._cleanup(store.folder)

    def release(self, session_id: str) -> None:
        """Take a session's store out of use and delete its files, once no request holds
        it: the database, its write-ahead log and its shared memory file."""
        store = self._stores.pop(session_id, None)
        if store is None:
            return
        store.released = True
        if store.leases == 0:
            _delete(store)
        else:
            self._held.add(store)
            store.folder.users += 1

    def close(self) -> None:
        """Release every store and end the folder's generation. The folder is removed
        once no request holds a store in it and no staging or copy in it is under way;
        those are never registered. A later open stages again in a new folder."""
        for session_id in list(self._stores):
            self.release(session_id)
        if self._folder is not None:
            folder, self._folder = self._folder, None
            folder.closed = True
            self._cleanup(folder)

    @staticmethod
    def _cleanup(folder: _Folder) -> None:
        if folder.closed and folder.users == 0:
            folder.directory.cleanup()
