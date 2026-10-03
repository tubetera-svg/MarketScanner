"""Push the local market-data DB (and app settings/state) to the online Turso DB.

Runs on the local PC only: the local API keeps using ``data/market_data.db`` and
reaches Turso over its HTTP API (``/v2/pipeline``), so the Linux-only ``libsql``
package is not needed. Credentials come from ``TURSO_SYNC_URL`` /
``TURSO_SYNC_TOKEN`` - deliberately not ``TURSO_DATABASE_URL``, which would
switch the whole local app onto Turso. When the API itself runs on Turso
(hosted) the feature is off.

Modes
- ``incremental``: ``ohlc_daily`` rows stored locally since the last sync
  (``created_at`` >= the saved cutoff; local rows are never edited in place,
  see ``database.upsert_ohlc``) plus the small tables in full. Nothing is
  deleted online.
- ``full``: mirror - every local row is upserted (local wins), then online rows
  whose key is not in the local DB are deleted. Upsert first, prune after, so
  the hosted app never sees an empty table.

Upserts only write rows whose values differ, to save Turso's write quota.
Scope ``all`` | ``data`` | ``settings``: ``data`` is the market-data tables;
``settings`` upserts the app settings/state files (``app_state`` rows, local
wins) - the hosted automations' own markers are never pushed
(``STATE_EXCLUDED``). An optional trading-date range limits the dated tables
(see ``TursoSync.run``).

CLI: ``python -m market_data.turso_sync [--full] [--scope all|data|settings]
     [--start-date YYYY-MM-DD] [--end-date YYYY-MM-DD] [--dry-run]``
"""

from __future__ import annotations

import argparse
import logging
import os
import sqlite3
import threading
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Callable, Iterable, Optional, Sequence

import requests

from . import database, state_store
from .automation_state import AutomationState
from .timeutil import utc_now

log = logging.getLogger(__name__)

URL_ENV = "TURSO_SYNC_URL"
TOKEN_ENV = "TURSO_SYNC_TOKEN"

ROOT = Path(__file__).resolve().parent.parent
# Runtime state of the hosted automations: pushing the local copy could repeat
# or skip their work (re-sent pushes), so it always stays as it is online.
STATE_EXCLUDED = frozenset({
    "data/state/automation_state.json",
    "data/state/ltf_setups.json",
    "data/state/ohlc_cache.json",
})

SCOPES = ("all", "data", "settings")  # data = OHLC + no-data markers + IPO/TV symbol tables
UPSERT_CHUNK = 250  # rows per INSERT statement (one statement = atomic)
DELETE_CHUNK = 500
KEY_PAGE = 5000
TIMEOUT_S = 60
RETRIES = 4


@dataclass(frozen=True)
class TableSpec:
    name: str
    key: tuple[str, ...]          # natural unique key
    values: tuple[str, ...]       # compared: a row is rewritten only if one differs
    extra: tuple[str, ...] = ()   # copied along with a rewrite, not compared


TABLES: tuple[TableSpec, ...] = (
    TableSpec("ohlc_daily", ("source", "exchange", "symbol", "date"),
              ("open", "high", "low", "close", "volume"), ("created_at",)),
    TableSpec("ohlc_no_data", ("source", "exchange", "symbol", "date"), (), ("checked_at",)),
    TableSpec("ipo_metadata", ("symbol",),
              ("exchange", "source", "listing_date", "listing_price", "issue_price"),
              ("created_at", "updated_at")),
    TableSpec("tv_symbol_cache", ("symbol",), ("tv_symbol", "exchange"), ("resolved_at",)),
)


class TursoSyncError(RuntimeError):
    pass


def sync_url() -> str:
    return os.environ.get(URL_ENV, "").strip()


def availability() -> tuple[bool, str]:
    """(available, reason) - only a local API with sync credentials may push."""
    if database.turso_url():
        return False, "This API already runs on Turso (hosted)."
    if not sync_url():
        return False, f"Set {URL_ENV} and {TOKEN_ENV} on the local API to enable."
    if not os.environ.get(TOKEN_ENV, "").strip():
        return False, f"{TOKEN_ENV} is not set."
    return True, ""


# ---------------------------------------------------------------- HTTP client

def _encode(value: Any) -> dict:
    if value is None:
        return {"type": "null"}
    if isinstance(value, bool):
        return {"type": "integer", "value": str(int(value))}
    if isinstance(value, int):
        return {"type": "integer", "value": str(value)}
    if isinstance(value, float):
        return {"type": "float", "value": value}
    return {"type": "text", "value": str(value)}


def _decode(cell: dict) -> Any:
    kind = cell.get("type")
    if kind == "null":
        return None
    if kind == "integer":
        return int(cell["value"])
    if kind == "float":
        return float(cell["value"])
    return cell.get("value")


class TursoHttp:
    """Minimal client for Turso's Hrana-over-HTTP ``/v2/pipeline`` endpoint."""

    def __init__(self, url: str, token: str, session: Optional[requests.Session] = None) -> None:
        base = url.strip()
        if base.startswith("libsql://"):
            base = "https://" + base[len("libsql://"):]
        self.endpoint = base.rstrip("/") + "/v2/pipeline"
        self._headers = {"Authorization": f"Bearer {token}", "Content-Type": "application/json"}
        self._session = session or requests.Session()

    def run(self, statements: Sequence[tuple[str, Sequence[Any]]]) -> list[dict]:
        """Execute statements in one request; returns each statement's result."""
        body = {"requests": [
            {"type": "execute", "stmt": {"sql": sql, "args": [_encode(v) for v in args]}}
            for sql, args in statements
        ] + [{"type": "close"}]}
        payload = self._post(body)
        results = payload.get("results") or []
        out: list[dict] = []
        for item in results[: len(statements)]:
            if item.get("type") != "ok":
                message = (item.get("error") or {}).get("message", "unknown error")
                raise TursoSyncError(f"Turso statement failed: {message}")
            out.append(item["response"]["result"])
        return out

    def query(self, sql: str, args: Sequence[Any] = ()) -> list[tuple]:
        result = self.run([(sql, args)])[0]
        return [tuple(_decode(c) for c in row) for row in result.get("rows", [])]

    def _post(self, body: dict) -> dict:
        delay = 2.0
        for attempt in range(RETRIES + 1):
            try:
                response = self._session.post(self.endpoint, json=body, headers=self._headers, timeout=TIMEOUT_S)
            except (requests.ConnectionError, requests.Timeout) as exc:
                if attempt == RETRIES:
                    raise TursoSyncError(f"Turso unreachable: {exc.__class__.__name__}") from exc
            else:
                if response.status_code == 200:
                    return response.json()
                if response.status_code in (401, 403):
                    raise TursoSyncError(f"Turso rejected the token (HTTP {response.status_code}).")
                if response.status_code != 429 and response.status_code < 500:
                    raise TursoSyncError(f"Turso HTTP {response.status_code}: {response.text[:200]}")
                if attempt == RETRIES:
                    raise TursoSyncError(f"Turso HTTP {response.status_code} after {RETRIES} retries.")
            time.sleep(delay)
            delay *= 2
        raise TursoSyncError("unreachable")


# ---------------------------------------------------------------- SQL builders

def _upsert_sql(spec: TableSpec, rows: int) -> str:
    cols = spec.key + spec.values + spec.extra
    placeholders = "(" + ", ".join("?" * len(cols)) + ")"
    sql = (f"INSERT INTO {spec.name} ({', '.join(cols)}) VALUES "
           + ", ".join([placeholders] * rows)
           + f" ON CONFLICT ({', '.join(spec.key)}) DO ")
    if not spec.values:
        return sql + "NOTHING"
    sets = ", ".join(f"{c} = excluded.{c}" for c in spec.values + spec.extra)
    differs = " OR ".join(f"{spec.name}.{c} IS NOT excluded.{c}" for c in spec.values)
    return sql + f"UPDATE SET {sets} WHERE {differs}"


def _date_filter(spec: TableSpec, start_date: Optional[str], end_date: Optional[str]) -> tuple[list[str], list]:
    """SQL conditions for an inclusive trading-date range (dated tables only)."""
    conds: list[str] = []
    args: list = []
    if "date" not in spec.key:
        return conds, args
    if start_date:
        conds.append("date >= ?")
        args.append(start_date)
    if end_date:
        conds.append("date <= ?")
        args.append(end_date)
    return conds, args


def _chunks(items: Sequence, size: int) -> Iterable[Sequence]:
    for start in range(0, len(items), size):
        yield items[start:start + size]


# ---------------------------------------------------------------- sync job

def _open_local(path: Path) -> sqlite3.Connection:
    """Read-only connection to the local DB file (never through database.connect)."""
    if not path.exists():
        raise TursoSyncError(f"Local DB not found: {path}")
    return sqlite3.connect(f"file:{path.as_posix()}?mode=ro", uri=True, timeout=15)


def _state_items(local: sqlite3.Connection) -> list[tuple[str, str]]:
    """(key, text) of the settings/state files to push, read without writing."""
    rows: dict[str, str] = {}
    if state_store.enabled():
        rows = {k: v for k, v in local.execute("SELECT key, value FROM app_state")}
    paths = sorted((ROOT / "config").glob("*.json")) + [ROOT / "config" / "watchlist.txt"]
    paths += sorted((ROOT / "data" / "state").glob("*.json"))
    keys = {p.relative_to(ROOT).as_posix() for p in paths if p.is_file()} | set(rows)
    items = []
    for key in sorted(keys - STATE_EXCLUDED):
        if not key.startswith(("config/", "data/state/")):
            continue
        if key in rows:
            items.append((key, rows[key]))
        else:
            items.append((key, (ROOT / key).read_text(encoding="utf-8")))
    return items


class TursoSync:
    """One sync run. ``progress`` is called with (table, done, total)."""

    def __init__(self, client: TursoHttp, local_path: Path,
                 progress: Optional[Callable[[str, int, int], None]] = None) -> None:
        self.client = client
        self.local_path = local_path
        self.progress = progress or (lambda table, done, total: None)

    def run(self, mode: str, scope: str = "all", since: Optional[str] = None,
            start_date: Optional[str] = None, end_date: Optional[str] = None, dry_run: bool = False) -> dict:
        """Push ``scope`` ("all" | "data" | "settings").

        A date range (trading dates, inclusive; either end optional) limits the
        dated tables (ohlc_daily, ohlc_no_data) to that range: every local row in
        it is pushed (the incremental cutoff is ignored) and a full mirror only
        deletes online rows inside it. The small undated tables always go whole.
        ``ohlc_cutoff`` in the result is None unless the run covered all OHLC rows
        (no range), so a ranged run never moves the incremental cutoff.
        """
        _validate(mode, scope, start_date, end_date)
        ranged = bool(start_date or end_date)
        push_data = scope in ("all", "data")
        local = _open_local(self.local_path)
        try:
            cutoff = local.execute("SELECT max(created_at) FROM ohlc_daily").fetchone()[0]
            tables: dict[str, dict] = {}
            if not dry_run:
                self._ensure_schema()
            for spec in TABLES if push_data else ():
                conds, args = _date_filter(spec, start_date, end_date)
                if spec.name == "ohlc_daily" and mode == "incremental" and since and not ranged:
                    conds.append("created_at >= ?")
                    args.append(since)
                where = f" WHERE {' AND '.join(conds)}" if conds else ""
                cols = spec.key + spec.values + spec.extra
                rows = local.execute(f"SELECT {', '.join(cols)} FROM {spec.name}{where}", args).fetchall()
                pushed = 0 if dry_run else self._upsert(spec, rows)
                stats = {"local_rows": len(rows), "upserted": pushed, "deleted": 0}
                if mode == "full" and not dry_run:
                    local_keys = {tuple(r[: len(spec.key)]) for r in rows}
                    stats["deleted"] = self._prune(spec, local_keys, start_date, end_date)
                tables[spec.name] = stats
            state = {"pushed": 0, "keys": []}
            if scope in ("all", "settings"):
                items = _state_items(local)
                state["keys"] = [k for k, _ in items]
                if not dry_run:
                    state["pushed"] = self._push_state(items)
            online = {} if dry_run or not push_data else {
                spec.name: self.client.query(f"SELECT count(*) FROM {spec.name}")[0][0] for spec in TABLES
            }
            local_counts = {
                spec.name: local.execute(f"SELECT count(*) FROM {spec.name}").fetchone()[0] for spec in TABLES
            }
        finally:
            local.close()
        return {
            "mode": mode,
            "scope": scope,
            "dry_run": dry_run,
            "start_date": start_date,
            "end_date": end_date,
            "since": None if ranged else since,
            "ohlc_cutoff": cutoff if push_data and not ranged else None,
            "tables": tables,
            "state": state,
            "local_counts": local_counts,
            "online_counts": online,
        }

    def _ensure_schema(self) -> None:
        statements = [s.strip() for s in database._SCHEMA.split(";") if s.strip()]
        self.client.run([(s, ()) for s in statements] + [(s, ()) for s in database._INDEXES])

    def _upsert(self, spec: TableSpec, rows: Sequence[tuple]) -> int:
        written = 0
        self.progress(spec.name, 0, len(rows))
        for done, chunk in enumerate(_chunks(rows, UPSERT_CHUNK), start=1):
            args = [v for row in chunk for v in row]
            result = self.client.run([(_upsert_sql(spec, len(chunk)), args)])[0]
            written += int(result.get("affected_row_count") or 0)
            self.progress(spec.name, min(done * UPSERT_CHUNK, len(rows)), len(rows))
        return written

    def _prune(self, spec: TableSpec, local_keys: set[tuple],
               start_date: Optional[str] = None, end_date: Optional[str] = None) -> int:
        """Delete online rows (inside the date range, if any) whose key is not local."""
        stale: list[int] = []
        last = 0
        key_cols = ", ".join(spec.key)
        conds, range_args = _date_filter(spec, start_date, end_date)
        extra = "".join(f" AND {c}" for c in conds)
        while True:
            page = self.client.query(
                f"SELECT rowid, {key_cols} FROM {spec.name} WHERE rowid > ?{extra} "
                f"ORDER BY rowid LIMIT {KEY_PAGE}",
                [last, *range_args],
            )
            if not page:
                break
            stale.extend(row[0] for row in page if tuple(row[1:]) not in local_keys)
            last = page[-1][0]
        deleted = 0
        for chunk in _chunks(stale, DELETE_CHUNK):
            sql = f"DELETE FROM {spec.name} WHERE rowid IN ({', '.join('?' * len(chunk))})"
            deleted += int(self.client.run([(sql, chunk)])[0].get("affected_row_count") or 0)
        return deleted

    def _push_state(self, items: Sequence[tuple[str, str]]) -> int:
        now = time.time()
        sql = ("INSERT INTO app_state (key, value, updated_at) VALUES (?, ?, ?) "
               "ON CONFLICT (key) DO UPDATE SET value = excluded.value, updated_at = excluded.updated_at "
               "WHERE app_state.value IS NOT excluded.value")
        results = self.client.run([(sql, (key, text, now)) for key, text in items])
        return sum(int(r.get("affected_row_count") or 0) for r in results)


# ---------------------------------------------------------------- background job (API)

_STATE = AutomationState("turso_sync")
_JOB_LOCK = threading.Lock()
_job: dict[str, Any] = {"running": False}


def status() -> dict:
    available, reason = availability()
    with _JOB_LOCK:
        job = dict(_job)
    local_counts: dict[str, int] = {}
    if available:
        try:
            conn = _open_local(database.resolve_db_path())
            try:
                local_counts = {s.name: conn.execute(f"SELECT count(*) FROM {s.name}").fetchone()[0] for s in TABLES}
            finally:
                conn.close()
        except (sqlite3.Error, TursoSyncError) as exc:
            log.warning("Turso sync: local counts unavailable (%s).", exc)
    return {"available": available, "reason": reason, "job": job,
            "last": _STATE.load(), "local_counts": local_counts}


def _validate(mode: str, scope: str, start_date: Optional[str], end_date: Optional[str]) -> None:
    if mode not in ("incremental", "full"):
        raise ValueError("mode must be 'incremental' or 'full'")
    if scope not in SCOPES:
        raise ValueError(f"scope must be one of {', '.join(SCOPES)}")
    if start_date and end_date and start_date > end_date:
        raise ValueError("start_date must be on or before end_date")


def start(mode: str, scope: str = "all", start_date: Optional[str] = None, end_date: Optional[str] = None) -> dict:
    """Start a sync in a background thread. RuntimeError if unavailable/busy, ValueError if bad input."""
    available, reason = availability()
    if not available:
        raise RuntimeError(reason)
    _validate(mode, scope, start_date, end_date)
    with _JOB_LOCK:
        if _job.get("running"):
            raise RuntimeError("A Turso sync is already running.")
        _job.clear()
        _job.update({"running": True, "mode": mode, "scope": scope, "start_date": start_date, "end_date": end_date,
                     "started_at": utc_now().isoformat(timespec="seconds"),
                     "table": None, "done": 0, "total": 0, "error": None})
    threading.Thread(target=_run_job, args=(mode, scope, start_date, end_date),
                     name="turso-sync", daemon=True).start()
    return status()


def _progress(table: str, done: int, total: int) -> None:
    with _JOB_LOCK:
        _job.update({"table": table, "done": done, "total": total})


def _execute(mode: str, scope: str, start_date: Optional[str], end_date: Optional[str],
             progress: Optional[Callable[[str, int, int], None]] = None, dry_run: bool = False) -> dict:
    """Run one sync and save the result (keeping the incremental cutoff if this run didn't move it)."""
    last = _STATE.load()
    since = last.get("ohlc_cutoff") if mode == "incremental" else None
    client = TursoHttp(sync_url() or "https://unused", os.environ.get(TOKEN_ENV, "").strip())
    result = TursoSync(client, database.resolve_db_path(), progress).run(
        mode, scope, since=since, start_date=start_date, end_date=end_date, dry_run=dry_run)
    if not dry_run:
        result["ohlc_cutoff"] = result["ohlc_cutoff"] or last.get("ohlc_cutoff")
        result["finished_at"] = utc_now().isoformat(timespec="seconds")
        _STATE.save(result)
    return result


def _run_job(mode: str, scope: str, start_date: Optional[str], end_date: Optional[str]) -> None:
    try:
        result = _execute(mode, scope, start_date, end_date, _progress)
        log.info("Turso sync (%s, %s) done: %s", mode, scope, result["tables"])
        error = None
    except Exception as exc:  # noqa: BLE001 - reported to the UI, never crashes the API
        log.warning("Turso sync (%s, %s) failed: %s", mode, scope, exc)
        error = str(exc)
    with _JOB_LOCK:
        _job.update({"running": False, "error": error, "finished_at": utc_now().isoformat(timespec="seconds")})


def main(argv: Optional[Sequence[str]] = None) -> int:
    parser = argparse.ArgumentParser(description="Push the local market-data DB to Turso.")
    parser.add_argument("--full", action="store_true", help="mirror: overwrite and delete online-only rows")
    parser.add_argument("--scope", choices=SCOPES, default="all", help="all (default), data (OHLC tables) or settings")
    parser.add_argument("--start-date", help="first trading date YYYY-MM-DD (dated tables only)")
    parser.add_argument("--end-date", help="last trading date YYYY-MM-DD (inclusive)")
    parser.add_argument("--dry-run", action="store_true", help="count local rows, send nothing")
    args = parser.parse_args(argv)
    available, reason = availability()
    if not available and not args.dry_run:
        print(reason)
        return 2
    mode = "full" if args.full else "incremental"
    _validate(mode, args.scope, args.start_date, args.end_date)
    printer = lambda table, done, total: print(f"\r{table}: {done}/{total}", end="", flush=True)  # noqa: E731
    result = _execute(mode, args.scope, args.start_date, args.end_date, printer, dry_run=args.dry_run)
    print()
    for name, stats in result["tables"].items():
        online = result["online_counts"].get(name, "-")
        print(f"{name}: local {result['local_counts'][name]}, sent {stats['local_rows']}, "
              f"written {stats['upserted']}, deleted {stats['deleted']}, online {online}")
    if result["scope"] in ("all", "settings"):
        print(f"state: {len(result['state']['keys'])} files, written {result['state']['pushed']}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
