"""Optional SQLite storage for the app's editable config and runtime state.

Set ``APP_STATE_STORE=db`` (e.g. on Render, where the disk is wiped on every
deploy/restart) to keep the files the app rewrites at runtime - config/*.json,
config/watchlist.txt and data/state/*.json - as rows of the ``app_state`` table
in the market-data DB instead of loose files. The DB itself must then live on
persistent storage (``MARKET_DATA_DB_PATH`` on a mounted disk). Unset (the
default) keeps the plain files, unchanged.

Rows are keyed by repo-relative path (``config/watchlist.txt``) and hold the
file's text. The first read of a key with no row seeds it from the file on disk
(the git-tracked copy); after that the DB copy wins and later edits to the file
are ignored. Paths outside config/ and data/state/ (tests, custom CLI paths)
always use plain files.

Errors surface as ``OSError`` (``FileNotFoundError`` when nothing is stored),
matching what callers already handle for file I/O.
"""

from __future__ import annotations

import os
import sqlite3
import time
from pathlib import Path
from typing import Optional

from . import database

ENV_VAR = "APP_STATE_STORE"
ROOT_DIR = Path(__file__).resolve().parent.parent
_MANAGED_DIRS = ("config/", "data/state/")


def enabled() -> bool:
    return os.environ.get(ENV_VAR, "").strip().lower() == "db"


def _key(path: Path | str) -> Optional[str]:
    try:
        rel = Path(path).resolve().relative_to(ROOT_DIR).as_posix()
    except ValueError:
        return None
    return rel if rel.startswith(_MANAGED_DIRS) else None


def handles(path: Path | str) -> bool:
    """True when ``path`` is stored in the DB (store enabled and path managed)."""
    return enabled() and _key(path) is not None


def _connect() -> sqlite3.Connection:
    database.init_db()
    return database.connect()


def _row(conn: sqlite3.Connection, key: str) -> Optional[sqlite3.Row]:
    return conn.execute("SELECT value, updated_at FROM app_state WHERE key = ?", (key,)).fetchone()


def _load_row(path: Path | str) -> sqlite3.Row:
    """Stored row for ``path``, seeding it from the file on first use."""
    key = _key(path)
    try:
        conn = _connect()
        try:
            row = _row(conn, key)
            if row is None:
                try:
                    with open(path, "r", encoding="utf-8", newline="") as handle:
                        text = handle.read()
                except FileNotFoundError:
                    raise FileNotFoundError(f"No stored state for {key}") from None
                conn.execute(
                    "INSERT OR IGNORE INTO app_state (key, value, updated_at) VALUES (?, ?, ?)",
                    (key, text, time.time()),
                )
                conn.commit()
                row = _row(conn, key)
            return row
        finally:
            conn.close()
    except sqlite3.Error as exc:
        raise OSError(f"app_state read failed for {key}: {exc}") from exc


def read_text(path: Path | str) -> str:
    """Contents of ``path``: the DB row when handled, else the file (like Path.read_text)."""
    if not handles(path):
        return Path(path).read_text(encoding="utf-8")
    return _load_row(path)["value"]


def write_text(path: Path | str, text: str) -> None:
    """Store ``text`` for ``path``: the DB row when handled, else the file (like Path.write_text)."""
    if not handles(path):
        Path(path).write_text(text, encoding="utf-8")
        return
    key = _key(path)
    try:
        conn = _connect()
        try:
            conn.execute(
                "INSERT INTO app_state (key, value, updated_at) VALUES (?, ?, ?) "
                "ON CONFLICT(key) DO UPDATE SET value = excluded.value, updated_at = excluded.updated_at",
                (key, text, time.time()),
            )
            conn.commit()
        finally:
            conn.close()
    except sqlite3.Error as exc:
        raise OSError(f"app_state write failed for {key}: {exc}") from exc


def updated_at(path: Path | str) -> float:
    """Last-write time of ``path`` (DB row when handled, else file mtime)."""
    if not handles(path):
        return os.path.getmtime(path)
    return float(_load_row(path)["updated_at"])
