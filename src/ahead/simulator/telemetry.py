"""Persistent, timestamped snapshots for the simulator's diagnostic export."""

from contextlib import closing, contextmanager
from pathlib import Path
import sqlite3
from time import time_ns
from zlib import compress

DEFAULT_INTERVAL_S = 15


class TelemetryArchive:
    def __init__(self, path: Path, interval_s: int):
        if interval_s < 1:
            raise ValueError("AHEAD_ARCHIVE_INTERVAL_S must be a positive whole number")
        self.path = path
        self.interval_s = interval_s
        self.error = None
        self._initialized = False

    @contextmanager
    def connect(self):
        with closing(sqlite3.connect(self.path, timeout=2)) as db:
            yield db

    def record(self, run_id, elapsed_s, snapshot):
        captured_ms = time_ns() // 1_000_000
        try:
            self.path.parent.mkdir(parents=True, exist_ok=True)
            with self.connect() as db, db:
                if not self._initialized:
                    db.execute("PRAGMA journal_mode=WAL")
                    db.execute("""
                        CREATE TABLE IF NOT EXISTS snapshots (
                            captured_ms INTEGER NOT NULL,
                            run_id TEXT NOT NULL,
                            elapsed_s REAL NOT NULL,
                            state BLOB NOT NULL
                        )
                    """)
                    db.execute("CREATE INDEX IF NOT EXISTS snapshots_time ON snapshots(captured_ms)")
                    db.commit()
                    self._initialized = True
                db.execute("INSERT INTO snapshots VALUES (?, ?, ?, ?)",
                           (captured_ms, run_id, elapsed_s, compress(snapshot, level=1)))
            self.error = None
        except (OSError, sqlite3.Error) as error:
            self._initialized = False
            self.error = str(error)

    def status(self):
        first = last = None
        if self.path.is_file():
            with self.connect() as db:
                first = db.execute("SELECT min(captured_ms) FROM snapshots").fetchone()[0]
                last = db.execute("SELECT max(captured_ms) FROM snapshots").fetchone()[0]
        size = 0
        for path in (self.path, self.path.with_name(self.path.name + "-wal")):
            try:
                size += path.stat().st_size
            except FileNotFoundError:
                pass
        return {"first_ms": first, "last_ms": last, "interval_s": self.interval_s,
                "size_bytes": size, "error": self.error}

    @contextmanager
    def range(self, start_ms, end_ms):
        with self.connect() as db, closing(db.execute("""
            SELECT captured_ms, run_id, elapsed_s, state FROM snapshots
            WHERE captured_ms BETWEEN ? AND ? ORDER BY captured_ms, rowid
        """, (start_ms, end_ms))) as rows:
            yield rows
