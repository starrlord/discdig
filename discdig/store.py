"""On-disk state: config file plus a SQLite queue that survives restarts.

The queue is the durable record; the downloader keeps a parallel in-memory view
for per-frame progress so the UI never waits on disk.  Rows are flushed on state
transitions and throttled during transfer.
"""

from __future__ import annotations

import json
import os
import sqlite3
import time
from dataclasses import asdict, dataclass, field
from pathlib import Path
from typing import Any, Iterable

APP_DIR = Path(os.environ.get("DISCDIG_HOME") or (Path.home() / ".discdig"))
CONFIG_PATH = APP_DIR / "config.json"
DB_PATH = APP_DIR / "queue.db"

# Task states.  queued -> running -> done | failed | paused
QUEUED, RUNNING, PAUSED, DONE, FAILED = "queued", "running", "paused", "done", "failed"
ACTIVE_STATES = (QUEUED, RUNNING, PAUSED)

#: Theme a fresh install starts on, and the fallback when a saved theme has
#: gone away.  Defined here rather than in app.py so both the config default
#: and the app agree without store.py having to import the UI.
DEFAULT_THEME = "dracula"


def _default_download_dir() -> str:
    for candidate in (Path.home() / "Downloads", Path.home()):
        if candidate.is_dir():
            return str(candidate / "discmaster")
    return str(Path.cwd() / "discmaster")


@dataclass
class Config:
    download_dir: str = field(default_factory=_default_download_dir)
    #: One transfer at a time by default: DiscMaster is volunteer-run, and a
    #: single stream generally saturates the link anyway.  Raise it with `+` in
    #: the queue pane, or in Settings.
    concurrency: int = 1
    #: flat = filename only | item = <Item Title>/<inner path> | full = <itemid>/<inner path>
    layout: str = "item"
    verify: bool = True
    prefer_archive_org: bool = True
    theme: str = DEFAULT_THEME
    confirm_over_bytes: int = 2 * 1024**3
    max_attempts: int = 4
    recurse_limit: int = 2000
    safe_search: str = "2"  # discmaster nsfw param: 2=off 1=moderate 0=strict

    @classmethod
    def load(cls) -> "Config":
        try:
            data = json.loads(CONFIG_PATH.read_text("utf-8"))
        except (OSError, json.JSONDecodeError):
            return cls()
        known = {f for f in cls.__dataclass_fields__}
        return cls(**{k: v for k, v in data.items() if k in known})

    def save(self) -> None:
        APP_DIR.mkdir(parents=True, exist_ok=True)
        tmp = CONFIG_PATH.with_suffix(".tmp")
        tmp.write_text(json.dumps(asdict(self), indent=2), "utf-8")
        tmp.replace(CONFIG_PATH)


@dataclass
class Task:
    id: int = 0
    itemid: int = 0
    fileid: str = ""
    name: str = ""
    family: str = "other"
    item_name: str = ""
    dest: str = ""
    url: str = ""
    source: str = "discmaster"  # or "archive.org"
    expected_size: int | None = None
    size_exact: bool = False
    md5: str = ""
    status: str = QUEUED
    downloaded: int = 0
    total: int | None = None
    attempts: int = 0
    error: str = ""
    warn: str = ""
    added_at: float = 0.0
    started_at: float = 0.0
    finished_at: float = 0.0

    # transient, never persisted
    speed: float = 0.0
    eta: float | None = None

    @property
    def part_path(self) -> Path:
        return Path(self.dest + ".part")

    @property
    def path(self) -> Path:
        return Path(self.dest)

    @property
    def pct(self) -> float:
        total = self.total or self.expected_size
        if not total:
            return 0.0
        return min(100.0, self.downloaded * 100.0 / total)


_SCHEMA = """
CREATE TABLE IF NOT EXISTS tasks (
    id            INTEGER PRIMARY KEY AUTOINCREMENT,
    itemid        INTEGER NOT NULL,
    fileid        TEXT    NOT NULL,
    name          TEXT    NOT NULL,
    family        TEXT    NOT NULL DEFAULT 'other',
    item_name     TEXT    NOT NULL DEFAULT '',
    dest          TEXT    NOT NULL,
    url           TEXT    NOT NULL,
    source        TEXT    NOT NULL DEFAULT 'discmaster',
    expected_size INTEGER,
    size_exact    INTEGER NOT NULL DEFAULT 0,
    md5           TEXT    NOT NULL DEFAULT '',
    status        TEXT    NOT NULL DEFAULT 'queued',
    downloaded    INTEGER NOT NULL DEFAULT 0,
    total         INTEGER,
    attempts      INTEGER NOT NULL DEFAULT 0,
    error         TEXT    NOT NULL DEFAULT '',
    warn          TEXT    NOT NULL DEFAULT '',
    added_at      REAL    NOT NULL DEFAULT 0,
    started_at    REAL    NOT NULL DEFAULT 0,
    finished_at   REAL    NOT NULL DEFAULT 0
);
CREATE UNIQUE INDEX IF NOT EXISTS tasks_target ON tasks(itemid, fileid);
CREATE INDEX IF NOT EXISTS tasks_status ON tasks(status, id);

CREATE TABLE IF NOT EXISTS history (
    query   TEXT PRIMARY KEY,
    kind    TEXT NOT NULL DEFAULT 'search',
    used_at REAL NOT NULL
);

CREATE TABLE IF NOT EXISTS bookmarks (
    key     TEXT PRIMARY KEY,
    label   TEXT NOT NULL,
    payload TEXT NOT NULL,
    added_at REAL NOT NULL
);
"""

_COLS = [
    "id", "itemid", "fileid", "name", "family", "item_name", "dest", "url", "source",
    "expected_size", "size_exact", "md5", "status", "downloaded", "total",
    "attempts", "error", "warn", "added_at", "started_at", "finished_at",
]


class Store:
    """Thin SQLite wrapper.  All calls are synchronous and fast (local file)."""

    def __init__(self, path: Path = DB_PATH) -> None:
        path.parent.mkdir(parents=True, exist_ok=True)
        self.db = sqlite3.connect(path, check_same_thread=False)
        self.db.row_factory = sqlite3.Row
        self.db.executescript(_SCHEMA)
        self._migrate()
        self.db.execute("PRAGMA journal_mode=WAL")
        self.db.commit()

    def _migrate(self) -> None:
        """Add columns introduced after a database was first created."""
        have = {r["name"] for r in self.db.execute("PRAGMA table_info(tasks)")}
        for column, ddl in (("family", "TEXT NOT NULL DEFAULT 'other'"),):
            if column not in have:
                self.db.execute(f"ALTER TABLE tasks ADD COLUMN {column} {ddl}")
        self.db.commit()

    def close(self) -> None:
        self.db.close()

    # -- tasks -------------------------------------------------------------

    @staticmethod
    def _row_to_task(row: sqlite3.Row) -> Task:
        data = {k: row[k] for k in _COLS}
        data["size_exact"] = bool(data["size_exact"])
        return Task(**data)

    def add(self, task: Task) -> tuple[Task, bool]:
        """Insert a task.  Returns ``(task, created)``; re-queues a failed twin."""
        cur = self.db.execute(
            "SELECT * FROM tasks WHERE itemid=? AND fileid=?", (task.itemid, task.fileid)
        )
        row = cur.fetchone()
        if row is not None:
            existing = self._row_to_task(row)
            if existing.status == FAILED:
                self.update(existing.id, status=QUEUED, error="", attempts=0)
                existing.status, existing.error, existing.attempts = QUEUED, "", 0
                return existing, True
            return existing, False

        task.added_at = task.added_at or time.time()
        cols = [c for c in _COLS if c != "id"]
        placeholders = ", ".join("?" for _ in cols)
        values = [getattr(task, c) for c in cols]
        cur = self.db.execute(
            f"INSERT INTO tasks ({', '.join(cols)}) VALUES ({placeholders})", values
        )
        self.db.commit()
        task.id = int(cur.lastrowid or 0)
        return task, True

    def add_many(self, tasks: Iterable[Task]) -> tuple[list[Task], int]:
        created: list[Task] = []
        dupes = 0
        for t in tasks:
            task, made = self.add(t)
            if made:
                created.append(task)
            else:
                dupes += 1
        return created, dupes

    def update(self, task_id: int, **fields: Any) -> None:
        if not fields:
            return
        sets = ", ".join(f"{k}=?" for k in fields)
        self.db.execute(
            f"UPDATE tasks SET {sets} WHERE id=?", [*fields.values(), task_id]
        )
        self.db.commit()

    def get(self, task_id: int) -> Task | None:
        row = self.db.execute("SELECT * FROM tasks WHERE id=?", (task_id,)).fetchone()
        return self._row_to_task(row) if row else None

    def all(self, statuses: Iterable[str] | None = None) -> list[Task]:
        if statuses:
            marks = ", ".join("?" for _ in statuses)
            rows = self.db.execute(
                f"SELECT * FROM tasks WHERE status IN ({marks}) ORDER BY id", list(statuses)
            )
        else:
            rows = self.db.execute("SELECT * FROM tasks ORDER BY id")
        return [self._row_to_task(r) for r in rows]

    def remove(self, task_id: int) -> None:
        self.db.execute("DELETE FROM tasks WHERE id=?", (task_id,))
        self.db.commit()

    def clear(self, statuses: Iterable[str]) -> int:
        marks = ", ".join("?" for _ in statuses)
        cur = self.db.execute(f"DELETE FROM tasks WHERE status IN ({marks})", list(statuses))
        self.db.commit()
        return cur.rowcount

    def counts(self) -> dict[str, int]:
        rows = self.db.execute("SELECT status, COUNT(*) c FROM tasks GROUP BY status")
        return {r["status"]: r["c"] for r in rows}

    def have(self, itemid: int, fileid: str) -> Task | None:
        row = self.db.execute(
            "SELECT * FROM tasks WHERE itemid=? AND fileid=?", (itemid, fileid)
        ).fetchone()
        return self._row_to_task(row) if row else None

    # -- history / bookmarks -----------------------------------------------

    def remember(self, query: str, kind: str = "search") -> None:
        query = query.strip()
        if not query:
            return
        self.db.execute(
            "INSERT INTO history(query, kind, used_at) VALUES(?,?,?) "
            "ON CONFLICT(query) DO UPDATE SET used_at=excluded.used_at",
            (query, kind, time.time()),
        )
        self.db.commit()

    def history(self, kind: str = "search", limit: int = 50) -> list[str]:
        rows = self.db.execute(
            "SELECT query FROM history WHERE kind=? ORDER BY used_at DESC LIMIT ?",
            (kind, limit),
        )
        return [r["query"] for r in rows]

    def bookmark(self, key: str, label: str, payload: dict[str, Any]) -> None:
        self.db.execute(
            "INSERT INTO bookmarks(key,label,payload,added_at) VALUES(?,?,?,?) "
            "ON CONFLICT(key) DO UPDATE SET label=excluded.label, payload=excluded.payload",
            (key, label, json.dumps(payload), time.time()),
        )
        self.db.commit()

    def unbookmark(self, key: str) -> None:
        self.db.execute("DELETE FROM bookmarks WHERE key=?", (key,))
        self.db.commit()

    def bookmarks(self) -> list[tuple[str, str, dict[str, Any]]]:
        rows = self.db.execute("SELECT * FROM bookmarks ORDER BY added_at DESC")
        return [(r["key"], r["label"], json.loads(r["payload"])) for r in rows]
