"""Listing synchronization and change detection."""
from __future__ import annotations

import re
from datetime import datetime, timedelta
from decimal import Decimal
from typing import Any

from sqlalchemy import select
from sqlalchemy.orm import Session

from ..db import utcnow
from ..logging_setup import get_logger, log_event
from ..mls.base import (
    MLSAuthError,
    MLSDataError,
    MLSError,
    MLSProvider,
    MLSRateLimitError,
    MLSTransientError,
    NormalizedListing,
)
from ..models import Listing, ListingChange, SyncRun
from .rules import get_rules
from .workflow import handle_changes

log = get_logger(__name__)

DETAIL_FIELDS = (
    "street_address", "unit_number", "city", "state", "postal_code", "address_display_allowed",
    "internet_display_allowed", "property_type", "property_subtype", "bedrooms", "bathrooms_total",
    "bathrooms_full", "bathrooms_half", "living_area_sqft", "lot_size_sqft", "lot_size_acres", "year_built",
    "description", "features", "list_agent_id", "list_agent_name", "list_agent_phone", "list_agent_email",
    "list_office_id", "list_office_name", "list_office_phone", "listing_url", "original_price", "listing_key",
    "status_raw",
)


class SyncError(Exception):
    def __init__(self, message: str, retryable: bool, retry_after: float | None = None):
        super().__init__(message)
        self.retryable = retryable
        self.retry_after = retry_after


def _slugify(text: str) -> str:
    return re.sub(r"[^a-z0-9]+", "-", text.lower()).strip("-")


def _jsonable(value: Any) -> Any:
    if isinstance(value, Decimal):
        return str(value)
    if isinstance(value, datetime):
        return value.isoformat()
    return value


def _photos_json(nl: NormalizedListing) -> list[dict[str, Any]]:
    return [{"url": p.url, "order": p.order, "caption": p.caption, "authorized": p.authorized} for p in nl.photos]


def _open_houses_json(nl: NormalizedListing) -> list[dict[str, Any]]:
    return [{"key": o.key, "start": o.start.isoformat(), "end": o.end.isoformat(), "remarks": o.remarks,
             "virtual": o.virtual} for o in nl.open_houses]


def _apply(listing: Listing, nl: NormalizedListing) -> None:
    for fld in DETAIL_FIELDS:
        setattr(listing, fld, getattr(nl, fld))
    listing.status = nl.status
    listing.price = nl.price
    listing.photos = _photos_json(nl)
    listing.open_houses = _open_houses_json(nl)
    listing.mls_created_at = nl.created_at
    listing.mls_modified_at = nl.modified_at
    listing.content_hash = nl.content_hash()
    listing.raw = nl.to_jsonable()


def _unique_slug(db: Session, nl: NormalizedListing, provider: str) -> str:
    base = _slugify(f"{nl.mls_id}-{nl.city or ''}") or _slugify(nl.mls_id)
    slug = base
    if db.scalar(select(Listing.id).where(Listing.slug == slug)):
        slug = f"{base}-{_slugify(provider)}"
    return slug


def detect_changes(listing: Listing, nl: NormalizedListing) -> list[tuple[str, Any, Any]]:
    changes: list[tuple[str, Any, Any]] = []
    if listing.status != nl.status:
        changes.append(("status_change", listing.status, nl.status))
    old_price = Decimal(listing.price) if listing.price is not None else None
    if old_price != nl.price:
        changes.append(("price_change", _jsonable(old_price), _jsonable(nl.price)))
    old_oh = {o["key"]: o for o in listing.open_houses or []}
    new_oh = {o["key"]: o for o in _open_houses_json(nl)}
    for key in new_oh.keys() - old_oh.keys():
        changes.append(("open_house_added", None, new_oh[key]))
    for key in old_oh.keys() - new_oh.keys():
        changes.append(("open_house_removed", old_oh[key], None))
    if [p["url"] for p in listing.photos or []] != [p.url for p in nl.photos]:
        changes.append(("photos_changed", len(listing.photos or []), len(nl.photos)))
    changed_fields = [f for f in DETAIL_FIELDS if _jsonable(getattr(listing, f)) != _jsonable(getattr(nl, f))
                      and not (f == "bathrooms_total" and getattr(listing, f) is not None and getattr(nl, f) is not None
                               and Decimal(getattr(listing, f)) == Decimal(getattr(nl, f)))]
    if changed_fields:
        changes.append(("details_changed", None, changed_fields))
    return changes


def upsert_listing(db: Session, provider: str, nl: NormalizedListing, run: SyncRun | None,
                   now: datetime | None = None) -> tuple[Listing, list[ListingChange], str]:
    """Insert or update one listing. Returns (listing, recorded changes, outcome)."""
    now = now or utcnow()
    listing = db.scalar(select(Listing).where(Listing.provider == provider, Listing.mls_id == nl.mls_id))
    if listing is None:
        listing = Listing(provider=provider, mls_id=nl.mls_id, slug=_unique_slug(db, nl, provider),
                          first_seen_at=now, last_synced_at=now, content_hash="")
        _apply(listing, nl)
        db.add(listing)
        db.flush()
        change = ListingChange(listing_id=listing.id, sync_run_id=run.id if run else None, change_type="new",
                               old_value=None, new_value=listing.status, detected_at=now)
        db.add(change)
        return listing, [change], "created"

    listing.last_synced_at = now
    if listing.content_hash == nl.content_hash():
        return listing, [], "unchanged"
    if nl.modified_at and listing.mls_modified_at and nl.modified_at < listing.mls_modified_at:
        return listing, [], "stale"  # out-of-order record; keep the newer data
    raw_changes = detect_changes(listing, nl)
    _apply(listing, nl)
    recorded = []
    for change_type, old, new in raw_changes:
        change = ListingChange(listing_id=listing.id, sync_run_id=run.id if run else None, change_type=change_type,
                               old_value=old, new_value=new, detected_at=now)
        db.add(change)
        recorded.append(change)
    db.flush()
    return listing, recorded, "updated"


def mark_removed(db: Session, listing: Listing, run: SyncRun, now: datetime) -> list[ListingChange]:
    old = listing.status
    listing.status = "Removed From Feed"
    listing.content_hash = f"removed:{now.isoformat()}"
    change = ListingChange(listing_id=listing.id, sync_run_id=run.id, change_type="status_change",
                           old_value=old, new_value=listing.status, detected_at=now)
    db.add(change)
    db.flush()
    return [change]


def run_sync(db: Session, provider: MLSProvider, *, mode: str = "incremental", mls_id: str | None = None,
             triggered_by: str | None = None, now: datetime | None = None) -> SyncRun:
    now = now or utcnow()
    run = SyncRun(provider=provider.name, mode="single" if mls_id else mode, status="running", started_at=now,
                  triggered_by=triggered_by)
    db.add(run)
    db.commit()

    since = None
    if mode == "incremental" and not mls_id:
        last = db.scalar(select(SyncRun).where(SyncRun.provider == provider.name, SyncRun.status == "succeeded",
                                               SyncRun.high_water_mark.is_not(None))
                         .order_by(SyncRun.id.desc()).limit(1))
        since = last.high_water_mark if last else None

    try:
        if mls_id:
            one = provider.fetch_listing(mls_id)
            listings, full, hwm, skipped = ([one] if one else []), False, None, 0
        else:
            result = provider.fetch_listings(modified_since=since)
            listings, full, hwm, skipped = result.listings, result.is_full_snapshot, result.high_water_mark, result.skipped_records
    except MLSAuthError as exc:
        _finish(db, run, "auth_error", str(exc))
        raise SyncError(f"MLS authorization failed: {exc}", retryable=False) from exc
    except MLSRateLimitError as exc:
        _finish(db, run, "rate_limited", str(exc))
        raise SyncError(str(exc), retryable=True, retry_after=exc.retry_after) from exc
    except (MLSTransientError, MLSDataError, MLSError) as exc:
        _finish(db, run, "failed", str(exc))
        raise SyncError(str(exc), retryable=True) from exc

    rules = get_rules(db)
    run.fetched_count = len(listings)
    seen: set[str] = set()
    errors: list[str] = []
    for nl in listings:
        seen.add(nl.mls_id)
        try:
            with db.begin_nested():
                listing, changes, outcome = upsert_listing(db, provider.name, nl, run, now)
                if outcome == "created":
                    run.created_count += 1
                elif outcome == "updated":
                    run.updated_count += 1
                else:
                    run.unchanged_count += 1
                if changes:
                    handle_changes(db, listing, changes, rules, now)
        except Exception as exc:  # noqa: BLE001 - one bad record must not abort the sync
            errors.append(f"{nl.mls_id}: {type(exc).__name__}: {exc}")
            log.exception("listing_sync_failed", extra={"fields": {"mls_id": nl.mls_id}})
        db.commit()

    if full and not listings:
        # An empty "complete" feed is far more likely an upstream fault than every listing
        # being withdrawn at once; never mass-remove listings on that basis.
        errors.append("Provider returned an empty full snapshot; removal detection skipped")
    elif full:
        missing = db.scalars(select(Listing).where(Listing.provider == provider.name,
                                                   Listing.mls_id.not_in(seen) if seen else Listing.id.is_not(None),
                                                   Listing.status != "Removed From Feed"))
        for listing in missing:
            changes = mark_removed(db, listing, run, now)
            handle_changes(db, listing, changes, rules, now)
            run.updated_count += 1
        db.commit()

    run.high_water_mark = hwm or since
    if skipped:
        errors.append(f"{skipped} record(s) could not be normalized and were skipped")
    _finish(db, run, "succeeded", "\n".join(errors) or None)
    log_event(log, "mls_sync_finished", run_id=run.id, fetched=run.fetched_count, created=run.created_count,
              updated=run.updated_count, unchanged=run.unchanged_count, errors=len(errors))
    return run


def _finish(db: Session, run: SyncRun, status: str, error: str | None) -> None:
    run.status = status
    run.error = error[:4000] if error else None
    run.finished_at = utcnow()
    db.commit()


def recent_auth_failure(db: Session, provider_name: str, within: timedelta = timedelta(hours=1)) -> bool:
    last = db.scalar(select(SyncRun).where(SyncRun.provider == provider_name).order_by(SyncRun.id.desc()).limit(1))
    return bool(last and last.status == "auth_error" and last.started_at > utcnow() - within)
