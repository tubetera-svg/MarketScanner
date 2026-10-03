"""Turso mode: the default DB goes through libsql; the real libsql is faked with a
plain sqlite3 file (tuple rows, no row_factory/total_changes), so no network."""
import sqlite3
from types import SimpleNamespace

import pytest

from market_data import database, state_store


class _FakeLibsqlConnection:
    def __init__(self, path):
        self._conn = sqlite3.connect(str(path))

    def execute(self, sql, params=()):
        return self._conn.execute(sql, params)

    def executescript(self, script):
        self._conn.executescript(script)

    def commit(self):
        self._conn.commit()

    def rollback(self):
        self._conn.rollback()

    def close(self):
        self._conn.close()


@pytest.fixture
def turso(tmp_path, monkeypatch):
    remote = tmp_path / "remote.db"
    connects = []

    def fake_connect(url, auth_token=""):
        connects.append((url, auth_token))
        return _FakeLibsqlConnection(remote)

    monkeypatch.setattr(database, "_libsql", SimpleNamespace(connect=fake_connect, Error=sqlite3.Error))
    monkeypatch.setattr(database, "_INITIALIZED", set())
    monkeypatch.setenv("MARKET_DATA_DB_PATH", str(tmp_path / "local.db"))
    monkeypatch.setenv(database.TURSO_URL_ENV, "libsql://test.turso.io")
    monkeypatch.setenv(database.TURSO_TOKEN_ENV, "token")
    return SimpleNamespace(remote=remote, local=tmp_path / "local.db", connects=connects)


def _bar(day, close):
    return {"source": "NSE", "symbol": "ABC", "exchange": "NSE", "date": day,
            "open": close, "high": close + 1, "low": close - 1, "close": close, "volume": 100}


def test_default_db_goes_to_turso_with_dict_rows_and_change_counts(turso):
    database.init_db()
    assert database.upsert_ohlc([_bar("2026-10-01", 10), _bar("2026-10-02", 11)]) == 2
    assert database.upsert_ohlc([_bar("2026-10-02", 99), _bar("2026-10-03", 12)]) == 1  # duplicate kept

    rows = database.query_ohlc("NSE", "ABC")
    assert [(r["date"], r["close"]) for r in rows] == [("2026-10-01", 10), ("2026-10-02", 11), ("2026-10-03", 12)]
    assert database.count_rows() == 3
    assert turso.connects[0] == ("libsql://test.turso.io", "token")
    assert turso.remote.exists() and not turso.local.exists()


def test_explicit_non_default_path_stays_local(turso, tmp_path):
    other = tmp_path / "other.db"
    database.init_db(other)
    assert database.upsert_ohlc([_bar("2026-10-01", 10)], db_path=other) == 1
    assert other.exists() and not turso.connects


def test_app_state_round_trips_through_turso(turso, monkeypatch):
    monkeypatch.setenv("APP_STATE_STORE", "db")
    path = state_store.ROOT_DIR / "config" / "_turso_test_only.json"
    state_store.write_text(path, '{"a": 1}')
    assert state_store.read_text(path) == '{"a": 1}'
    assert not path.exists()
