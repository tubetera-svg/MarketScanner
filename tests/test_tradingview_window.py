"""Regression tests for the TradingView fetch window (tvDatafeed ``n_bars``).

``tvDatafeed.get_hist`` only ever returns the most recent ``n_bars`` ending
*now*, so a request for an older session must ask for enough bars to reach from
today back to ``start_date``. Sizing the window from the requested span alone
left the requested day before the returned window, so every bar was filtered
out -> "TradingView returned 0 15m bars" (e.g. the AM Silver Bullet date test
for a session a few days old). Offline: the client is faked.
"""

from __future__ import annotations

from datetime import date, timedelta

import pandas as pd

from market_data.sources import tradingview_source


class _RecordingClient:
    """Captures the ``get_hist`` kwargs and returns a synthetic frame."""

    def __init__(self, frame: pd.DataFrame) -> None:
        self.frame = frame
        self.calls: list[dict] = []

    def get_hist(self, **kwargs):
        self.calls.append(kwargs)
        return self.frame


def _session_frame(day: date, bars: int) -> pd.DataFrame:
    """``bars`` contiguous 15m bars starting at local midnight of ``day``."""
    index = pd.DatetimeIndex([pd.Timestamp(day) + pd.Timedelta(minutes=15 * i) for i in range(bars)])
    return pd.DataFrame(
        {
            "open": [1.0] * bars,
            "high": [2.0] * bars,
            "low": [0.5] * bars,
            "close": [1.5] * bars,
            "volume": [10.0] * bars,
        },
        index=index,
    )


def test_old_session_request_asks_for_enough_bars(monkeypatch):
    session = date.today() - timedelta(days=3)
    client = _RecordingClient(_session_frame(session, 96))
    monkeypatch.setattr(tradingview_source, "_get_client", lambda: client)

    rows = tradingview_source.fetch_timeframe("OANDA:XAUUSD", session, session, "15m", "OANDA")

    assert rows, "a past session must still return bars"
    gap_days = (date.today() - session).days
    # Must span at least the weekday count of the gap at ~96 15m bars/day.
    assert client.calls[0]["n_bars"] >= gap_days * 96


def test_current_span_keeps_original_window(monkeypatch):
    end = date.today()
    start = end - timedelta(days=29)  # 30 daily bars ending today
    client = _RecordingClient(_session_frame(end, 1))
    monkeypatch.setattr(tradingview_source, "_get_client", lambda: client)

    tradingview_source.fetch_timeframe("OANDA:XAUUSD", start, end, "1d", "OANDA")

    assert client.calls[0]["n_bars"] == 30 * 1 * 2 + 10  # span_days * bars_per_day * 2 + 10


def test_window_is_capped(monkeypatch):
    old = date.today() - timedelta(days=400)
    client = _RecordingClient(_session_frame(old, 96))
    monkeypatch.setattr(tradingview_source, "_get_client", lambda: client)

    tradingview_source.fetch_timeframe("OANDA:XAUUSD", old, old, "15m", "OANDA")

    assert client.calls[0]["n_bars"] == 5000
