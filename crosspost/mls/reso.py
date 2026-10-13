"""RESO Web API (OData) provider.

Requires credentials issued by your MLS or its approved data vendor. Supports
either a static bearer token (``RESO_ACCESS_TOKEN``) or OAuth2 client
credentials (``RESO_TOKEN_URL`` + ``RESO_CLIENT_ID`` + ``RESO_CLIENT_SECRET``).

Server capabilities differ between vendors (``$expand`` support, page sizes,
field availability, rate limits). Validate against your vendor's metadata
document (``$metadata``) before going live.
"""
from __future__ import annotations

import time
from datetime import datetime, timedelta

import httpx

from ..logging_setup import get_logger, log_event
from .base import (
    FetchResult,
    MLSAuthError,
    MLSDataError,
    MLSProvider,
    MLSRateLimitError,
    MLSTransientError,
    NormalizedListing,
)
from .mapping import reso_to_normalized

log = get_logger(__name__)


class RESOWebAPIProvider(MLSProvider):
    name = "reso"

    def __init__(
        self,
        base_url: str,
        *,
        access_token: str | None = None,
        token_url: str | None = None,
        client_id: str | None = None,
        client_secret: str | None = None,
        scope: str | None = None,
        resource: str = "Property",
        expand: str = "Media,OpenHouse",
        page_size: int = 200,
        extra_filter: str | None = None,
        timeout: float = 20.0,
        max_pages: int = 500,
        client: httpx.Client | None = None,
    ):
        if not base_url:
            raise MLSAuthError("RESO_BASE_URL is not configured")
        if not access_token and not (token_url and client_id and client_secret):
            raise MLSAuthError("RESO credentials are not configured (token or client credentials required)")
        self.base_url = base_url.rstrip("/")
        self._static_token = access_token
        self.token_url, self.client_id, self.client_secret, self.scope = token_url, client_id, client_secret, scope
        self.resource, self.expand, self.page_size = resource, expand, page_size
        self.extra_filter, self.max_pages = extra_filter, max_pages
        self._client = client or httpx.Client(timeout=timeout)
        self._token: str | None = access_token
        self._token_expiry: float = float("inf") if access_token else 0.0

    # -- auth -------------------------------------------------------------
    def _get_token(self, force: bool = False) -> str:
        if self._static_token:
            return self._static_token
        if self._token and not force and time.time() < self._token_expiry - 60:
            return self._token
        data = {"grant_type": "client_credentials"}
        if self.scope:
            data["scope"] = self.scope
        try:
            resp = self._client.post(self.token_url, data=data, auth=(self.client_id, self.client_secret))
        except httpx.HTTPError as exc:
            raise MLSTransientError(f"token endpoint unreachable: {type(exc).__name__}") from exc
        if resp.status_code in (400, 401, 403):
            raise MLSAuthError(f"token request rejected ({resp.status_code})")
        if resp.status_code >= 500:
            raise MLSTransientError(f"token endpoint error ({resp.status_code})")
        body = resp.json()
        self._token = body["access_token"]
        self._token_expiry = time.time() + float(body.get("expires_in", 3600))
        return self._token

    def _request(self, url: str, params: dict | None = None) -> dict:
        for attempt in range(2):
            token = self._get_token(force=attempt > 0)
            try:
                resp = self._client.get(url, params=params, headers={"Authorization": f"Bearer {token}", "Accept": "application/json"})
            except httpx.TimeoutException as exc:
                raise MLSTransientError("MLS request timed out") from exc
            except httpx.HTTPError as exc:
                raise MLSTransientError(f"MLS connection error: {type(exc).__name__}") from exc
            if resp.status_code == 401 and attempt == 0 and not self._static_token:
                continue  # token may have expired early; refresh once
            if resp.status_code in (401, 403):
                raise MLSAuthError(f"MLS rejected credentials ({resp.status_code})")
            if resp.status_code == 429:
                retry_after = resp.headers.get("Retry-After")
                raise MLSRateLimitError("MLS rate limit reached", retry_after=float(retry_after) if retry_after and retry_after.isdigit() else None)
            if resp.status_code >= 500:
                raise MLSTransientError(f"MLS server error ({resp.status_code})")
            if resp.status_code >= 400:
                raise MLSDataError(f"MLS request invalid ({resp.status_code})")
            return resp.json()
        raise MLSAuthError("MLS rejected refreshed credentials")

    # -- provider API -------------------------------------------------------
    def _filter(self, modified_since: datetime | None) -> str | None:
        clauses = []
        if modified_since:
            # Small overlap guards against clock skew; duplicates are de-duplicated by content hash.
            ts = (modified_since - timedelta(minutes=1)).strftime("%Y-%m-%dT%H:%M:%SZ")
            clauses.append(f"ModificationTimestamp gt {ts}")
        if self.extra_filter:
            clauses.append(f"({self.extra_filter})")
        return " and ".join(clauses) or None

    def fetch_listings(self, modified_since: datetime | None = None) -> FetchResult:
        params: dict[str, str] = {"$top": str(self.page_size), "$orderby": "ModificationTimestamp asc"}
        flt = self._filter(modified_since)
        if flt:
            params["$filter"] = flt
        if self.expand:
            params["$expand"] = self.expand
        url: str | None = f"{self.base_url}/{self.resource}"
        listings: list[NormalizedListing] = []
        skipped = 0
        pages = 0
        while url and pages < self.max_pages:
            body = self._request(url, params)
            pages += 1
            for record in body.get("value", []):
                try:
                    listings.append(reso_to_normalized(record))
                except MLSDataError:
                    skipped += 1
            url = body.get("@odata.nextLink")
            params = None  # nextLink already includes the query
        if url:
            log_event(log, "reso_page_limit_reached", pages=pages)
        hwm = max((l.modified_at for l in listings if l.modified_at), default=None)
        return FetchResult(
            listings=listings,
            is_full_snapshot=modified_since is None and url is None and not self.extra_filter,
            high_water_mark=hwm,
            skipped_records=skipped,
        )

    def fetch_listing(self, mls_id: str) -> NormalizedListing | None:
        safe_id = mls_id.replace("'", "''")
        params = {"$filter": f"ListingId eq '{safe_id}'", "$top": "1"}
        if self.expand:
            params["$expand"] = self.expand
        body = self._request(f"{self.base_url}/{self.resource}", params)
        values = body.get("value", [])
        return reso_to_normalized(values[0]) if values else None

    def check_connection(self) -> tuple[bool, str]:
        try:
            self._request(f"{self.base_url}/{self.resource}", {"$top": "1", "$select": "ListingKey"})
            return True, "ok"
        except Exception as exc:  # noqa: BLE001 - surfaced to admin
            return False, str(exc)
