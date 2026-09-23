"""SQLite registry and hash-chained append-only event log for P0."""

from __future__ import annotations

import hashlib
import json
import sqlite3
import uuid
from contextlib import contextmanager
from copy import deepcopy
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from types import MappingProxyType
from typing import Any, Iterator, Mapping

from .contracts import ContractLoader
from .lifecycle import LifecycleValidator


class StorageError(RuntimeError):
    """Base error for registry and event-log operations."""


class VersionConflictError(StorageError):
    """Raised when an immutable version or optimistic state check conflicts."""


class ObjectNotFoundError(StorageError):
    """Raised when a registry object does not exist."""


class EventIntegrityError(StorageError):
    """Raised when a stream no longer matches its append-only hash chain."""


def _utc_now() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds").replace("+00:00", "Z")


def _require_timestamp(value: str) -> str:
    if not isinstance(value, str) or not value:
        raise StorageError("timestamp must be a non-empty RFC 3339 string")
    try:
        parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError as exc:
        raise StorageError(f"invalid timestamp: {value}") from exc
    if parsed.tzinfo is None:
        raise StorageError("timestamp must include an offset or Z")
    return value


def _canonical_json(value: Any) -> str:
    try:
        return json.dumps(
            value,
            sort_keys=True,
            separators=(",", ":"),
            ensure_ascii=False,
            allow_nan=False,
        )
    except (TypeError, ValueError) as exc:
        raise StorageError(f"value is not canonical JSON data: {exc}") from exc


def _sha256_text(value: str) -> str:
    return hashlib.sha256(value.encode("utf-8")).hexdigest()


def _freeze_json(value: Any) -> Any:
    if isinstance(value, dict):
        return MappingProxyType({key: _freeze_json(item) for key, item in value.items()})
    if isinstance(value, list):
        return tuple(_freeze_json(item) for item in value)
    return value


@dataclass(frozen=True, slots=True)
class RegisteredObject:
    """Current registry projection plus the immutable current payload version."""

    object_id: str
    object_type: str
    current_version: str
    lifecycle_state: str
    lifecycle_state_at_registration: str
    content_hash: str
    payload: Mapping[str, Any]


@dataclass(frozen=True, slots=True)
class EventRecord:
    event_id: str
    stream_id: str
    stream_sequence: int
    actor: str
    event_type: str
    payload: Mapping[str, Any]
    payload_ref: str | None
    payload_hash: str
    previous_hash: str
    event_hash: str
    occurred_at: str


def _open_connection(database: str | Path) -> sqlite3.Connection:
    target = str(database)
    connection = sqlite3.connect(target, timeout=30, isolation_level=None)
    connection.row_factory = sqlite3.Row
    connection.execute("PRAGMA foreign_keys = ON")
    connection.execute("PRAGMA recursive_triggers = ON")
    connection.execute("PRAGMA busy_timeout = 5000")
    if target != ":memory:":
        connection.execute("PRAGMA journal_mode = WAL")
    return connection


@contextmanager
def _transaction(connection: sqlite3.Connection) -> Iterator[None]:
    connection.execute("BEGIN IMMEDIATE")
    try:
        yield
    except Exception:
        connection.execute("ROLLBACK")
        raise
    else:
        connection.execute("COMMIT")


class EventLog:
    """Append-only, per-stream hash-chained SQLite event log."""

    def __init__(
        self,
        database: str | Path = ":memory:",
        *,
        _connection: sqlite3.Connection | None = None,
    ):
        self.database = database
        self._owns_connection = _connection is None
        self._connection = _connection or _open_connection(database)
        self._initialize_schema()

    def _initialize_schema(self) -> None:
        self._connection.executescript(
            """
            CREATE TABLE IF NOT EXISTS events (
                event_id TEXT PRIMARY KEY,
                stream_id TEXT NOT NULL,
                stream_sequence INTEGER NOT NULL CHECK (stream_sequence > 0),
                actor TEXT NOT NULL CHECK (length(actor) > 0),
                event_type TEXT NOT NULL CHECK (length(event_type) > 0),
                payload_json TEXT NOT NULL,
                payload_ref TEXT,
                payload_hash TEXT NOT NULL CHECK (length(payload_hash) = 64),
                previous_hash TEXT NOT NULL,
                event_hash TEXT NOT NULL UNIQUE CHECK (length(event_hash) = 64),
                occurred_at TEXT NOT NULL,
                UNIQUE (stream_id, stream_sequence)
            );

            CREATE TABLE IF NOT EXISTS event_stream_heads (
                stream_id TEXT PRIMARY KEY,
                last_sequence INTEGER NOT NULL,
                last_event_hash TEXT NOT NULL
            );

            CREATE TRIGGER IF NOT EXISTS event_stream_heads_no_delete
            BEFORE DELETE ON event_stream_heads
            BEGIN
                SELECT RAISE(ABORT, 'event stream heads cannot be deleted');
            END;

            CREATE TRIGGER IF NOT EXISTS events_no_update
            BEFORE UPDATE ON events
            BEGIN
                SELECT RAISE(ABORT, 'events are append-only');
            END;

            CREATE TRIGGER IF NOT EXISTS events_no_delete
            BEFORE DELETE ON events
            BEGIN
                SELECT RAISE(ABORT, 'events are append-only');
            END;

            CREATE TRIGGER IF NOT EXISTS events_sequence_guard
            BEFORE INSERT ON events
            WHEN NEW.stream_sequence != COALESCE(
                (SELECT last_sequence + 1 FROM event_stream_heads
                 WHERE stream_id = NEW.stream_id),
                1
            )
            BEGIN
                SELECT RAISE(ABORT, 'event stream sequence is not contiguous');
            END;

            CREATE TRIGGER IF NOT EXISTS events_previous_hash_guard
            BEFORE INSERT ON events
            WHEN NEW.previous_hash != COALESCE(
                (SELECT last_event_hash FROM event_stream_heads
                 WHERE stream_id = NEW.stream_id),
                '0000000000000000000000000000000000000000000000000000000000000000'
            )
            BEGIN
                SELECT RAISE(ABORT, 'event previous hash does not match stream head');
            END;
            """
        )

    @staticmethod
    def _event_hash(
        *,
        event_id: str,
        stream_id: str,
        stream_sequence: int,
        actor: str,
        event_type: str,
        payload_ref: str | None,
        payload_hash: str,
        previous_hash: str,
        occurred_at: str,
    ) -> str:
        header = {
            "actor": actor,
            "event_id": event_id,
            "event_type": event_type,
            "occurred_at": occurred_at,
            "payload_hash": payload_hash,
            "payload_ref": payload_ref,
            "previous_hash": previous_hash,
            "stream_id": stream_id,
            "stream_sequence": stream_sequence,
        }
        return _sha256_text(_canonical_json(header))

    def append(
        self,
        *,
        stream_id: str,
        actor: str,
        event_type: str,
        payload: Mapping[str, Any],
        payload_ref: str | None = None,
        event_id: str | None = None,
        occurred_at: str | None = None,
    ) -> EventRecord:
        with _transaction(self._connection):
            return self._append_in_transaction(
                stream_id=stream_id,
                actor=actor,
                event_type=event_type,
                payload=payload,
                payload_ref=payload_ref,
                event_id=event_id,
                occurred_at=occurred_at,
            )

    def _append_in_transaction(
        self,
        *,
        stream_id: str,
        actor: str,
        event_type: str,
        payload: Mapping[str, Any],
        payload_ref: str | None = None,
        event_id: str | None = None,
        occurred_at: str | None = None,
    ) -> EventRecord:
        if not isinstance(stream_id, str) or not stream_id:
            raise StorageError("stream_id must be a non-empty string")
        if not isinstance(actor, str) or not actor:
            raise StorageError("actor must be a non-empty string")
        if not isinstance(event_type, str) or not event_type:
            raise StorageError("event_type must be a non-empty string")
        if not isinstance(payload, Mapping):
            raise StorageError("event payload must be a mapping")
        if payload_ref is not None and not isinstance(payload_ref, str):
            raise StorageError("payload_ref must be a string or None")

        event_id = event_id or str(uuid.uuid4())
        occurred_at = _require_timestamp(occurred_at or _utc_now())
        payload_copy = deepcopy(dict(payload))
        payload_json = _canonical_json(payload_copy)
        payload_hash = _sha256_text(payload_json)
        head = self._connection.execute(
            "SELECT last_sequence, last_event_hash FROM event_stream_heads WHERE stream_id = ?",
            (stream_id,),
        ).fetchone()
        stream_sequence = 1 if head is None else int(head["last_sequence"]) + 1
        previous_hash = "0" * 64 if head is None else str(head["last_event_hash"])
        event_hash = self._event_hash(
            event_id=event_id,
            stream_id=stream_id,
            stream_sequence=stream_sequence,
            actor=actor,
            event_type=event_type,
            payload_ref=payload_ref,
            payload_hash=payload_hash,
            previous_hash=previous_hash,
            occurred_at=occurred_at,
        )
        try:
            self._connection.execute(
                """
                INSERT INTO events (
                    event_id, stream_id, stream_sequence, actor, event_type,
                    payload_json, payload_ref, payload_hash, previous_hash,
                    event_hash, occurred_at
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    event_id,
                    stream_id,
                    stream_sequence,
                    actor,
                    event_type,
                    payload_json,
                    payload_ref,
                    payload_hash,
                    previous_hash,
                    event_hash,
                    occurred_at,
                ),
            )
            self._connection.execute(
                """
                INSERT INTO event_stream_heads (stream_id, last_sequence, last_event_hash)
                VALUES (?, ?, ?)
                ON CONFLICT(stream_id) DO UPDATE SET
                    last_sequence = excluded.last_sequence,
                    last_event_hash = excluded.last_event_hash
                """,
                (stream_id, stream_sequence, event_hash),
            )
        except sqlite3.IntegrityError as exc:
            raise StorageError(f"event append failed: {exc}") from exc
        return EventRecord(
            event_id=event_id,
            stream_id=stream_id,
            stream_sequence=stream_sequence,
            actor=actor,
            event_type=event_type,
            payload=_freeze_json(payload_copy),
            payload_ref=payload_ref,
            payload_hash=payload_hash,
            previous_hash=previous_hash,
            event_hash=event_hash,
            occurred_at=occurred_at,
        )

    def read_stream(
        self,
        stream_id: str,
        *,
        after_sequence: int = 0,
        limit: int | None = None,
    ) -> tuple[EventRecord, ...]:
        if after_sequence < 0:
            raise StorageError("after_sequence must be non-negative")
        if limit is not None and limit <= 0:
            raise StorageError("limit must be positive")
        query = """
            SELECT * FROM events
            WHERE stream_id = ? AND stream_sequence > ?
            ORDER BY stream_sequence
        """
        parameters: tuple[Any, ...] = (stream_id, after_sequence)
        if limit is not None:
            query += " LIMIT ?"
            parameters = (*parameters, limit)
        rows = self._connection.execute(query, parameters).fetchall()
        return tuple(self._row_to_event(row) for row in rows)

    def stream_ids(self) -> tuple[str, ...]:
        rows = self._connection.execute(
            """
            SELECT stream_id FROM events
            UNION
            SELECT stream_id FROM event_stream_heads
            ORDER BY stream_id
            """
        ).fetchall()
        return tuple(row["stream_id"] for row in rows)

    @staticmethod
    def _row_to_event(row: sqlite3.Row) -> EventRecord:
        payload = json.loads(row["payload_json"])
        return EventRecord(
            event_id=row["event_id"],
            stream_id=row["stream_id"],
            stream_sequence=row["stream_sequence"],
            actor=row["actor"],
            event_type=row["event_type"],
            payload=_freeze_json(payload),
            payload_ref=row["payload_ref"],
            payload_hash=row["payload_hash"],
            previous_hash=row["previous_hash"],
            event_hash=row["event_hash"],
            occurred_at=row["occurred_at"],
        )

    def verify_stream(self, stream_id: str) -> int:
        rows = self._connection.execute(
            "SELECT * FROM events WHERE stream_id = ? ORDER BY stream_sequence",
            (stream_id,),
        ).fetchall()
        previous_hash = "0" * 64
        for expected_sequence, row in enumerate(rows, start=1):
            if row["stream_sequence"] != expected_sequence:
                raise EventIntegrityError(
                    f"stream {stream_id} has non-contiguous sequence at {expected_sequence}"
                )
            payload_json = _canonical_json(json.loads(row["payload_json"]))
            payload_hash = _sha256_text(payload_json)
            if payload_hash != row["payload_hash"]:
                raise EventIntegrityError(
                    f"stream {stream_id} payload hash mismatch at {expected_sequence}"
                )
            if row["previous_hash"] != previous_hash:
                raise EventIntegrityError(
                    f"stream {stream_id} previous hash mismatch at {expected_sequence}"
                )
            event_hash = self._event_hash(
                event_id=row["event_id"],
                stream_id=row["stream_id"],
                stream_sequence=row["stream_sequence"],
                actor=row["actor"],
                event_type=row["event_type"],
                payload_ref=row["payload_ref"],
                payload_hash=row["payload_hash"],
                previous_hash=row["previous_hash"],
                occurred_at=row["occurred_at"],
            )
            if event_hash != row["event_hash"]:
                raise EventIntegrityError(
                    f"stream {stream_id} event hash mismatch at {expected_sequence}"
                )
            previous_hash = event_hash

        head = self._connection.execute(
            "SELECT last_sequence, last_event_hash FROM event_stream_heads WHERE stream_id = ?",
            (stream_id,),
        ).fetchone()
        if rows:
            if head is None or head["last_sequence"] != len(rows) or head["last_event_hash"] != previous_hash:
                raise EventIntegrityError(f"stream {stream_id} head projection mismatch")
        elif head is not None:
            raise EventIntegrityError(f"stream {stream_id} has a head without events")
        return len(rows)

    def verify_all_streams(self) -> int:
        required_triggers = {
            "events_no_update",
            "events_no_delete",
            "events_sequence_guard",
            "events_previous_hash_guard",
            "event_stream_heads_no_delete",
        }
        present_triggers = {
            str(row["name"])
            for row in self._connection.execute(
                "SELECT name FROM sqlite_master WHERE type = 'trigger'"
            ).fetchall()
        }
        missing_triggers = sorted(required_triggers - present_triggers)
        if missing_triggers:
            raise EventIntegrityError(
                "event-log integrity triggers missing: " + ", ".join(missing_triggers)
            )
        total = 0
        for stream_id in self.stream_ids():
            total += self.verify_stream(stream_id)
        return total

    def close(self) -> None:
        if self._owns_connection:
            self._connection.close()

    def __enter__(self) -> EventLog:
        return self

    def __exit__(self, *_: object) -> None:
        self.close()


class Registry:
    """Immutable-version object registry with lifecycle projection and events."""

    _INITIAL_LIFECYCLE_STATES = frozenset({"raw", "quarantine", "candidate"})

    def __init__(
        self,
        database: str | Path,
        contracts: ContractLoader,
        lifecycle: LifecycleValidator,
    ):
        self.database = database
        self.contracts = contracts
        self.lifecycle = lifecycle
        self._connection = _open_connection(database)
        self.events = EventLog(database, _connection=self._connection)
        self._initialize_schema()

    def _initialize_schema(self) -> None:
        self._connection.executescript(
            """
            CREATE TABLE IF NOT EXISTS objects (
                object_id TEXT PRIMARY KEY,
                object_type TEXT NOT NULL,
                current_version TEXT NOT NULL,
                lifecycle_state TEXT NOT NULL,
                content_hash TEXT NOT NULL CHECK (length(content_hash) = 64),
                created_at TEXT NOT NULL,
                updated_at TEXT NOT NULL
            );

            CREATE TRIGGER IF NOT EXISTS objects_no_delete
            BEFORE DELETE ON objects
            BEGIN
                SELECT RAISE(ABORT, 'registry object projections cannot be deleted');
            END;

            CREATE TABLE IF NOT EXISTS object_versions (
                object_id TEXT NOT NULL,
                version TEXT NOT NULL,
                object_type TEXT NOT NULL,
                lifecycle_state_at_write TEXT NOT NULL,
                payload_json TEXT NOT NULL,
                content_hash TEXT NOT NULL CHECK (length(content_hash) = 64),
                created_at TEXT NOT NULL,
                registration_event_id TEXT NOT NULL UNIQUE,
                PRIMARY KEY (object_id, version),
                FOREIGN KEY (object_id) REFERENCES objects(object_id),
                FOREIGN KEY (registration_event_id) REFERENCES events(event_id)
            );

            CREATE TABLE IF NOT EXISTS lifecycle_transitions (
                transition_id TEXT PRIMARY KEY,
                object_id TEXT NOT NULL,
                from_state TEXT NOT NULL,
                to_state TEXT NOT NULL,
                requirement_tokens_json TEXT NOT NULL,
                actor TEXT NOT NULL,
                occurred_at TEXT NOT NULL,
                event_id TEXT NOT NULL UNIQUE,
                FOREIGN KEY (object_id) REFERENCES objects(object_id),
                FOREIGN KEY (event_id) REFERENCES events(event_id)
            );

            CREATE TRIGGER IF NOT EXISTS object_versions_no_update
            BEFORE UPDATE ON object_versions
            BEGIN
                SELECT RAISE(ABORT, 'object versions are immutable');
            END;

            CREATE TRIGGER IF NOT EXISTS object_versions_no_delete
            BEFORE DELETE ON object_versions
            BEGIN
                SELECT RAISE(ABORT, 'object versions are immutable');
            END;

            CREATE TRIGGER IF NOT EXISTS lifecycle_transitions_no_update
            BEFORE UPDATE ON lifecycle_transitions
            BEGIN
                SELECT RAISE(ABORT, 'lifecycle transitions are append-only');
            END;

            CREATE TRIGGER IF NOT EXISTS lifecycle_transitions_no_insert
            BEFORE INSERT ON lifecycle_transitions
            BEGIN
                SELECT RAISE(ABORT, 'P0 cannot authorize lifecycle transitions');
            END;

            CREATE TRIGGER IF NOT EXISTS lifecycle_transitions_no_delete
            BEFORE DELETE ON lifecycle_transitions
            BEGIN
                SELECT RAISE(ABORT, 'lifecycle transitions are append-only');
            END;

            PRAGMA user_version = 1;
            """
        )

    def put_new_version(
        self,
        payload: Mapping[str, Any],
        *,
        actor: str | None = None,
        expected_current_version: str | None = None,
    ) -> RegisteredObject:
        if not isinstance(payload, Mapping):
            raise StorageError("registry payload must be a mapping")
        payload_copy = deepcopy(dict(payload))
        object_id = payload_copy.get("object_id")
        object_type = payload_copy.get("object_type")
        version = payload_copy.get("version")
        lifecycle_state = payload_copy.get("lifecycle_state")
        created_at = payload_copy.get("created_at")
        for value, label in (
            (object_id, "object_id"),
            (object_type, "object_type"),
            (version, "version"),
            (lifecycle_state, "lifecycle_state"),
            (created_at, "created_at"),
        ):
            if not isinstance(value, str) or not value:
                raise StorageError(f"registry payload requires non-empty {label}")
        self.contracts.validate_instance(object_type, payload_copy)
        _require_timestamp(created_at)
        actor = actor or str(payload_copy.get("created_by") or "unknown")
        payload_json = _canonical_json(payload_copy)
        content_hash = _sha256_text(payload_json)
        now = _utc_now()

        try:
            with _transaction(self._connection):
                existing = self._connection.execute(
                    "SELECT * FROM objects WHERE object_id = ?", (object_id,)
                ).fetchone()
                if existing is None:
                    if expected_current_version is not None:
                        raise VersionConflictError(
                            f"object {object_id} has no current version to match"
                        )
                    if lifecycle_state not in self._INITIAL_LIFECYCLE_STATES:
                        raise VersionConflictError(
                            f"new object cannot begin with authoritative lifecycle state {lifecycle_state}"
                        )
                else:
                    if expected_current_version is None:
                        raise VersionConflictError(
                            f"existing object {object_id} requires expected_current_version"
                        )
                    if existing["current_version"] != expected_current_version:
                        raise VersionConflictError(
                            f"object {object_id} current version is "
                            f"{existing['current_version']}, expected {expected_current_version}"
                        )
                    if existing["object_type"] != object_type:
                        raise VersionConflictError(
                            f"object {object_id} cannot change object_type"
                        )
                    if existing["lifecycle_state"] != lifecycle_state:
                        raise VersionConflictError(
                            "put_new_version cannot change lifecycle; use set_lifecycle"
                        )

                registration_event = self.events._append_in_transaction(
                    stream_id=f"object:{object_id}",
                    actor=actor,
                    event_type="registry.object_version_created",
                    payload={
                        "object_id": object_id,
                        "object_type": object_type,
                        "version": version,
                        "content_hash": content_hash,
                    },
                    payload_ref=f"registry:{object_id}@{version}",
                    occurred_at=now,
                )

                if existing is None:
                    self._connection.execute(
                        """
                        INSERT INTO objects (
                            object_id, object_type, current_version, lifecycle_state,
                            content_hash, created_at, updated_at
                        ) VALUES (?, ?, ?, ?, ?, ?, ?)
                        """,
                        (
                            object_id,
                            object_type,
                            version,
                            lifecycle_state,
                            content_hash,
                            created_at,
                            now,
                        ),
                    )

                self._connection.execute(
                    """
                    INSERT INTO object_versions (
                        object_id, version, object_type, lifecycle_state_at_write,
                        payload_json, content_hash, created_at, registration_event_id
                    ) VALUES (?, ?, ?, ?, ?, ?, ?, ?)
                    """,
                    (
                        object_id,
                        version,
                        object_type,
                        lifecycle_state,
                        payload_json,
                        content_hash,
                        created_at,
                        registration_event.event_id,
                    ),
                )
                if existing is not None:
                    self._connection.execute(
                        """
                        UPDATE objects
                        SET current_version = ?, content_hash = ?, updated_at = ?
                        WHERE object_id = ?
                        """,
                        (version, content_hash, now, object_id),
                    )
        except sqlite3.IntegrityError as exc:
            raise VersionConflictError(f"immutable version insert failed: {exc}") from exc
        return self.get_object(object_id)

    def get_object(self, object_id: str) -> RegisteredObject:
        row = self._connection.execute(
            """
            SELECT o.object_id, o.object_type, o.current_version,
                   o.lifecycle_state, o.content_hash, v.lifecycle_state_at_write,
                   v.payload_json
            FROM objects AS o
            JOIN object_versions AS v
              ON v.object_id = o.object_id AND v.version = o.current_version
            WHERE o.object_id = ?
            """,
            (object_id,),
        ).fetchone()
        if row is None:
            raise ObjectNotFoundError(f"object not found: {object_id}")
        return RegisteredObject(
            object_id=row["object_id"],
            object_type=row["object_type"],
            current_version=row["current_version"],
            lifecycle_state=row["lifecycle_state"],
            lifecycle_state_at_registration=row["lifecycle_state_at_write"],
            content_hash=row["content_hash"],
            payload=_freeze_json(json.loads(row["payload_json"])),
        )

    def get_version(self, object_id: str, version: str) -> Mapping[str, Any]:
        row = self._connection.execute(
            "SELECT payload_json FROM object_versions WHERE object_id = ? AND version = ?",
            (object_id, version),
        ).fetchone()
        if row is None:
            raise ObjectNotFoundError(f"object version not found: {object_id}@{version}")
        return _freeze_json(json.loads(row["payload_json"]))

    def list_versions(self, object_id: str) -> tuple[str, ...]:
        rows = self._connection.execute(
            "SELECT version FROM object_versions WHERE object_id = ? ORDER BY rowid",
            (object_id,),
        ).fetchall()
        if not rows:
            raise ObjectNotFoundError(f"object not found: {object_id}")
        return tuple(row["version"] for row in rows)

    def set_lifecycle(
        self,
        object_id: str,
        to_state: str,
        *,
        evidence: Mapping[str, Any],
        actor: str,
        expected_from_state: str,
        occurred_at: str | None = None,
    ) -> RegisteredObject:
        """Reject durable promotions until an independent authority exists.

        The P0 lifecycle component can establish that a proposed edge names all
        policy requirements, but it cannot verify the referenced evidence,
        verifier independence, or owner/delegated canonical authority.  Caller
        assertions therefore never become registry state in P0.
        """

        if not actor:
            raise StorageError("lifecycle actor must be non-empty")
        _require_timestamp(occurred_at or _utc_now())
        row = self._connection.execute(
            "SELECT lifecycle_state FROM objects WHERE object_id = ?", (object_id,)
        ).fetchone()
        if row is None:
            raise ObjectNotFoundError(f"object not found: {object_id}")
        from_state = str(row["lifecycle_state"])
        if from_state != expected_from_state:
            raise VersionConflictError(
                f"object {object_id} lifecycle is {from_state}, expected {expected_from_state}"
            )
        # Always raises in P0, even when the declared requirement names are
        # structurally complete.  No event or projection write occurs.
        self.lifecycle.require_transition(from_state, to_state, evidence)
        raise AssertionError("P0 lifecycle validator cannot authorize a transition")

    def lifecycle_history(self, object_id: str) -> tuple[Mapping[str, Any], ...]:
        rows = self._connection.execute(
            """
            SELECT * FROM lifecycle_transitions
            WHERE object_id = ? ORDER BY rowid
            """,
            (object_id,),
        ).fetchall()
        return tuple(
            _freeze_json(
                {
                    "transition_id": row["transition_id"],
                    "object_id": row["object_id"],
                    "from_state": row["from_state"],
                    "to_state": row["to_state"],
                    "requirement_tokens": json.loads(row["requirement_tokens_json"]),
                    "actor": row["actor"],
                    "occurred_at": row["occurred_at"],
                    "event_id": row["event_id"],
                }
            )
            for row in rows
        )

    def replay_object(self, object_id: str) -> tuple[str, str]:
        object_row = self._connection.execute(
            "SELECT * FROM objects WHERE object_id = ?", (object_id,)
        ).fetchone()
        if object_row is None:
            raise ObjectNotFoundError(f"object not found: {object_id}")
        version_rows = self._connection.execute(
            "SELECT * FROM object_versions WHERE object_id = ? ORDER BY rowid",
            (object_id,),
        ).fetchall()
        transition_rows = self._connection.execute(
            "SELECT * FROM lifecycle_transitions WHERE object_id = ? ORDER BY rowid",
            (object_id,),
        ).fetchall()
        versions_by_event = {row["registration_event_id"]: row for row in version_rows}
        transitions_by_event = {row["event_id"]: row for row in transition_rows}
        seen_versions: set[str] = set()
        seen_transitions: set[str] = set()
        current_state: str | None = None
        current_version: str | None = None
        for event in self.events.read_stream(f"object:{object_id}"):
            version_row = versions_by_event.get(event.event_id)
            if event.event_type == "registry.object_version_created":
                if version_row is None:
                    raise StorageError(
                        f"object {object_id} has a version event without an immutable version"
                    )
                registration_state = str(version_row["lifecycle_state_at_write"])
                if current_state is None:
                    current_state = registration_state
                elif registration_state != current_state:
                    raise StorageError(
                        f"object {object_id} version {version_row['version']} bypassed lifecycle history"
                    )
                current_version = str(version_row["version"])
                seen_versions.add(event.event_id)
                continue
            transition_row = transitions_by_event.get(event.event_id)
            if event.event_type == "registry.lifecycle_transition":
                if transition_row is None:
                    raise StorageError(
                        f"object {object_id} has a transition event without transition history"
                    )
                if current_state != transition_row["from_state"]:
                    raise StorageError(
                        f"object {object_id} transition history does not replay"
                    )
                current_state = str(transition_row["to_state"])
                seen_transitions.add(event.event_id)
                continue
            raise StorageError(
                f"object {object_id} has unexpected registry stream event type: "
                f"{event.event_type}"
            )

        if seen_versions != set(versions_by_event):
            raise StorageError(f"object {object_id} has versions without registration events")
        if seen_transitions != set(transitions_by_event):
            raise StorageError(f"object {object_id} has transitions without events")
        if current_version is None or current_state is None:
            raise StorageError(f"object {object_id} has no replayable registration")
        return current_version, current_state

    def verify_integrity(self) -> int:
        sqlite_result = self._connection.execute("PRAGMA integrity_check").fetchone()[0]
        if sqlite_result != "ok":
            raise StorageError(f"SQLite integrity check failed: {sqlite_result}")
        foreign_key_rows = self._connection.execute("PRAGMA foreign_key_check").fetchall()
        if foreign_key_rows:
            details = ", ".join(
                f"{row['table']}[{row['rowid']}] -> {row['parent']}"
                for row in foreign_key_rows
            )
            raise StorageError(f"SQLite foreign-key check failed: {details}")

        required_registry_triggers = {
            "objects_no_delete",
            "object_versions_no_update",
            "object_versions_no_delete",
            "lifecycle_transitions_no_insert",
            "lifecycle_transitions_no_update",
            "lifecycle_transitions_no_delete",
        }
        present_triggers = {
            str(row["name"])
            for row in self._connection.execute(
                "SELECT name FROM sqlite_master WHERE type = 'trigger'"
            ).fetchall()
        }
        missing_registry_triggers = sorted(required_registry_triggers - present_triggers)
        if missing_registry_triggers:
            raise StorageError(
                "registry integrity triggers missing: "
                + ", ".join(missing_registry_triggers)
            )

        object_rows = self._connection.execute(
            "SELECT * FROM objects ORDER BY object_id"
        ).fetchall()
        projection_ids = {str(row["object_id"]) for row in object_rows}
        version_ids = {
            str(row["object_id"])
            for row in self._connection.execute(
                "SELECT DISTINCT object_id FROM object_versions"
            ).fetchall()
        }
        transition_ids = {
            str(row["object_id"])
            for row in self._connection.execute(
                "SELECT DISTINCT object_id FROM lifecycle_transitions"
            ).fetchall()
        }
        if projection_ids != version_ids:
            missing_projections = sorted(version_ids - projection_ids)
            empty_projections = sorted(projection_ids - version_ids)
            raise StorageError(
                "registry object/version projection mismatch: "
                f"missing_objects={missing_projections}, objects_without_versions={empty_projections}"
            )
        orphan_transition_ids = sorted(transition_ids - projection_ids)
        if orphan_transition_ids:
            raise StorageError(
                "lifecycle transitions reference missing objects: "
                + ", ".join(orphan_transition_ids)
            )
        for object_row in object_rows:
            object_id = str(object_row["object_id"])
            self.events.verify_stream(f"object:{object_id}")
            version_rows = self._connection.execute(
                "SELECT * FROM object_versions WHERE object_id = ? ORDER BY rowid",
                (object_id,),
            ).fetchall()
            if not version_rows:
                raise StorageError(f"object {object_id} has no immutable versions")
            for version_row in version_rows:
                payload = json.loads(version_row["payload_json"])
                content_hash = _sha256_text(_canonical_json(payload))
                if content_hash != version_row["content_hash"]:
                    raise StorageError(
                        f"object {object_id}@{version_row['version']} content hash mismatch"
                    )
                if (
                    payload.get("object_id") != object_id
                    or payload.get("object_type") != version_row["object_type"]
                    or payload.get("version") != version_row["version"]
                    or payload.get("lifecycle_state")
                    != version_row["lifecycle_state_at_write"]
                ):
                    raise StorageError(
                        f"object {object_id}@{version_row['version']} metadata mismatch"
                    )
                self.contracts.validate_instance(version_row["object_type"], payload)
            replayed_version, replayed_state = self.replay_object(object_id)
            if replayed_version != object_row["current_version"]:
                raise StorageError(f"object {object_id} current-version projection mismatch")
            if replayed_state != object_row["lifecycle_state"]:
                raise StorageError(f"object {object_id} lifecycle projection mismatch")
            if version_rows[-1]["content_hash"] != object_row["content_hash"]:
                raise StorageError(f"object {object_id} content-hash projection mismatch")
        self.events.verify_all_streams()
        return len(object_rows)

    def close(self) -> None:
        self._connection.close()

    def __enter__(self) -> Registry:
        return self

    def __exit__(self, *_: object) -> None:
        self.close()
