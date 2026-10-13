"""MLS provider interface.

Every MLS data source (RESO Web API, a vendor feed, the development mock)
implements :class:`MLSProvider` and returns :class:`NormalizedListing`
objects. The synchronization service only depends on this interface, so a new
feed can be added without touching the rest of the system.

Providers must only use authorized access (API credentials, licensed feeds).
Scraping MLS websites is explicitly out of scope.
"""
from __future__ import annotations

import hashlib
import json
from abc import ABC, abstractmethod
from dataclasses import asdict, dataclass, field
from datetime import datetime
from decimal import Decimal
from typing import Any


class MLSError(Exception):
    """Base class for MLS provider errors."""


class MLSAuthError(MLSError):
    """Credentials are missing, invalid or expired. Not retryable until fixed."""


class MLSRateLimitError(MLSError):
    def __init__(self, message: str, retry_after: float | None = None):
        super().__init__(message)
        self.retry_after = retry_after


class MLSTransientError(MLSError):
    """Timeouts, 5xx responses, connection errors. Retry with backoff."""


class MLSDataError(MLSError):
    """A record could not be normalized. The record is skipped, the sync continues."""


@dataclass
class Photo:
    url: str
    order: int = 0
    caption: str | None = None
    # Whether the MLS / listing broker permits this image to be redistributed.
    authorized: bool = True


@dataclass
class OpenHouse:
    key: str
    start: datetime
    end: datetime
    remarks: str | None = None
    virtual: bool = False


@dataclass
class NormalizedListing:
    mls_id: str
    status: str  # RESO StandardStatus vocabulary
    status_raw: str | None = None
    listing_key: str | None = None
    street_address: str | None = None
    unit_number: str | None = None
    city: str | None = None
    state: str | None = None
    postal_code: str | None = None
    address_display_allowed: bool = True
    internet_display_allowed: bool = True
    price: Decimal | None = None
    original_price: Decimal | None = None
    property_type: str | None = None
    property_subtype: str | None = None
    bedrooms: int | None = None
    bathrooms_total: Decimal | None = None
    bathrooms_full: int | None = None
    bathrooms_half: int | None = None
    living_area_sqft: int | None = None
    lot_size_sqft: int | None = None
    lot_size_acres: Decimal | None = None
    year_built: int | None = None
    description: str | None = None
    features: list[str] = field(default_factory=list)
    photos: list[Photo] = field(default_factory=list)
    open_houses: list[OpenHouse] = field(default_factory=list)
    list_agent_id: str | None = None
    list_agent_name: str | None = None
    list_agent_phone: str | None = None
    list_agent_email: str | None = None
    list_office_id: str | None = None
    list_office_name: str | None = None
    list_office_phone: str | None = None
    listing_url: str | None = None
    created_at: datetime | None = None
    modified_at: datetime | None = None
    raw: dict[str, Any] = field(default_factory=dict)

    def to_jsonable(self) -> dict[str, Any]:
        data = asdict(self)
        data.pop("raw", None)
        return json.loads(json.dumps(data, default=_json_default))

    def content_hash(self) -> str:
        """Hash of all marketing-relevant fields (timestamps excluded)."""
        data = self.to_jsonable()
        data.pop("modified_at", None)
        return hashlib.sha256(json.dumps(data, sort_keys=True).encode()).hexdigest()


def _json_default(value: Any) -> Any:
    if isinstance(value, datetime):
        return value.isoformat()
    if isinstance(value, Decimal):
        return str(value)
    raise TypeError(f"not serializable: {type(value)}")


@dataclass
class FetchResult:
    listings: list[NormalizedListing]
    # True when the result is the complete set of listings in scope, which
    # allows the sync service to detect listings removed from the feed.
    is_full_snapshot: bool
    high_water_mark: datetime | None = None
    skipped_records: int = 0


class MLSProvider(ABC):
    name: str = "base"

    @abstractmethod
    def fetch_listings(self, modified_since: datetime | None = None) -> FetchResult:
        """Return listings modified since the given time (or all if None)."""

    @abstractmethod
    def fetch_listing(self, mls_id: str) -> NormalizedListing | None:
        """Return a single listing by MLS number, or None if not found/visible."""

    def check_connection(self) -> tuple[bool, str]:
        """Lightweight credential/connectivity check for the dashboard."""
        try:
            self.fetch_listings(modified_since=None)
            return True, "ok"
        except MLSError as exc:
            return False, str(exc)
