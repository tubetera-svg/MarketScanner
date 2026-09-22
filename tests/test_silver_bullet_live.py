"""Tests for the live AM Silver Bullet scanner's New York session gating.

Covers the two timezone-sensitive behaviours:
  * the live scan retires once 11:00 New York has passed (it must not report
    ``running`` for the rest of the day), and
  * the auto-check sleep is measured in real elapsed time so a DST change
    cannot shift the wake-up out of the 10:00-11:00 New York window.
"""

from __future__ import annotations

import asyncio
from datetime import date, datetime, timedelta, timezone
from zoneinfo import ZoneInfo

import pytest

from api.main import SilverBulletLiveScanner


NEW_YORK = ZoneInfo("America/New_York")
HOUR = 3600


def _clock(*times: datetime) -> type[datetime]:
    """datetime replacement whose ``now()`` walks ``times`` (last value repeats).

    ``_loop`` reads the clock three times per iteration (window check + 3-minute
    alignment), so callers pass each instant once per read they expect.
    """
    sequence = list(times)

    class _Clock(datetime):
        @classmethod
        def now(cls, tz=None):  # type: ignore[override]
            value = sequence.pop(0) if len(sequence) > 1 else sequence[0]
            if tz is None:
                return value.replace(tzinfo=None)
            return value.astimezone(tz)

    return _Clock


def _patched(monkeypatch, scanner: SilverBulletLiveScanner, *times: datetime) -> list[tuple[date, datetime]]:
    """Freeze the scanner clock and stub the TradingView-backed scan."""
    scans: list[tuple[date, datetime]] = []

    async def fake_scan(scan_date: date, now: datetime) -> None:
        scans.append((scan_date, now))

    real_sleep = asyncio.sleep

    async def instant_sleep(_seconds: float) -> None:
        await real_sleep(0)

    monkeypatch.setattr("api.main.datetime", _clock(*times))
    monkeypatch.setattr("api.main.asyncio.sleep", instant_sleep)
    monkeypatch.setattr(scanner, "_scan", fake_scan)
    return scans


def _drive(scanner: SilverBulletLiveScanner) -> dict:
    async def run() -> dict:
        scanner.task = asyncio.ensure_future(scanner._loop())
        assert scanner.status()["running"] is True
        await asyncio.wait_for(scanner.task, timeout=5)
        return scanner.status()

    return asyncio.run(run())


def test_live_scan_stops_once_new_york_window_closes(monkeypatch):
    scanner = SilverBulletLiveScanner()
    scanner.symbols = ["COMEX:GC1!"]
    # 09:30 (waiting) -> 10:03 (in window) -> 11:01 (window over).
    scans = _patched(
        monkeypatch,
        scanner,
        *[datetime(2026, 9, 22, 9, 30, tzinfo=NEW_YORK) for _ in range(3)],
        *[datetime(2026, 9, 22, 10, 3, tzinfo=NEW_YORK) for _ in range(3)],
        *[datetime(2026, 9, 22, 11, 1, tzinfo=NEW_YORK) for _ in range(3)],
    )

    status = _drive(scanner)

    assert status["running"] is False
    assert status["next_check_at"] is None
    assert [item[1].hour for item in scans] == [10]
    assert scans[0][0] == date(2026, 9, 22)
    assert scans[0][1].tzinfo is NEW_YORK


def test_live_scan_started_after_the_window_never_scans(monkeypatch):
    scanner = SilverBulletLiveScanner()
    scanner.symbols = ["COMEX:GC1!"]
    scans = _patched(
        monkeypatch,
        scanner,
        *[datetime(2026, 9, 22, 12, 5, tzinfo=NEW_YORK) for _ in range(3)],
    )

    status = _drive(scanner)

    assert status["running"] is False
    assert scans == []


def test_auto_check_sleep_waits_for_the_window_to_open():
    scanner = SilverBulletLiveScanner()

    assert scanner._seconds_until_next_auto_check(
        datetime(2026, 9, 22, 9, 15, tzinfo=NEW_YORK)
    ) == pytest.approx(45 * 60)
    assert scanner._seconds_until_next_auto_check(
        datetime(2026, 9, 22, 10, 30, tzinfo=NEW_YORK)
    ) == pytest.approx(scanner.AUTO_CHECK_SECONDS)


def test_auto_check_sleep_uses_real_time_across_dst_start():
    """2026-03-08 02:00 New York jumps EST -> EDT, so this "day" is 23 real hours.

    Naive (same-tzinfo) subtraction returned 22h and woke the check at 11:00 New
    York, skipping the whole 10:00-11:00 window on the first session after the
    transition.
    """
    scanner = SilverBulletLiveScanner()
    now = datetime(2026, 3, 7, 12, 0, tzinfo=NEW_YORK)
    seconds = scanner._seconds_until_next_auto_check(now)

    assert seconds == pytest.approx(21 * HOUR)
    # Wall-clock subtraction over the same tzinfo would have said 22 hours.
    assert datetime(2026, 3, 8, 10, 0, tzinfo=NEW_YORK) - now == timedelta(hours=22)
    assert now.astimezone(timezone.utc) + timedelta(seconds=seconds) == datetime(
        2026, 3, 8, 10, 0, tzinfo=NEW_YORK
    ).astimezone(timezone.utc)


def test_auto_check_sleep_uses_real_time_across_dst_end():
    """2026-11-01 02:00 New York falls back EDT -> EST: 23 real hours to 10:00."""
    scanner = SilverBulletLiveScanner()
    now = datetime(2026, 10, 31, 12, 0, tzinfo=NEW_YORK)
    seconds = scanner._seconds_until_next_auto_check(now)

    assert seconds == pytest.approx(23 * HOUR)
    assert now.astimezone(timezone.utc) + timedelta(seconds=seconds) == datetime(
        2026, 11, 1, 10, 0, tzinfo=NEW_YORK
    ).astimezone(timezone.utc)
    # Regular (no transition) close-of-window wait stays 23 hours of wall time.
    assert scanner._seconds_until_next_auto_check(
        datetime(2026, 9, 22, 11, 0, tzinfo=NEW_YORK)
    ) == pytest.approx(23 * HOUR)

