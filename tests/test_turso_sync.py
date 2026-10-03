"""Local -> Turso push (market_data.turso_sync), against a fake Turso HTTP endpoint
backed by in-memory SQLite. No network."""

from __future__ import annotations

import json
import sqlite3

import pytest

from market_data import database, turso_sync


def _row(symbol, day, close=10.0, source="NSE"):
    return {"source": source, "symbol": symbol, "exchange": "NSE", "date": day,
            "open": close, "high": close + 1, "low": close - 1, "close": close, "volume": 100.0}


class FakeTurso(turso_sync.TursoHttp):
    """Answers /v2/pipeline bodies by executing them on an in-memory SQLite DB."""

    def __init__(self) -> None:
        super().__init__("libsql://demo-db.turso.io", "secret")
        self.db = sqlite3.connect(":memory:")
        self.requests = 0

    def _post(self, body: dict) -> dict:
        self.requests += 1
        results = []
        for item in body["requests"]:
            if item["type"] == "close":
                results.append({"type": "ok", "response": {"type": "close"}})
                continue
            stmt = item["stmt"]
            args = [turso_sync._decode(a) for a in stmt["args"]]
            cur = self.db.execute(stmt["sql"], args)
            rows = [[turso_sync._encode(v) for v in r] for r in cur.fetchall()]
            results.append({"type": "ok", "response": {"type": "execute", "result": {
                "cols": [], "rows": rows, "affected_row_count": max(cur.rowcount, 0)}}})
        self.db.commit()
        return json.loads(json.dumps({"results": results}))  # same shape as over the wire

    def ohlc(self):
        return self.db.execute(
            "SELECT symbol, date, close FROM ohlc_daily ORDER BY symbol, date").fetchall()


@pytest.fixture
def local_db(tmp_path):
    path = tmp_path / "local.db"
    database.init_db(path)
    database.upsert_ohlc([_row("AAA", "2026-10-01"), _row("AAA", "2026-10-02"), _row("BBB", "2026-10-01")], path)
    return path


def test_endpoint_from_libsql_url():
    assert FakeTurso().endpoint == "https://demo-db.turso.io/v2/pipeline"


def test_full_mirror_overwrites_and_prunes(local_db):
    fake = FakeTurso()
    sync = turso_sync.TursoSync(fake, local_db)
    sync._ensure_schema()
    fake.db.execute(
        "INSERT INTO ohlc_daily (source, symbol, exchange, date, open, high, low, close, volume) VALUES "
        "('NSE','AAA','NSE','2026-10-01',1,2,0,1,5), ('NSE','ZZZ','NSE','2026-10-01',1,2,0,1,5)")

    result = sync.run("full", scope="data")

    assert fake.ohlc() == [("AAA", "2026-10-01", 10.0), ("AAA", "2026-10-02", 10.0), ("BBB", "2026-10-01", 10.0)]
    assert result["tables"]["ohlc_daily"] == {"local_rows": 3, "upserted": 3, "deleted": 1}
    assert result["online_counts"]["ohlc_daily"] == 3

    # Unchanged rows are not rewritten on the next mirror (saves Turso writes).
    again = sync.run("full", scope="data")
    assert again["tables"]["ohlc_daily"] == {"local_rows": 3, "upserted": 0, "deleted": 0}


def test_incremental_sends_only_rows_since_cutoff_and_never_deletes(local_db):
    fake = FakeTurso()
    sync = turso_sync.TursoSync(fake, local_db)
    sync._ensure_schema()
    fake.db.execute(
        "INSERT INTO ohlc_daily (source, symbol, exchange, date, open, high, low, close, volume) "
        "VALUES ('NSE','ZZZ','NSE','2026-10-01',1,2,0,1,5)")
    with sqlite3.connect(local_db) as conn:
        conn.execute("UPDATE ohlc_daily SET created_at = '2026-10-01 10:00:00'")
        conn.execute("UPDATE ohlc_daily SET created_at = '2026-10-03 12:00:00' WHERE symbol = 'BBB'")

    result = sync.run("incremental", scope="data", since="2026-10-03 12:00:00")

    assert result["tables"]["ohlc_daily"]["local_rows"] == 1
    assert result["ohlc_cutoff"] == "2026-10-03 12:00:00"
    assert fake.ohlc() == [("BBB", "2026-10-01", 10.0), ("ZZZ", "2026-10-01", 1.0)]


def test_state_push_skips_hosted_automation_markers(local_db, tmp_path, monkeypatch):
    root = tmp_path / "repo"
    (root / "config").mkdir(parents=True)
    (root / "data" / "state").mkdir(parents=True)
    (root / "config" / "app_settings.json").write_text('{"ui": 1}', encoding="utf-8")
    (root / "config" / "watchlist.txt").write_text("NSE:AAA\n", encoding="utf-8")
    (root / "data" / "state" / "price_alerts.json").write_text("[]", encoding="utf-8")
    for name in ("automation_state.json", "ltf_setups.json", "ohlc_cache.json"):
        (root / "data" / "state" / name).write_text("{}", encoding="utf-8")
    monkeypatch.setattr(turso_sync, "ROOT", root)
    monkeypatch.delenv("APP_STATE_STORE", raising=False)

    fake = FakeTurso()
    sync = turso_sync.TursoSync(fake, local_db)
    sync._ensure_schema()
    fake.db.execute("INSERT INTO app_state VALUES ('config/app_settings.json', 'old', 1)")
    fake.db.execute("INSERT INTO app_state VALUES ('data/state/automation_state.json', 'hosted', 1)")

    result = sync.run("incremental", scope="settings")

    stored = dict(fake.db.execute("SELECT key, value FROM app_state"))
    assert result["state"]["keys"] == ["config/app_settings.json", "config/watchlist.txt",
                                       "data/state/price_alerts.json"]
    assert stored["config/app_settings.json"] == '{"ui": 1}'
    assert stored["data/state/automation_state.json"] == "hosted"
    assert "data/state/ltf_setups.json" not in stored
    assert result["tables"] == {} and result["ohlc_cutoff"] is None  # settings only: no OHLC pushed


def test_date_range_limits_push_and_mirror_prune(local_db):
    fake = FakeTurso()
    sync = turso_sync.TursoSync(fake, local_db)
    sync._ensure_schema()
    fake.db.execute(
        "INSERT INTO ohlc_daily (source, symbol, exchange, date, open, high, low, close, volume) VALUES "
        "('NSE','OLD','NSE','2026-09-01',1,2,0,1,5), ('NSE','ZZZ','NSE','2026-10-02',1,2,0,1,5)")

    result = sync.run("full", scope="data", start_date="2026-10-02", end_date="2026-10-31")

    # Only the in-range local row is sent; only the in-range online-only row is deleted.
    assert result["tables"]["ohlc_daily"] == {"local_rows": 1, "upserted": 1, "deleted": 1}
    assert fake.ohlc() == [("AAA", "2026-10-02", 10.0), ("OLD", "2026-09-01", 1.0)]
    assert result["ohlc_cutoff"] is None  # a ranged run never moves the incremental cutoff


def test_invalid_range_and_scope_rejected(local_db):
    sync = turso_sync.TursoSync(FakeTurso(), local_db)
    with pytest.raises(ValueError):
        sync.run("full", start_date="2026-10-03", end_date="2026-10-01")
    with pytest.raises(ValueError):
        sync.run("full", scope="everything")


def test_local_db_is_opened_read_only(local_db):
    conn = turso_sync._open_local(local_db)
    try:
        with pytest.raises(sqlite3.OperationalError):
            conn.execute("DELETE FROM ohlc_daily")
    finally:
        conn.close()


def test_unavailable_when_api_runs_on_turso_or_without_credentials(monkeypatch):
    monkeypatch.delenv(turso_sync.URL_ENV, raising=False)
    monkeypatch.delenv(database.TURSO_URL_ENV, raising=False)
    assert turso_sync.availability()[0] is False
    monkeypatch.setenv(turso_sync.URL_ENV, "libsql://demo-db.turso.io")
    monkeypatch.setenv(turso_sync.TOKEN_ENV, "secret")
    assert turso_sync.availability() == (True, "")
    monkeypatch.setenv(database.TURSO_URL_ENV, "libsql://demo-db.turso.io")
    assert turso_sync.availability()[0] is False
    with pytest.raises(RuntimeError):
        turso_sync.start("full")


def test_http_errors_surface_without_token(monkeypatch):
    class Resp:
        status_code = 401
        text = "nope"

    class Session:
        def post(self, *args, **kwargs):
            return Resp()

    client = turso_sync.TursoHttp("libsql://demo-db.turso.io", "secret-token", Session())
    with pytest.raises(turso_sync.TursoSyncError) as err:
        client.query("SELECT 1")
    assert "secret-token" not in str(err.value)
