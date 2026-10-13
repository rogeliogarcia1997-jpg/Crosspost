"""Campaigns and trackable links.

One campaign per listing. Every post gets its own tracking link
(``/t/<code>``) that records a click and redirects to the listing landing page
with UTM parameters plus the tracking code, so visits and leads can be
attributed to the exact platform and post.
"""
from __future__ import annotations

import re
import secrets
from urllib.parse import urlencode

from sqlalchemy import select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session

from ..config import get_settings
from ..db import utcnow
from ..models import Campaign, Listing, Post, TrackingLink


def _code(prefix: str = "") -> str:
    return prefix + secrets.token_urlsafe(6).replace("-", "x").replace("_", "y")


def campaign_code(listing: Listing) -> str:
    return f"mls-{re.sub(r'[^A-Za-z0-9]+', '', listing.mls_id).lower()}"


def get_or_create_campaign(db: Session, listing: Listing) -> Campaign:
    campaign = db.scalar(select(Campaign).where(Campaign.listing_id == listing.id))
    if campaign:
        return campaign
    code = campaign_code(listing)
    if db.scalar(select(Campaign.id).where(Campaign.code == code)):
        code = f"{code}-{listing.id}"
    campaign = Campaign(listing_id=listing.id, code=code, status="active")
    try:
        with db.begin_nested():
            db.add(campaign)
    except IntegrityError:
        campaign = db.scalar(select(Campaign).where(Campaign.listing_id == listing.id))
        assert campaign is not None
    return campaign


def end_campaign(campaign: Campaign, reason: str) -> None:
    if campaign.status != "ended":
        campaign.status = "ended"
        campaign.ended_at = utcnow()
        campaign.end_reason = reason[:200]


def reactivate_campaign(campaign: Campaign) -> None:
    campaign.status = "active"
    campaign.ended_at = None
    campaign.end_reason = None


def landing_path(listing: Listing) -> str:
    return f"/p/{listing.slug}"


def create_tracking_link(db: Session, campaign: Campaign, listing: Listing, post: Post) -> TrackingLink:
    existing = db.scalar(select(TrackingLink).where(TrackingLink.post_id == post.id))
    if existing:
        return existing
    for _ in range(5):
        code = _code()
        if not db.scalar(select(TrackingLink.id).where(TrackingLink.code == code)):
            break
    link = TrackingLink(
        code=code,
        campaign_id=campaign.id,
        listing_id=listing.id,
        post_id=post.id,
        platform=post.platform,
        utm_source=post.platform,
        utm_medium="social",
        utm_campaign=campaign.code,
        utm_content=f"{post.kind}-{post.id}",
    )
    db.add(link)
    db.flush()
    return link


def tracking_url(link: TrackingLink) -> str:
    return f"{get_settings().public_base_url}/t/{link.code}"


def destination_url(link: TrackingLink, listing: Listing) -> str:
    params = {
        "utm_source": link.utm_source,
        "utm_medium": link.utm_medium,
        "utm_campaign": link.utm_campaign,
        "utm_content": link.utm_content or "",
        "tl": link.code,
    }
    return f"{landing_path(listing)}?{urlencode(params)}"
