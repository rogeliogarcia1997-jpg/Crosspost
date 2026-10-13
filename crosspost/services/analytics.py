"""Campaign attribution analytics.

Every figure here is *directly measured* by this application:

* link clicks   - requests to our /t/<code> redirect (bots excluded)
* landing visits - landing page views (bots excluded)
* leads / inquiries - form submissions
* qualified leads - leads whose status a user set to qualified/closed

Platform-side metrics (impressions, reach, engagement, platform-reported
clicks) are NOT collected: they require platform insights permissions that
are not part of this integration. They are reported as unavailable rather
than estimated.
"""
from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime, timedelta

from sqlalchemy import func, select
from sqlalchemy.orm import Session

from ..db import utcnow
from ..models import (
    QUALIFIED_LEAD_STATUSES,
    Campaign,
    Inquiry,
    Job,
    Lead,
    LinkClick,
    Listing,
    PageVisit,
    Post,
    SocialAccount,
    TrackingLink,
)

AWAITING_STATUSES = ("pending_approval", "scheduled", "queued", "retry_scheduled", "manual_ready")
ATTENTION_STATUSES = ("failed", "needs_verification", "blocked_auth")
UNAVAILABLE_METRICS = {
    "impressions": "Not collected: requires platform insights permissions not included in this integration.",
    "engagement": "Not collected: requires platform insights permissions not included in this integration.",
    "platform_clicks": "Not collected. 'Link clicks' counts measured clicks on our tracked links instead.",
}


def _rate(numerator: int, denominator: int) -> float | None:
    return round(100.0 * numerator / denominator, 1) if denominator else None


@dataclass
class PlatformRow:
    platform: str
    posts_published: int = 0
    posts_failed: int = 0
    clicks: int = 0
    visits: int = 0
    leads: int = 0
    qualified: int = 0

    @property
    def conversion_rate(self) -> float | None:
        return _rate(self.leads, self.visits)

    @property
    def click_to_lead_rate(self) -> float | None:
        return _rate(self.leads, self.clicks)


@dataclass
class Overview:
    active_listings: int
    marketed_listings: int
    awaiting_publication: int
    published_posts: int
    failed_posts: int
    attention_posts: int
    leads: int
    inquiries: int
    qualified_leads: int
    visits: int
    clicks: int
    failed_jobs: int
    accounts_needing_reauth: int
    platforms: list[PlatformRow] = field(default_factory=list)

    @property
    def conversion_rate(self) -> float | None:
        return _rate(self.leads, self.visits)


def overview(db: Session, since: datetime | None = None) -> Overview:
    since = since or datetime(1970, 1, 1)
    count = lambda stmt: db.scalar(stmt) or 0  # noqa: E731
    active = count(select(func.count()).select_from(Listing).where(Listing.status == "Active"))
    marketed = count(select(func.count()).select_from(Campaign).where(Campaign.status == "active"))
    awaiting = count(select(func.count()).select_from(Post).where(Post.status.in_(AWAITING_STATUSES)))
    published = count(select(func.count()).select_from(Post).where(Post.status == "published", Post.published_at >= since))
    failed = count(select(func.count()).select_from(Post).where(Post.status == "failed"))
    attention = count(select(func.count()).select_from(Post).where(Post.status.in_(ATTENTION_STATUSES)))
    leads = count(select(func.count()).select_from(Lead).where(Lead.created_at >= since))
    inquiries = count(select(func.count()).select_from(Inquiry).where(Inquiry.submitted_at >= since))
    qualified = count(select(func.count()).select_from(Lead).where(Lead.status.in_(QUALIFIED_LEAD_STATUSES), Lead.created_at >= since))
    visits = count(select(func.count()).select_from(PageVisit).where(PageVisit.is_bot.is_(False), PageVisit.occurred_at >= since))
    clicks = count(select(func.count()).select_from(LinkClick).where(LinkClick.is_bot.is_(False), LinkClick.occurred_at >= since))
    failed_jobs = count(select(func.count()).select_from(Job).where(Job.status == "dead"))
    reauth = count(select(func.count()).select_from(SocialAccount).where(SocialAccount.status == "reauth_required"))
    return Overview(active, marketed, awaiting, published, failed, attention, leads, inquiries, qualified, visits,
                    clicks, failed_jobs, reauth, platforms=by_platform(db, since))


def by_platform(db: Session, since: datetime | None = None) -> list[PlatformRow]:
    since = since or datetime(1970, 1, 1)
    rows: dict[str, PlatformRow] = {}

    def row(p: str | None) -> PlatformRow:
        key = p or "direct"
        rows.setdefault(key, PlatformRow(key))
        return rows[key]

    for platform, status, n in db.execute(select(Post.platform, Post.status, func.count()).group_by(Post.platform, Post.status)):
        if status == "published":
            row(platform).posts_published += n
        elif status in ATTENTION_STATUSES:
            row(platform).posts_failed += n
    for platform, n in db.execute(select(TrackingLink.platform, func.count()).join(LinkClick, LinkClick.tracking_link_id == TrackingLink.id)
                                  .where(LinkClick.is_bot.is_(False), LinkClick.occurred_at >= since).group_by(TrackingLink.platform)):
        row(platform).clicks += n
    for platform, n in db.execute(select(PageVisit.platform, func.count()).where(PageVisit.is_bot.is_(False), PageVisit.occurred_at >= since)
                                  .group_by(PageVisit.platform)):
        row(platform).visits += n
    for platform, status, n in db.execute(select(Lead.source_platform, Lead.status, func.count()).where(Lead.created_at >= since)
                                          .group_by(Lead.source_platform, Lead.status)):
        row(platform).leads += n
        if status in QUALIFIED_LEAD_STATUSES:
            row(platform).qualified += n
    return sorted(rows.values(), key=lambda r: (-r.leads, -r.visits, r.platform))


@dataclass
class ListingRow:
    listing_id: int
    mls_id: str
    address: str
    status: str
    campaign_status: str | None
    posts_published: int
    clicks: int
    visits: int
    leads: int
    qualified: int

    @property
    def conversion_rate(self) -> float | None:
        return _rate(self.leads, self.visits)


def by_listing(db: Session, limit: int = 100) -> list[ListingRow]:
    published = dict(db.execute(select(Post.listing_id, func.count()).where(Post.status == "published").group_by(Post.listing_id)).all())
    clicks = dict(db.execute(select(TrackingLink.listing_id, func.count()).join(LinkClick, LinkClick.tracking_link_id == TrackingLink.id)
                             .where(LinkClick.is_bot.is_(False)).group_by(TrackingLink.listing_id)).all())
    visits = dict(db.execute(select(PageVisit.listing_id, func.count()).where(PageVisit.is_bot.is_(False)).group_by(PageVisit.listing_id)).all())
    leads = dict(db.execute(select(Lead.listing_id, func.count()).group_by(Lead.listing_id)).all())
    qualified = dict(db.execute(select(Lead.listing_id, func.count()).where(Lead.status.in_(QUALIFIED_LEAD_STATUSES)).group_by(Lead.listing_id)).all())
    result = []
    for listing in db.scalars(select(Listing).order_by(Listing.id.desc()).limit(limit)):
        result.append(ListingRow(
            listing.id, listing.mls_id, listing.display_address, listing.status,
            listing.campaign.status if listing.campaign else None, published.get(listing.id, 0),
            clicks.get(listing.id, 0), visits.get(listing.id, 0), leads.get(listing.id, 0), qualified.get(listing.id, 0),
        ))
    return sorted(result, key=lambda r: (-r.leads, -r.visits, -r.listing_id))


def strongest_sources(db: Session, days: int = 90) -> list[PlatformRow]:
    rows = by_platform(db, utcnow() - timedelta(days=days))
    return [r for r in rows if r.leads > 0][:5]
