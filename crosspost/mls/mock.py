"""Mock MLS provider for development and tests.

This is NOT a live MLS connection. It reads RESO-shaped records from a JSON
file (``MLS_MOCK_FEED_PATH``) or from an in-memory list, and runs them through
the same mapping code the RESO provider uses. Failure injection hooks let tests
exercise auth errors, rate limits and outages.
"""
from __future__ import annotations

import json
from datetime import datetime
from pathlib import Path
from typing import Any

from .base import FetchResult, MLSAuthError, MLSDataError, MLSProvider, MLSRateLimitError, MLSTransientError, NormalizedListing
from .mapping import reso_to_normalized


class MockMLSProvider(MLSProvider):
    name = "mock"

    def __init__(self, feed_path: str | None = None, records: list[dict[str, Any]] | None = None,
                 base_url: str = ""):
        self.feed_path = feed_path
        self._records = records
        self.base_url = base_url.rstrip("/")
        self.fail_with: str | None = None  # "auth" | "rate_limit" | "transient"

    # -- test helpers -------------------------------------------------------
    def set_records(self, records: list[dict[str, Any]]) -> None:
        self._records = records

    def upsert_record(self, record: dict[str, Any]) -> None:
        records = self._load()
        records = [r for r in records if r.get("ListingId") != record.get("ListingId")]
        records.append(record)
        self._records = records

    # -- provider API -------------------------------------------------------
    def _load(self) -> list[dict[str, Any]]:
        if self._records is not None:
            return list(self._records)
        if not self.feed_path or not Path(self.feed_path).exists():
            return []
        data = json.loads(Path(self.feed_path).read_text())
        return data.get("value", data) if isinstance(data, dict) else data

    def _absolutize(self, record: dict[str, Any]) -> dict[str, Any]:
        """Fixture photos are served by this app; make their URLs absolute like a real feed."""
        media = record.get("Media")
        if not media:
            return record
        fixed = []
        for m in media:
            url = m.get("MediaURL") or ""
            fixed.append({**m, "MediaURL": f"{self.base_url}{url}" if url.startswith("/") else url})
        return {**record, "Media": fixed}

    def _maybe_fail(self) -> None:
        if self.fail_with == "auth":
            raise MLSAuthError("mock: credentials rejected (401)")
        if self.fail_with == "rate_limit":
            raise MLSRateLimitError("mock: rate limited (429)", retry_after=60)
        if self.fail_with == "transient":
            raise MLSTransientError("mock: upstream unavailable (503)")

    def fetch_listings(self, modified_since: datetime | None = None) -> FetchResult:
        self._maybe_fail()
        listings: list[NormalizedListing] = []
        skipped = 0
        for record in self._load():
            try:
                listing = reso_to_normalized(self._absolutize(record))
            except MLSDataError:
                skipped += 1
                continue
            listings.append(listing)
        hwm = max((l.modified_at for l in listings if l.modified_at), default=None)
        # The mock always returns the complete feed, so it is a full snapshot.
        return FetchResult(listings=listings, is_full_snapshot=True, high_water_mark=hwm, skipped_records=skipped)

    def fetch_listing(self, mls_id: str) -> NormalizedListing | None:
        self._maybe_fail()
        for record in self._load():
            if str(record.get("ListingId")) == str(mls_id):
                return reso_to_normalized(self._absolutize(record))
        return None
