"""Atomic feed caching and persistent first-seen history, using stdlib SQLite."""

import hashlib
import json
import sqlite3
from contextlib import closing
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path

from bugalert.feed import FeedData, parse_feed


@dataclass(frozen=True, slots=True)
class Observation:
    first_seen: str
    updated_at: str
    baseline: bool


@dataclass(frozen=True, slots=True)
class StoredFeed:
    feed: FeedData
    fetched_at: str
    source: str
    observations: dict[int, Observation]


class StateStore:
    def __init__(self, path: Path):
        self.path = path

    def initialize(self) -> None:
        self.path.parent.mkdir(parents=True, exist_ok=True)
        with closing(sqlite3.connect(self.path, timeout=10)) as db, db:
            db.execute("PRAGMA journal_mode=WAL")
            db.execute(
                "CREATE TABLE IF NOT EXISTS snapshot ("
                "id INTEGER PRIMARY KEY CHECK (id = 1), payload TEXT NOT NULL, "
                "fetched_at TEXT NOT NULL, source TEXT NOT NULL)"
            )
            db.execute(
                "CREATE TABLE IF NOT EXISTS observations ("
                "bug_id INTEGER PRIMARY KEY, first_seen TEXT NOT NULL, updated_at TEXT NOT NULL, "
                "fingerprint TEXT NOT NULL, baseline INTEGER NOT NULL)"
            )

    @staticmethod
    def _observations(db: sqlite3.Connection) -> dict[int, Observation]:
        return {
            row[0]: Observation(row[1], row[2], bool(row[3]))
            for row in db.execute(
                "SELECT bug_id, first_seen, updated_at, baseline FROM observations"
            )
        }

    def load(self) -> StoredFeed | None:
        with closing(sqlite3.connect(self.path, timeout=10)) as db:
            row = db.execute(
                "SELECT payload, fetched_at, source FROM snapshot WHERE id = 1"
            ).fetchone()
            if row is None:
                return None
            return StoredFeed(parse_feed(row[0]), row[1], row[2], self._observations(db))

    def save(self, feed: FeedData, now: datetime, source: str) -> StoredFeed:
        timestamp = now.isoformat()
        with closing(sqlite3.connect(self.path, timeout=10)) as db, db:
            baseline = db.execute("SELECT 1 FROM snapshot WHERE id = 1").fetchone() is None
            existing = dict(db.execute("SELECT bug_id, fingerprint FROM observations"))
            for bug in feed.bugs:
                if bug["product"].casefold() != "eos":
                    continue
                fingerprint = hashlib.sha256(
                    json.dumps(bug, sort_keys=True, ensure_ascii=False).encode()
                ).hexdigest()
                if bug["bugId"] not in existing:
                    db.execute(
                        "INSERT INTO observations VALUES (?, ?, ?, ?, ?)",
                        (bug["bugId"], timestamp, timestamp, fingerprint, int(baseline)),
                    )
                elif existing[bug["bugId"]] != fingerprint:
                    db.execute(
                        "UPDATE observations SET updated_at = ?, fingerprint = ? WHERE bug_id = ?",
                        (timestamp, fingerprint, bug["bugId"]),
                    )
            db.execute(
                "INSERT INTO snapshot VALUES (1, ?, ?, ?) ON CONFLICT(id) DO UPDATE SET "
                "payload = excluded.payload, fetched_at = excluded.fetched_at, "
                "source = excluded.source",
                (json.dumps(feed.payload, ensure_ascii=False), timestamp, source),
            )
            observations = self._observations(db)
        return StoredFeed(feed, timestamp, source, observations)
