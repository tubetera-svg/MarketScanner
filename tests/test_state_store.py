"""APP_STATE_STORE=db: config/ and data/state/ files live in the app_state table."""

from __future__ import annotations

import json
import sys

import pytest

from market_data import state_store

ROOT = state_store.ROOT_DIR
if str(ROOT / "src") not in sys.path:
    sys.path.insert(0, str(ROOT / "src"))

import ict_scanner  # noqa: E402
import ltf_confirmation as ltf  # noqa: E402

PROBE = ROOT / "data" / "state" / "_state_store_probe.json"  # never created on disk
WATCHLIST = ROOT / "config" / "watchlist.txt"


@pytest.fixture()
def db_mode(tmp_db, monkeypatch):
    monkeypatch.setenv(state_store.ENV_VAR, "db")
    return tmp_db


def test_disabled_uses_plain_files(tmp_db, tmp_path, monkeypatch):
    monkeypatch.delenv(state_store.ENV_VAR, raising=False)
    assert not state_store.handles(WATCHLIST)
    target = tmp_path / "x.json"
    state_store.write_text(target, "{}")
    assert target.read_text(encoding="utf-8") == "{}"


def test_only_managed_paths_are_stored(db_mode, tmp_path):
    assert state_store.handles(WATCHLIST)
    assert state_store.handles(PROBE)
    assert not state_store.handles(tmp_path / "watchlist.txt")
    assert not state_store.handles(ROOT / "data" / "market_data.db")


def test_first_read_seeds_from_file_then_db_wins(db_mode):
    on_disk = WATCHLIST.read_bytes()
    seeded = state_store.read_text(WATCHLIST)
    assert seeded == on_disk.decode("utf-8")
    state_store.write_text(WATCHLIST, "NSE:INFY\n")
    assert state_store.read_text(WATCHLIST) == "NSE:INFY\n"
    assert WATCHLIST.read_bytes() == on_disk  # the git-tracked file is never rewritten


def test_missing_key_raises_file_not_found_and_write_skips_disk(db_mode):
    with pytest.raises(FileNotFoundError):
        state_store.read_text(PROBE)
    before = state_store.time.time()
    state_store.write_text(PROBE, json.dumps({"a": 1}))
    assert json.loads(state_store.read_text(PROBE)) == {"a": 1}
    assert state_store.updated_at(PROBE) >= before
    assert not PROBE.exists()


def test_scanner_watchlist_round_trip_in_db(db_mode):
    on_disk = WATCHLIST.read_bytes()
    assert ict_scanner.modify_watchlist_file(str(WATCHLIST), add="NSE:ZZTESTSYM")
    symbols = [symbol for symbol, _ in ict_scanner.load_watchlist(str(WATCHLIST), allow_empty=True)]
    assert "NSE:ZZTESTSYM" in symbols
    assert ict_scanner.modify_watchlist_file(str(WATCHLIST), remove="NSE:ZZTESTSYM")
    assert WATCHLIST.read_bytes() == on_disk


def test_ltf_store_and_json_cache_in_db(db_mode):
    store = ltf.LtfSetupStore(str(PROBE))
    assert store.load() == {}
    state_store.write_text(PROBE, json.dumps({"NSE:AAA": {"x": 1}, "NSE:BBB": {}}))
    assert ict_scanner.remove_symbol_from_json_cache(str(PROBE), "nse:aaa")
    assert json.loads(state_store.read_text(PROBE)) == {"NSE:BBB": {}}
    assert not PROBE.exists()
