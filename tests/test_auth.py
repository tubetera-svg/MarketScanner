"""Admin sign-in and read-only guest access (api/auth.py + access_control in api/main.py)."""

from __future__ import annotations

import pytest
from fastapi import HTTPException
from fastapi.routing import APIRoute
from fastapi.testclient import TestClient

from api import main as api_main

auth = api_main.auth  # the module instance api/main.py uses
LOCAL = ("127.0.0.1", 50000)
REMOTE = ("203.0.113.7", 50000)


@pytest.fixture(autouse=True)
def isolated_state(tmp_path, monkeypatch):
    """Keep settings and credentials in a temp dir; reset caches and throttles."""
    monkeypatch.delenv("APP_STATE_STORE", raising=False)
    monkeypatch.setattr(api_main.app_settings, "SETTINGS_PATH", tmp_path / "app_settings.json")
    monkeypatch.setattr(auth, "AUTH_PATH", tmp_path / "admin_auth.json")
    auth._cache.update(at=0.0, value=None)
    auth._failures.clear()
    api_main._access_cache["value"] = None
    api_main._guest_last_scan.clear()
    yield
    auth._cache.update(at=0.0, value=None)
    api_main._access_cache["value"] = None


def _client(peer=REMOTE) -> TestClient:
    return TestClient(api_main.app, client=peer)


def test_localhost_is_admin_without_login():
    assert _client(LOCAL).get("/api/auth/me").json()["role"] == "admin"


def test_proxied_loopback_is_not_local():
    """A tunnel/proxy on this machine forwards remote users from 127.0.0.1."""
    response = _client(LOCAL).get("/api/auth/me", headers={"X-Forwarded-For": "198.51.100.1"})
    assert response.json()["role"] == "guest"


def test_remote_without_token_is_guest():
    body = _client().get("/api/auth/me").json()
    assert body["role"] == "guest" and body["password_set"] is False


def test_guest_cannot_call_any_write_route():
    """Every non-GET route except the explicit guest list is refused - including future routes."""
    client = _client()
    checked = 0
    for route in api_main.app.routes:
        if not isinstance(route, APIRoute):
            continue
        path = route.path.replace("{", "").replace("}", "")
        for method in route.methods - {"GET", "HEAD"}:
            if route.path in api_main._GUEST_WRITES:
                continue
            response = client.request(method, path, json={})
            assert response.status_code == 403, f"{method} {route.path} -> {response.status_code}"
            checked += 1
    assert checked > 20  # sanity: the sweep really saw the write routes


def test_guest_page_reads_follow_settings():
    api_main._access_cache.update(at=api_main.monotonic(), value={**api_main.app_settings.DEFAULTS["access"]})
    assert not api_main._guest_allowed("GET", "/api/market-data/records", {})  # Database page off by default
    assert api_main._guest_allowed("GET", "/api/price-alerts", {})  # Alerts page on by default
    assert api_main._guest_allowed("GET", "/api/market-data/ipo/performance", {})
    api_main._access_cache["value"]["guest_pages"] = ["scanner"]
    assert not api_main._guest_allowed("GET", "/api/price-alerts", {})
    assert not api_main._guest_allowed("POST", "/api/backtest", {})
    assert api_main._guest_allowed("POST", "/api/strategy-scan", {})
    api_main._access_cache["value"]["guest_pages"] = []
    assert not api_main._guest_allowed("POST", "/api/strategy-scan", {})


def test_guest_cannot_force_remote_fetches():
    assert _client().get("/api/news/high-impact?refresh=true").status_code == 403
    assert _client().get("/api/ohlc?source=NSE&symbol=X&start_date=2026-01-01&end_date=2026-01-02&auto_fetch=1").status_code == 403


def test_guest_settings_are_display_only():
    body = _client().get("/api/settings").json()
    assert set(body["settings"]) == {"ui", "sounds", "access"}
    assert "status" not in body


def test_password_hash_and_tokens(tmp_path):
    with pytest.raises(ValueError):
        auth.set_password("short")
    auth.set_password("correct horse")
    stored = (tmp_path / "admin_auth.json").read_text(encoding="utf-8")
    assert "correct horse" not in stored
    assert auth.check_password("correct horse") and not auth.check_password("wrong")
    token = auth.issue_token(days=1)
    assert auth.verify_token(token)
    assert not auth.verify_token(token[:-2] + "xx")
    assert not auth.verify_token(auth.issue_token(days=0))  # expired
    auth.set_password("another password")  # new signing key signs every device out
    assert not auth.verify_token(token)


def test_login_flow_and_throttle():
    client = _client()
    assert client.post("/api/auth/login", json={"password": "x"}).status_code == 409  # none set yet
    auth.set_password("correct horse")
    for _ in range(auth.MAX_FAILED_LOGINS):
        assert client.post("/api/auth/login", json={"password": "nope"}).status_code == 401
    assert client.post("/api/auth/login", json={"password": "correct horse"}).status_code == 429
    auth._failures.clear()
    token = client.post("/api/auth/login", json={"password": "correct horse"}).json()["token"]
    me = client.get("/api/auth/me", headers={"Authorization": f"Bearer {token}"}).json()
    assert me["role"] == "admin"


def test_client_ip_uses_proxy_appended_address():
    class Req:
        client = type("C", (), {"host": "10.0.0.5"})()
        headers = {"x-forwarded-for": "1.1.1.1, 198.51.100.9"}
    assert auth.client_ip(Req()) == "198.51.100.9"


def test_guest_scan_limits(monkeypatch):
    monkeypatch.setattr(api_main.service, "watchlist", lambda: [{"symbol": f"NSE:S{i}"} for i in range(150)])
    monkeypatch.setattr(api_main.strategy_bridge, "list_strategies", lambda: ([
        {"name": "on_one", "enabled": True}, {"name": "off_one", "enabled": False}], True))
    api_main._access_cache.update(at=api_main.monotonic(), value={**api_main.app_settings.DEFAULTS["access"]})
    request = api_main.StrategyScanRequest(symbols=["nse:s1", "NSE:S2"], strategies=["off_one"], include_context=True)

    limited = api_main._guest_scan_request(request, "ip1")
    assert limited.symbols == ["NSE:S1", "NSE:S2"]
    assert limited.strategies is None  # disabled strategy dropped -> every enabled one
    assert limited.include_context is False and limited.include_bias is False  # extra info off for guests

    with pytest.raises(HTTPException) as too_many:
        api_main._guest_scan_request(api_main.StrategyScanRequest(symbols=[f"NSE:S{i}" for i in range(101)]), "ip1")
    assert too_many.value.status_code == 400
    with pytest.raises(HTTPException) as outside:
        api_main._guest_scan_request(api_main.StrategyScanRequest(symbols=["NSE:NOTLISTED"]), "ip1")
    assert outside.value.status_code == 403

    api_main._guest_last_scan["ip1"] = api_main.monotonic()
    with pytest.raises(HTTPException) as cooldown:
        api_main._guest_scan_request(request, "ip1")
    assert cooldown.value.status_code == 429
    assert api_main._guest_scan_request(request, "ip2").symbols  # other guests unaffected


def test_access_settings_are_clamped():
    saved = api_main.app_settings.save_settings({"access": {"guest_pages": ["scanner", "settings"], "guest_max_symbols": 99999}})
    assert saved["access"]["guest_pages"] == ["scanner"]
    assert saved["access"]["guest_max_symbols"] == 500
