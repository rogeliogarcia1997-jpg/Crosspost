"""Event-driven marketing workflow: react to detected listing changes."""
from __future__ import annotations

from datetime import datetime, timedelta
from decimal import Decimal

from sqlalchemy import select
from sqlalchemy.orm import Session

from ..db import utcnow
from ..logging_setup import get_logger, log_event
from ..models import AVAILABLE_STATUSES, Campaign, Listing, ListingChange, Post
from .campaigns import end_campaign, get_or_create_campaign, reactivate_campaign
from .compliance import marketing_authorization
from .publishing import cancel_open_posts, plan_posts, retract_published_posts
from .rules import PublishingRules

log = get_logger(__name__)

STATUS_ANNOUNCEMENTS = {"Pending", "Active Under Contract", "Closed"}


def _has_launched(db: Session, listing: Listing) -> bool:
    return bool(db.scalar(select(Post.id).where(Post.listing_id == listing.id, Post.kind == "new_listing").limit(1)))


def launch_listing(db: Session, listing: Listing, rules: PublishingRules, now: datetime | None = None) -> list[Post]:
    """Create the campaign and the new-listing posts for an available, authorized listing."""
    auth = marketing_authorization(listing)
    if not auth.authorized:
        log_event(log, "listing_not_authorized", listing_id=listing.id, reason=auth.reason)
        return []
    if not listing.is_available:
        return []
    campaign = get_or_create_campaign(db, listing)
    if campaign.status != "active":
        reactivate_campaign(campaign)
    posts = plan_posts(db, listing, "new_listing", "new", rules, now=now)
    # Open houses already on the listing at launch are announced too.
    if rules.announce_open_houses:
        for oh in listing.open_houses or []:
            posts += _plan_open_house(db, listing, oh, rules, now)
    return posts


def _plan_open_house(db: Session, listing: Listing, oh: dict, rules: PublishingRules, now: datetime | None) -> list[Post]:
    now = now or utcnow()
    start, end = datetime.fromisoformat(oh["start"]), datetime.fromisoformat(oh["end"])
    if end <= now:
        return []
    publish_at = max(now, start - timedelta(hours=rules.open_house_lead_hours))
    return plan_posts(db, listing, "open_house", f"oh:{oh['key']}", rules, context={"open_house": oh},
                      scheduled_for=publish_at if publish_at > now else None, now=now)


def handle_changes(db: Session, listing: Listing, changes: list[ListingChange], rules: PublishingRules,
                   now: datetime | None = None) -> None:
    now = now or utcnow()
    types = {c.change_type for c in changes}
    authorized = marketing_authorization(listing).authorized

    if "new" in types:
        launch_listing(db, listing, rules, now)
        return

    for change in changes:
        if change.change_type != "status_change":
            continue
        old, new = change.old_value, change.new_value
        if new in AVAILABLE_STATUSES and old not in AVAILABLE_STATUSES:
            if _has_launched(db, listing):
                campaign = get_or_create_campaign(db, listing)
                reactivate_campaign(campaign)
                if authorized:
                    plan_posts(db, listing, "back_on_market", f"bom:{change.id}", rules, now=now)
            else:
                launch_listing(db, listing, rules, now)
        elif new not in AVAILABLE_STATUSES:
            stop_promotion(db, listing, f"Status changed from {old} to {new}", rules)
            if rules.announce_status_changes and authorized and new in STATUS_ANNOUNCEMENTS and _has_launched(db, listing):
                plan_posts(db, listing, "status_update", f"status:{new}", rules, now=now)

    if not listing.is_available or not authorized:
        return
    if not _has_launched(db, listing):
        # e.g. listing became authorized after an admin override
        launch_listing(db, listing, rules, now)
        return

    for change in changes:
        if change.change_type == "price_change" and rules.announce_price_reductions:
            old_price = Decimal(str(change.old_value)) if change.old_value is not None else None
            new_price = Decimal(str(change.new_value)) if change.new_value is not None else None
            if old_price and new_price and new_price < old_price:
                plan_posts(db, listing, "price_reduction", f"price:{new_price:.0f}", rules,
                           context={"old_price": str(old_price)}, now=now)
        elif change.change_type == "open_house_added" and rules.announce_open_houses:
            _plan_open_house(db, listing, change.new_value, rules, now)
        elif change.change_type == "open_house_removed":
            for post in db.scalars(select(Post).where(Post.listing_id == listing.id, Post.kind == "open_house",
                                                      Post.event_key == f"oh:{change.old_value.get('key')}")):
                if post.status in ("pending_approval", "scheduled", "queued", "retry_scheduled", "blocked_auth", "manual_ready"):
                    post.status, post.last_error = "canceled", "Open house removed from the MLS"


def stop_promotion(db: Session, listing: Listing, reason: str, rules: PublishingRules) -> None:
    """Stop representing a listing as available: cancel pending promos, end the campaign."""
    canceled = cancel_open_posts(db, listing, reason)
    campaign = db.scalar(select(Campaign).where(Campaign.listing_id == listing.id))
    if campaign:
        end_campaign(campaign, reason)
    retracted = retract_published_posts(db, listing, rules)
    log_event(log, "listing_promotion_stopped", listing_id=listing.id, canceled_posts=canceled, retractions=retracted)


def repost_scan(db: Session, rules: PublishingRules, now: datetime | None = None) -> int:
    """Optionally re-promote still-available listings after the configured interval."""
    if rules.repost_interval_days <= 0:
        return 0
    now = now or utcnow()
    cutoff = now - timedelta(days=rules.repost_interval_days)
    count = 0
    for campaign in db.scalars(select(Campaign).where(Campaign.status == "active")):
        listing = campaign.listing
        if not listing.is_available or not marketing_authorization(listing).authorized:
            continue
        last = db.scalar(select(Post.published_at).where(Post.campaign_id == campaign.id, Post.status == "published")
                         .order_by(Post.published_at.desc()).limit(1))
        if last is None or last > cutoff:
            continue
        campaign.repost_count += 1
        campaign.last_repost_at = now
        count += len(plan_posts(db, listing, "repost", f"repost:{campaign.repost_count}", rules, now=now))
    return count
