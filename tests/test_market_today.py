"""all_strategy uses each market's own calendar date, never the host-local date."""
from __future__ import annotations

import sys
from datetime import date, datetime, timezone
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
for p in (str(ROOT), str(ROOT / "src")):
    if p not in sys.path:
        sys.path.insert(0, p)

import all_strategy  # noqa: E402

# 20:00 UTC on Oct 3 = 01:30 IST Oct 4 = 16:00 New York Oct 3: all three
# market dates differ from a naive reading in at least one zone.
_INSTANT = datetime(2026, 10, 3, 20, 0, tzinfo=timezone.utc)


class _FixedDatetime(datetime):
    @classmethod
    def now(cls, tz=None):
        return _INSTANT.astimezone(tz) if tz else _INSTANT.replace(tzinfo=None)


def test_market_today_uses_each_markets_zone(monkeypatch):
    monkeypatch.setattr(all_strategy, "datetime", _FixedDatetime)
    assert all_strategy._market_today("NSE:INFY") == date(2026, 10, 4)
    assert all_strategy._market_today("INFY") == date(2026, 10, 4)
    assert all_strategy._market_today("NSEIX:GIFTNIFTY") == date(2026, 10, 4)
    assert all_strategy._market_today("CRYPTO:BTCUSD") == date(2026, 10, 3)
    assert all_strategy._market_today("OANDA:XAUUSD") == date(2026, 10, 3)


def test_protected_swing_frame_caps_intraday_end_at_ist_today(monkeypatch):
    from market_data.sources import tradingview_source

    monkeypatch.setattr(all_strategy, "datetime", _FixedDatetime)
    seen = {}

    def fake_fetch(**kwargs):
        seen.update(kwargs)
        return []

    monkeypatch.setattr(tradingview_source, "fetch_timeframe", fake_fetch)
    # Intraday bars are labelled in IST, so a live forex scan at 01:30 IST must
    # still include the bars dated Oct 4 IST, but never reach past them.
    all_strategy._protected_swing_frame("OANDA:XAUUSD", "1h", None, date(2026, 10, 5))
    assert seen["end_date"] == date(2026, 10, 4)


def test_service_market_today_and_ist_today_ignore_host_zone():
    from market_data import service

    assert service.market_today("NSE", "NSE:INFY", now=_INSTANT) == date(2026, 10, 4)
    assert service.market_today("TRADINGVIEW", "NSEIX:GIFTNIFTY", now=_INSTANT) == date(2026, 10, 4)
    assert service.market_today("TRADINGVIEW", "CRYPTO:BTCUSD", now=_INSTANT) == date(2026, 10, 3)
    assert service.market_today("TRADINGVIEW", "OANDA:EURUSD", now=_INSTANT) == date(2026, 10, 3)
    assert service.ist_today(now=_INSTANT) == date(2026, 10, 4)
    # The same instant expressed in another zone (e.g. a US-hosted server) gives the same answer.
    from zoneinfo import ZoneInfo

    la = _INSTANT.astimezone(ZoneInfo("America/Los_Angeles"))
    assert service.market_today("NSE", "NSE:INFY", now=la) == date(2026, 10, 4)


def test_protected_swing_frame_utc_rows_match_legacy_ist_frame(monkeypatch):
    """Parity: UTC instants are converted back to the naive IST index strategies use."""
    import pandas as pd
    from market_data.sources import tradingview_source

    utc_rows = [
        {"date": "2026-10-02T18:45:00+00:00", "open": 1, "high": 2, "low": 0.5, "close": 1.5},  # 00:15 IST Oct 3
        {"date": "2026-10-03T03:45:00+00:00", "open": 1.5, "high": 2.5, "low": 1, "close": 2},  # 09:15 IST Oct 3
    ]
    monkeypatch.setattr(tradingview_source, "fetch_timeframe", lambda **kwargs: utc_rows)
    frame = all_strategy._protected_swing_frame("OANDA:XAUUSD", "1h", None, date(2026, 10, 3))
    assert list(frame.index) == [pd.Timestamp("2026-10-03 00:15"), pd.Timestamp("2026-10-03 09:15")]
    assert frame.index.tz is None


def test_price_alert_bar_close_accepts_utc_and_legacy_labels():
    sys.path.insert(0, str(ROOT / "api"))
    import price_alerts

    expected = datetime(2026, 10, 3, 4, 20, tzinfo=timezone.utc)  # 09:45 IST open + 5m
    assert price_alerts.bar_close_utc("2026-10-03T04:15:00+00:00") == expected
    assert price_alerts.bar_close_utc("2026-10-03T09:45") == expected
