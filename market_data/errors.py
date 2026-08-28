"""Market-data error types shared by the service and the API layer."""

from __future__ import annotations

from typing import Any, Optional


class MarketDataError(Exception):
    """Base class for market-data failures."""


class SourceDisabledError(MarketDataError):
    """The requested source is disabled via its fetch flag."""


class NoDataError(MarketDataError):
    """No data could be returned for the request (local or fetched).

    `detail` explains why (e.g. auto-fetch disabled, empty upstream).
    """

    def __init__(self, message: str, *, detail: Optional[dict[str, Any]] = None) -> None:
        super().__init__(message)
        self.detail = detail or {}


class FetchError(MarketDataError):
    """Upstream fetching failed; any locally available rows ride along."""

    def __init__(self, message: str, *, rows: Optional[list[dict]] = None,
                 missing_dates: Optional[list[str]] = None) -> None:
        super().__init__(message)
        self.rows = rows or []
        self.missing_dates = missing_dates or []
