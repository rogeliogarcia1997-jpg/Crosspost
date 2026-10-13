"""ORM models (relational schema).

Schema changes must be accompanied by an Alembic migration in ``migrations/``.
"""
from __future__ import annotations

from datetime import datetime
from decimal import Decimal
from typing import Any

from sqlalchemy import (
    JSON,
    BigInteger,
    Boolean,
    DateTime,
    ForeignKey,
    Index,
    Integer,
    Numeric,
    String,
    Text,
    UniqueConstraint,
)
from sqlalchemy.orm import Mapped, mapped_column, relationship

from .db import Base, utcnow

# Listing statuses (RESO StandardStatus vocabulary) that permit "available" marketing.
AVAILABLE_STATUSES = {"Active"}
UNAVAILABLE_STATUSES = {
    "Active Under Contract", "Pending", "Closed", "Withdrawn", "Expired", "Canceled", "Hold",
    "Delete", "Incomplete", "Coming Soon", "Removed From Feed", "Unknown",
}

ROLE_ADMIN = "admin"
ROLE_MARKETER = "marketer"
ROLE_VIEWER = "viewer"
ROLES = (ROLE_ADMIN, ROLE_MARKETER, ROLE_VIEWER)


class TimestampMixin:
    created_at: Mapped[datetime] = mapped_column(DateTime, default=utcnow, nullable=False)
    updated_at: Mapped[datetime] = mapped_column(DateTime, default=utcnow, onupdate=utcnow, nullable=False)


class User(TimestampMixin, Base):
    __tablename__ = "users"
    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    email: Mapped[str] = mapped_column(String(255), unique=True, nullable=False)
    password_hash: Mapped[str] = mapped_column(String(255), nullable=False)
    role: Mapped[str] = mapped_column(String(20), default=ROLE_VIEWER, nullable=False)
    is_active: Mapped[bool] = mapped_column(Boolean, default=True, nullable=False)
    last_login_at: Mapped[datetime | None] = mapped_column(DateTime)


class SyncRun(Base):
    __tablename__ = "sync_runs"
    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    provider: Mapped[str] = mapped_column(String(50), nullable=False)
    mode: Mapped[str] = mapped_column(String(20), nullable=False)  # incremental | full | single
    status: Mapped[str] = mapped_column(String(20), default="running", nullable=False)
    started_at: Mapped[datetime] = mapped_column(DateTime, default=utcnow, nullable=False)
    finished_at: Mapped[datetime | None] = mapped_column(DateTime)
    fetched_count: Mapped[int] = mapped_column(Integer, default=0)
    created_count: Mapped[int] = mapped_column(Integer, default=0)
    updated_count: Mapped[int] = mapped_column(Integer, default=0)
    unchanged_count: Mapped[int] = mapped_column(Integer, default=0)
    error: Mapped[str | None] = mapped_column(Text)
    high_water_mark: Mapped[datetime | None] = mapped_column(DateTime)
    triggered_by: Mapped[str | None] = mapped_column(String(100))


class Listing(TimestampMixin, Base):
    __tablename__ = "listings"
    __table_args__ = (UniqueConstraint("provider", "mls_id", name="uq_listing_provider_mlsid"),)

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    provider: Mapped[str] = mapped_column(String(50), nullable=False)
    mls_id: Mapped[str] = mapped_column(String(100), nullable=False)
    listing_key: Mapped[str | None] = mapped_column(String(100))
    slug: Mapped[str] = mapped_column(String(200), unique=True, nullable=False)
    status: Mapped[str] = mapped_column(String(40), nullable=False)
    status_raw: Mapped[str | None] = mapped_column(String(80))

    street_address: Mapped[str | None] = mapped_column(String(255))
    unit_number: Mapped[str | None] = mapped_column(String(50))
    city: Mapped[str | None] = mapped_column(String(100))
    state: Mapped[str | None] = mapped_column(String(50))
    postal_code: Mapped[str | None] = mapped_column(String(20))
    address_display_allowed: Mapped[bool] = mapped_column(Boolean, default=True)
    internet_display_allowed: Mapped[bool] = mapped_column(Boolean, default=True)

    price: Mapped[Decimal | None] = mapped_column(Numeric(14, 2))
    original_price: Mapped[Decimal | None] = mapped_column(Numeric(14, 2))
    property_type: Mapped[str | None] = mapped_column(String(80))
    property_subtype: Mapped[str | None] = mapped_column(String(80))
    bedrooms: Mapped[int | None] = mapped_column(Integer)
    bathrooms_total: Mapped[Decimal | None] = mapped_column(Numeric(5, 1))
    bathrooms_full: Mapped[int | None] = mapped_column(Integer)
    bathrooms_half: Mapped[int | None] = mapped_column(Integer)
    living_area_sqft: Mapped[int | None] = mapped_column(Integer)
    lot_size_sqft: Mapped[int | None] = mapped_column(BigInteger)
    lot_size_acres: Mapped[Decimal | None] = mapped_column(Numeric(10, 3))
    year_built: Mapped[int | None] = mapped_column(Integer)
    description: Mapped[str | None] = mapped_column(Text)
    features: Mapped[list[str]] = mapped_column(JSON, default=list)
    photos: Mapped[list[dict[str, Any]]] = mapped_column(JSON, default=list)
    open_houses: Mapped[list[dict[str, Any]]] = mapped_column(JSON, default=list)

    list_agent_id: Mapped[str | None] = mapped_column(String(100))
    list_agent_name: Mapped[str | None] = mapped_column(String(200))
    list_agent_phone: Mapped[str | None] = mapped_column(String(50))
    list_agent_email: Mapped[str | None] = mapped_column(String(255))
    list_office_id: Mapped[str | None] = mapped_column(String(100))
    list_office_name: Mapped[str | None] = mapped_column(String(200))
    list_office_phone: Mapped[str | None] = mapped_column(String(50))
    listing_url: Mapped[str | None] = mapped_column(String(500))

    mls_created_at: Mapped[datetime | None] = mapped_column(DateTime)
    mls_modified_at: Mapped[datetime | None] = mapped_column(DateTime)
    content_hash: Mapped[str] = mapped_column(String(64), nullable=False)
    raw: Mapped[dict[str, Any]] = mapped_column(JSON, default=dict)

    # None = follow MARKETING_SCOPE rules; True/False = explicit admin decision.
    marketing_override: Mapped[bool | None] = mapped_column(Boolean)
    first_seen_at: Mapped[datetime] = mapped_column(DateTime, default=utcnow)
    last_synced_at: Mapped[datetime] = mapped_column(DateTime, default=utcnow)

    campaign: Mapped["Campaign | None"] = relationship(back_populates="listing", uselist=False)
    changes: Mapped[list["ListingChange"]] = relationship(back_populates="listing", order_by="ListingChange.id")

    @property
    def is_available(self) -> bool:
        return self.status in AVAILABLE_STATUSES

    @property
    def display_address(self) -> str:
        if not self.address_display_allowed or not self.street_address:
            return ", ".join(p for p in [self.city, self.state] if p)
        street = self.street_address + (f" #{self.unit_number}" if self.unit_number else "")
        return f"{street}, {self.city}, {self.state} {self.postal_code or ''}".strip()

    @property
    def authorized_photos(self) -> list[dict[str, Any]]:
        return [p for p in (self.photos or []) if p.get("authorized", True) and p.get("url")]


class ListingChange(Base):
    __tablename__ = "listing_changes"
    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    listing_id: Mapped[int] = mapped_column(ForeignKey("listings.id", ondelete="CASCADE"), nullable=False, index=True)
    sync_run_id: Mapped[int | None] = mapped_column(ForeignKey("sync_runs.id", ondelete="SET NULL"))
    change_type: Mapped[str] = mapped_column(String(40), nullable=False)
    old_value: Mapped[Any] = mapped_column(JSON)
    new_value: Mapped[Any] = mapped_column(JSON)
    detected_at: Mapped[datetime] = mapped_column(DateTime, default=utcnow, nullable=False)

    listing: Mapped[Listing] = relationship(back_populates="changes")


class Campaign(TimestampMixin, Base):
    __tablename__ = "campaigns"
    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    listing_id: Mapped[int] = mapped_column(ForeignKey("listings.id", ondelete="CASCADE"), unique=True, nullable=False)
    code: Mapped[str] = mapped_column(String(64), unique=True, nullable=False)
    status: Mapped[str] = mapped_column(String(20), default="active", nullable=False)  # active|ended
    ended_at: Mapped[datetime | None] = mapped_column(DateTime)
    end_reason: Mapped[str | None] = mapped_column(String(200))
    last_repost_at: Mapped[datetime | None] = mapped_column(DateTime)
    repost_count: Mapped[int] = mapped_column(Integer, default=0)

    listing: Mapped[Listing] = relationship(back_populates="campaign")
    posts: Mapped[list["Post"]] = relationship(back_populates="campaign", order_by="Post.id")


class SocialAccount(TimestampMixin, Base):
    __tablename__ = "social_accounts"
    __table_args__ = (UniqueConstraint("platform", "external_account_id", name="uq_social_platform_external"),)
    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    platform: Mapped[str] = mapped_column(String(40), nullable=False)
    display_name: Mapped[str] = mapped_column(String(200), nullable=False)
    external_account_id: Mapped[str] = mapped_column(String(200), nullable=False)
    status: Mapped[str] = mapped_column(String(30), default="connected", nullable=False)
    enabled: Mapped[bool] = mapped_column(Boolean, default=True, nullable=False)
    access_token_enc: Mapped[str | None] = mapped_column(Text)
    refresh_token_enc: Mapped[str | None] = mapped_column(Text)
    token_expires_at: Mapped[datetime | None] = mapped_column(DateTime)
    granted_scopes: Mapped[str | None] = mapped_column(Text)
    settings: Mapped[dict[str, Any]] = mapped_column(JSON, default=dict)
    last_checked_at: Mapped[datetime | None] = mapped_column(DateTime)
    last_error: Mapped[str | None] = mapped_column(Text)


class Post(TimestampMixin, Base):
    __tablename__ = "posts"
    __table_args__ = (
        UniqueConstraint("campaign_id", "social_account_id", "kind", "event_key", name="uq_post_idempotency"),
        Index("ix_posts_status", "status"),
    )
    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    campaign_id: Mapped[int] = mapped_column(ForeignKey("campaigns.id", ondelete="CASCADE"), nullable=False)
    listing_id: Mapped[int] = mapped_column(ForeignKey("listings.id", ondelete="CASCADE"), nullable=False, index=True)
    social_account_id: Mapped[int] = mapped_column(ForeignKey("social_accounts.id", ondelete="CASCADE"), nullable=False)
    platform: Mapped[str] = mapped_column(String(40), nullable=False)
    kind: Mapped[str] = mapped_column(String(30), nullable=False)
    event_key: Mapped[str] = mapped_column(String(120), nullable=False)
    status: Mapped[str] = mapped_column(String(30), nullable=False)
    text: Mapped[str] = mapped_column(Text, default="")
    title: Mapped[str | None] = mapped_column(String(300))
    link_url: Mapped[str | None] = mapped_column(String(500))
    media: Mapped[list[dict[str, Any]]] = mapped_column(JSON, default=list)
    hashtags: Mapped[list[str]] = mapped_column(JSON, default=list)
    warnings: Mapped[list[str]] = mapped_column(JSON, default=list)
    context: Mapped[dict[str, Any]] = mapped_column(JSON, default=dict)  # event data used to render (e.g. old price)
    listing_version: Mapped[str | None] = mapped_column(String(64))
    requires_approval: Mapped[bool] = mapped_column(Boolean, default=False)
    scheduled_for: Mapped[datetime | None] = mapped_column(DateTime)
    approved_by_id: Mapped[int | None] = mapped_column(ForeignKey("users.id", ondelete="SET NULL"))
    approved_at: Mapped[datetime | None] = mapped_column(DateTime)
    rejected_reason: Mapped[str | None] = mapped_column(String(500))
    external_post_id: Mapped[str | None] = mapped_column(String(200))
    external_post_url: Mapped[str | None] = mapped_column(String(500))
    published_at: Mapped[datetime | None] = mapped_column(DateTime)
    attempts: Mapped[int] = mapped_column(Integer, default=0)
    last_error: Mapped[str | None] = mapped_column(Text)
    error_category: Mapped[str | None] = mapped_column(String(40))

    campaign: Mapped[Campaign] = relationship(back_populates="posts")
    listing: Mapped[Listing] = relationship()
    social_account: Mapped[SocialAccount] = relationship()
    attempts_log: Mapped[list["PublishAttempt"]] = relationship(back_populates="post", order_by="PublishAttempt.id")
    tracking_link: Mapped["TrackingLink | None"] = relationship(back_populates="post", uselist=False)


class PublishAttempt(Base):
    __tablename__ = "publish_attempts"
    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    post_id: Mapped[int] = mapped_column(ForeignKey("posts.id", ondelete="CASCADE"), nullable=False, index=True)
    attempt_no: Mapped[int] = mapped_column(Integer, nullable=False)
    started_at: Mapped[datetime] = mapped_column(DateTime, default=utcnow, nullable=False)
    finished_at: Mapped[datetime | None] = mapped_column(DateTime)
    outcome: Mapped[str] = mapped_column(String(30), default="started")
    http_status: Mapped[int | None] = mapped_column(Integer)
    message: Mapped[str | None] = mapped_column(Text)

    post: Mapped[Post] = relationship(back_populates="attempts_log")


class TrackingLink(Base):
    __tablename__ = "tracking_links"
    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    code: Mapped[str] = mapped_column(String(32), unique=True, nullable=False)
    campaign_id: Mapped[int] = mapped_column(ForeignKey("campaigns.id", ondelete="CASCADE"), nullable=False, index=True)
    listing_id: Mapped[int] = mapped_column(ForeignKey("listings.id", ondelete="CASCADE"), nullable=False)
    post_id: Mapped[int | None] = mapped_column(ForeignKey("posts.id", ondelete="SET NULL"), unique=True)
    platform: Mapped[str] = mapped_column(String(40), nullable=False)
    utm_source: Mapped[str] = mapped_column(String(80), nullable=False)
    utm_medium: Mapped[str] = mapped_column(String(80), nullable=False)
    utm_campaign: Mapped[str] = mapped_column(String(120), nullable=False)
    utm_content: Mapped[str | None] = mapped_column(String(120))
    created_at: Mapped[datetime] = mapped_column(DateTime, default=utcnow, nullable=False)

    post: Mapped[Post | None] = relationship(back_populates="tracking_link")


class LinkClick(Base):
    __tablename__ = "link_clicks"
    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    tracking_link_id: Mapped[int] = mapped_column(ForeignKey("tracking_links.id", ondelete="CASCADE"), nullable=False, index=True)
    occurred_at: Mapped[datetime] = mapped_column(DateTime, default=utcnow, nullable=False)
    referrer: Mapped[str | None] = mapped_column(String(500))
    ip_hash: Mapped[str | None] = mapped_column(String(64))
    is_bot: Mapped[bool] = mapped_column(Boolean, default=False)


class PageVisit(Base):
    __tablename__ = "page_visits"
    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    listing_id: Mapped[int] = mapped_column(ForeignKey("listings.id", ondelete="CASCADE"), nullable=False, index=True)
    campaign_id: Mapped[int | None] = mapped_column(ForeignKey("campaigns.id", ondelete="SET NULL"))
    tracking_link_id: Mapped[int | None] = mapped_column(ForeignKey("tracking_links.id", ondelete="SET NULL"))
    platform: Mapped[str | None] = mapped_column(String(40))
    utm_source: Mapped[str | None] = mapped_column(String(80))
    utm_medium: Mapped[str | None] = mapped_column(String(80))
    utm_campaign: Mapped[str | None] = mapped_column(String(120))
    referrer: Mapped[str | None] = mapped_column(String(500))
    visitor_id: Mapped[str | None] = mapped_column(String(64))
    ip_hash: Mapped[str | None] = mapped_column(String(64))
    is_bot: Mapped[bool] = mapped_column(Boolean, default=False)
    occurred_at: Mapped[datetime] = mapped_column(DateTime, default=utcnow, nullable=False)


class Contact(TimestampMixin, Base):
    __tablename__ = "contacts"
    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    email: Mapped[str] = mapped_column(String(255), unique=True, nullable=False)
    full_name: Mapped[str] = mapped_column(String(200), nullable=False)
    phone: Mapped[str | None] = mapped_column(String(40))


LEAD_STATUSES = ("new", "contacted", "qualified", "unqualified", "nurturing", "closed", "archived")
QUALIFIED_LEAD_STATUSES = {"qualified", "closed"}


class Lead(TimestampMixin, Base):
    __tablename__ = "leads"
    __table_args__ = (UniqueConstraint("contact_id", "listing_id", name="uq_lead_contact_listing"),)
    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    contact_id: Mapped[int] = mapped_column(ForeignKey("contacts.id", ondelete="CASCADE"), nullable=False)
    listing_id: Mapped[int] = mapped_column(ForeignKey("listings.id", ondelete="CASCADE"), nullable=False, index=True)
    campaign_id: Mapped[int | None] = mapped_column(ForeignKey("campaigns.id", ondelete="SET NULL"))
    source_platform: Mapped[str] = mapped_column(String(40), default="direct", nullable=False)
    source_tracking_link_id: Mapped[int | None] = mapped_column(ForeignKey("tracking_links.id", ondelete="SET NULL"))
    status: Mapped[str] = mapped_column(String(20), default="new", nullable=False)
    preferred_contact_method: Mapped[str] = mapped_column(String(10), default="email")
    marketing_consent: Mapped[bool] = mapped_column(Boolean, default=False)
    marketing_consent_at: Mapped[datetime | None] = mapped_column(DateTime)
    sms_consent: Mapped[bool] = mapped_column(Boolean, default=False)
    sms_consent_at: Mapped[datetime | None] = mapped_column(DateTime)
    notes: Mapped[str | None] = mapped_column(Text)
    last_inquiry_at: Mapped[datetime] = mapped_column(DateTime, default=utcnow)

    contact: Mapped[Contact] = relationship()
    listing: Mapped[Listing] = relationship()
    campaign: Mapped[Campaign | None] = relationship()
    inquiries: Mapped[list["Inquiry"]] = relationship(back_populates="lead", order_by="Inquiry.id")


class Inquiry(Base):
    __tablename__ = "inquiries"
    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    lead_id: Mapped[int] = mapped_column(ForeignKey("leads.id", ondelete="CASCADE"), nullable=False, index=True)
    listing_id: Mapped[int] = mapped_column(ForeignKey("listings.id", ondelete="CASCADE"), nullable=False)
    campaign_id: Mapped[int | None] = mapped_column(ForeignKey("campaigns.id", ondelete="SET NULL"))
    tracking_link_id: Mapped[int | None] = mapped_column(ForeignKey("tracking_links.id", ondelete="SET NULL"))
    platform: Mapped[str] = mapped_column(String(40), default="direct", nullable=False)
    message: Mapped[str | None] = mapped_column(Text)
    preferred_contact_method: Mapped[str] = mapped_column(String(10), default="email")
    referrer_url: Mapped[str | None] = mapped_column(String(500))
    landing_url: Mapped[str | None] = mapped_column(String(500))
    utm_source: Mapped[str | None] = mapped_column(String(80))
    utm_medium: Mapped[str | None] = mapped_column(String(80))
    utm_campaign: Mapped[str | None] = mapped_column(String(120))
    utm_content: Mapped[str | None] = mapped_column(String(120))
    utm_term: Mapped[str | None] = mapped_column(String(120))
    ip_hash: Mapped[str | None] = mapped_column(String(64))
    consent_text_version: Mapped[str | None] = mapped_column(String(20))
    submitted_at: Mapped[datetime] = mapped_column(DateTime, default=utcnow, nullable=False)

    lead: Mapped[Lead] = relationship(back_populates="inquiries")


class Notification(Base):
    __tablename__ = "notifications"
    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    kind: Mapped[str] = mapped_column(String(40), nullable=False)
    recipient: Mapped[str] = mapped_column(String(255), nullable=False)
    subject: Mapped[str] = mapped_column(String(300), nullable=False)
    status: Mapped[str] = mapped_column(String(20), default="pending")
    lead_id: Mapped[int | None] = mapped_column(ForeignKey("leads.id", ondelete="SET NULL"))
    error: Mapped[str | None] = mapped_column(Text)
    created_at: Mapped[datetime] = mapped_column(DateTime, default=utcnow, nullable=False)
    sent_at: Mapped[datetime | None] = mapped_column(DateTime)


class CRMSync(Base):
    __tablename__ = "crm_syncs"
    __table_args__ = (UniqueConstraint("inquiry_id", "provider", name="uq_crm_inquiry_provider"),)
    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    lead_id: Mapped[int] = mapped_column(ForeignKey("leads.id", ondelete="CASCADE"), nullable=False)
    inquiry_id: Mapped[int] = mapped_column(ForeignKey("inquiries.id", ondelete="CASCADE"), nullable=False)
    provider: Mapped[str] = mapped_column(String(40), nullable=False)
    status: Mapped[str] = mapped_column(String(20), default="pending")
    external_id: Mapped[str | None] = mapped_column(String(200))
    last_error: Mapped[str | None] = mapped_column(Text)
    synced_at: Mapped[datetime | None] = mapped_column(DateTime)
    created_at: Mapped[datetime] = mapped_column(DateTime, default=utcnow, nullable=False)


class Job(Base):
    __tablename__ = "jobs"
    __table_args__ = (Index("ix_jobs_ready", "status", "run_at"),)
    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    type: Mapped[str] = mapped_column(String(60), nullable=False)
    payload: Mapped[dict[str, Any]] = mapped_column(JSON, default=dict)
    status: Mapped[str] = mapped_column(String(20), default="queued", nullable=False)
    run_at: Mapped[datetime] = mapped_column(DateTime, default=utcnow, nullable=False)
    attempts: Mapped[int] = mapped_column(Integer, default=0, nullable=False)
    max_attempts: Mapped[int] = mapped_column(Integer, default=6, nullable=False)
    idempotency_key: Mapped[str | None] = mapped_column(String(200), unique=True)
    locked_by: Mapped[str | None] = mapped_column(String(100))
    locked_at: Mapped[datetime | None] = mapped_column(DateTime)
    last_error: Mapped[str | None] = mapped_column(Text)
    created_at: Mapped[datetime] = mapped_column(DateTime, default=utcnow, nullable=False)
    finished_at: Mapped[datetime | None] = mapped_column(DateTime)


class AuditLog(Base):
    __tablename__ = "audit_logs"
    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    user_id: Mapped[int | None] = mapped_column(ForeignKey("users.id", ondelete="SET NULL"))
    action: Mapped[str] = mapped_column(String(80), nullable=False)
    entity_type: Mapped[str | None] = mapped_column(String(40))
    entity_id: Mapped[str | None] = mapped_column(String(64))
    details: Mapped[dict[str, Any]] = mapped_column(JSON, default=dict)
    created_at: Mapped[datetime] = mapped_column(DateTime, default=utcnow, nullable=False)


class AppSetting(Base):
    __tablename__ = "app_settings"
    key: Mapped[str] = mapped_column(String(80), primary_key=True)
    value: Mapped[Any] = mapped_column(JSON)
    updated_at: Mapped[datetime] = mapped_column(DateTime, default=utcnow, onupdate=utcnow)


class RateLimitHit(Base):
    __tablename__ = "rate_limit_hits"
    __table_args__ = (Index("ix_rate_limit_lookup", "bucket", "key_hash", "created_at"),)
    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    bucket: Mapped[str] = mapped_column(String(40), nullable=False)
    key_hash: Mapped[str] = mapped_column(String(64), nullable=False)
    created_at: Mapped[datetime] = mapped_column(DateTime, default=utcnow, nullable=False)
