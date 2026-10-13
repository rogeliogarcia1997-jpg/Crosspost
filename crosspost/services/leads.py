"""Buyer lead capture: validation, spam prevention, attribution, persistence."""
from __future__ import annotations

import re
from dataclasses import dataclass
from datetime import datetime, timedelta
from typing import Literal

from itsdangerous import BadSignature, SignatureExpired, URLSafeTimedSerializer
from pydantic import BaseModel, EmailStr, Field, ValidationError, field_validator, model_validator
from sqlalchemy import select
from sqlalchemy.orm import Session

from ..config import get_settings
from ..db import utcnow
from ..jobs.queue import enqueue
from ..logging_setup import get_logger, log_event
from ..models import Campaign, Contact, Inquiry, Lead, Listing, TrackingLink
from ..security import hash_ip, rate_limited

log = get_logger(__name__)

CONSENT_TEXT_VERSION = "2026-10"
_URL_RE = re.compile(r"https?://|www\.", re.I)
_PHONE_RE = re.compile(r"^\+?[0-9 ().-]{7,25}$")
_SAFE_TOKEN_RE = re.compile(r"[^a-zA-Z0-9_.:-]")


class LeadSubmission(BaseModel):
    full_name: str = Field(min_length=2, max_length=120)
    email: EmailStr
    phone: str | None = Field(default=None, max_length=30)
    preferred_contact_method: Literal["email", "phone", "text"] = "email"
    message: str | None = Field(default=None, max_length=2000)
    marketing_consent: bool = False
    sms_consent: bool = False

    @field_validator("full_name")
    @classmethod
    def _name(cls, v: str) -> str:
        v = " ".join(v.split())
        if _URL_RE.search(v) or re.search(r"[<>{}]", v):
            raise ValueError("Please enter a valid name.")
        return v

    @field_validator("phone")
    @classmethod
    def _phone(cls, v: str | None) -> str | None:
        if v is None or not v.strip():
            return None
        v = v.strip()
        if not _PHONE_RE.match(v) or len(re.sub(r"\D", "", v)) < 7:
            raise ValueError("Please enter a valid phone number.")
        return v

    @field_validator("message")
    @classmethod
    def _message(cls, v: str | None) -> str | None:
        if v is None:
            return None
        v = v.strip()
        if len(_URL_RE.findall(v)) > 2:
            raise ValueError("Please remove links from your message.")
        return v or None

    @model_validator(mode="after")
    def _contact_rules(self) -> "LeadSubmission":
        if self.preferred_contact_method in ("phone", "text") and not self.phone:
            raise ValueError("Please provide a phone number to be contacted by phone or text.")
        if self.preferred_contact_method == "text" and not self.sms_consent:
            raise ValueError("Please confirm you agree to receive text messages about your inquiry.")
        return self


@dataclass
class Attribution:
    tracking_code: str | None = None
    utm_source: str | None = None
    utm_medium: str | None = None
    utm_campaign: str | None = None
    utm_content: str | None = None
    utm_term: str | None = None
    referrer: str | None = None
    landing_url: str | None = None


@dataclass
class SubmissionResult:
    status: Literal["created", "duplicate", "spam", "rate_limited", "invalid"]
    lead: Lead | None = None
    inquiry: Inquiry | None = None
    errors: list[str] | None = None


def _serializer() -> URLSafeTimedSerializer:
    return URLSafeTimedSerializer(get_settings().secret_key.get_secret_value(), salt="lead-form")


def issue_form_token(listing_id: int, now: datetime | None = None) -> str:
    issued = (now or utcnow()).timestamp()
    return _serializer().dumps({"l": listing_id, "t": issued})


def check_form_token(token: str | None, listing_id: int, now: datetime | None = None) -> bool:
    if not token:
        return False
    try:
        data = _serializer().loads(token, max_age=6 * 3600)
    except (BadSignature, SignatureExpired):
        return False
    if data.get("l") != listing_id:
        return False
    elapsed = (now or utcnow()).timestamp() - float(data.get("t", 0))
    return elapsed >= get_settings().lead_form_min_seconds


def _clean(value: str | None, limit: int) -> str | None:
    if not value:
        return None
    return _SAFE_TOKEN_RE.sub("", value)[:limit] or None


def resolve_attribution(db: Session, listing: Listing, attr: Attribution) -> tuple[str, Campaign | None, TrackingLink | None]:
    link = None
    if attr.tracking_code:
        link = db.scalar(select(TrackingLink).where(TrackingLink.code == _clean(attr.tracking_code, 32)))
        if link and link.listing_id != listing.id:
            link = None
    if link:
        return link.platform, db.get(Campaign, link.campaign_id), link
    campaign = listing.campaign
    source = _clean(attr.utm_source, 40)
    return (source or "direct"), campaign, None


def submit_inquiry(
    db: Session,
    listing: Listing,
    raw_form: dict,
    attribution: Attribution,
    *,
    ip: str | None,
    form_token: str | None,
    honeypot: str | None,
    now: datetime | None = None,
) -> SubmissionResult:
    now = now or utcnow()
    settings = get_settings()
    if honeypot:
        log_event(log, "lead_spam_honeypot", listing_id=listing.id)
        return SubmissionResult("spam")
    if not check_form_token(form_token, listing.id, now):
        log_event(log, "lead_spam_token", listing_id=listing.id)
        return SubmissionResult("spam", errors=["Your session expired or the form was submitted too quickly. Please try again."])
    try:
        form = LeadSubmission(**raw_form)
    except ValidationError as exc:
        return SubmissionResult("invalid", errors=[e["msg"].removeprefix("Value error, ") for e in exc.errors()])

    ip_hash = hash_ip(ip)
    if ip_hash and rate_limited(db, "lead_ip", ip_hash, settings.lead_rate_limit_per_10min, 600):
        return SubmissionResult("rate_limited", errors=["Too many submissions. Please try again later."])
    email = form.email.lower()
    if rate_limited(db, "lead_email", email, settings.lead_rate_limit_per_10min, 600):
        return SubmissionResult("rate_limited", errors=["Too many submissions. Please try again later."])

    platform, campaign, link = resolve_attribution(db, listing, attribution)

    contact = db.scalar(select(Contact).where(Contact.email == email))
    if contact is None:
        contact = Contact(email=email, full_name=form.full_name, phone=form.phone)
        db.add(contact)
        db.flush()
    else:
        contact.full_name = form.full_name
        if form.phone:
            contact.phone = form.phone

    lead = db.scalar(select(Lead).where(Lead.contact_id == contact.id, Lead.listing_id == listing.id))
    if lead is None:
        lead = Lead(contact_id=contact.id, listing_id=listing.id, campaign_id=campaign.id if campaign else None,
                    source_platform=platform, source_tracking_link_id=link.id if link else None, status="new")
        db.add(lead)
    else:
        recent = db.scalar(select(Inquiry).where(Inquiry.lead_id == lead.id,
                                                 Inquiry.submitted_at >= now - timedelta(minutes=10))
                           .order_by(Inquiry.id.desc()).limit(1))
        if recent and (recent.message or "") == (form.message or ""):
            return SubmissionResult("duplicate", lead=lead, inquiry=recent)
    lead.preferred_contact_method = form.preferred_contact_method
    lead.last_inquiry_at = now
    if form.marketing_consent and not lead.marketing_consent:
        lead.marketing_consent, lead.marketing_consent_at = True, now
    if form.sms_consent and not lead.sms_consent:
        lead.sms_consent, lead.sms_consent_at = True, now
    db.flush()

    inquiry = Inquiry(
        lead_id=lead.id, listing_id=listing.id, campaign_id=campaign.id if campaign else None,
        tracking_link_id=link.id if link else None, platform=platform, message=form.message,
        preferred_contact_method=form.preferred_contact_method,
        referrer_url=(attribution.referrer or "")[:500] or None, landing_url=(attribution.landing_url or "")[:500] or None,
        utm_source=_clean(attribution.utm_source, 80) or (link.utm_source if link else None),
        utm_medium=_clean(attribution.utm_medium, 80) or (link.utm_medium if link else None),
        utm_campaign=_clean(attribution.utm_campaign, 120) or (link.utm_campaign if link else None),
        utm_content=_clean(attribution.utm_content, 120) or (link.utm_content if link else None),
        utm_term=_clean(attribution.utm_term, 120), ip_hash=ip_hash, consent_text_version=CONSENT_TEXT_VERSION,
        submitted_at=now,
    )
    db.add(inquiry)
    db.flush()
    enqueue(db, "notify_lead", {"inquiry_id": inquiry.id}, idempotency_key=f"notify_lead:{inquiry.id}")
    if settings.crm_provider != "none":
        enqueue(db, "crm_push", {"inquiry_id": inquiry.id}, idempotency_key=f"crm_push:{inquiry.id}")
    log_event(log, "lead_captured", lead_id=lead.id, inquiry_id=inquiry.id, listing_id=listing.id, platform=platform)
    return SubmissionResult("created", lead=lead, inquiry=inquiry)


def erase_contact(db: Session, contact: Contact) -> int:
    """Delete a buyer's personal data (privacy request): contact, leads, inquiries, CRM sync rows.

    Returns the number of leads removed. Aggregate counts (page visits, clicks) contain no PII and remain.
    """
    leads = list(db.scalars(select(Lead).where(Lead.contact_id == contact.id)))
    for lead in leads:
        for inquiry in list(lead.inquiries):
            db.delete(inquiry)
        db.delete(lead)
    db.flush()
    db.delete(contact)
    db.flush()
    return len(leads)


def purge_expired_leads(db: Session, retention_days: int, now: datetime | None = None) -> int:
    """Remove inquiries older than the retention period, then leads and contacts left without any."""
    if retention_days <= 0:
        return 0
    cutoff = (now or utcnow()) - timedelta(days=retention_days)
    removed = 0
    for inquiry in db.scalars(select(Inquiry).where(Inquiry.submitted_at < cutoff)):
        db.delete(inquiry)
        removed += 1
    db.flush()
    for lead in db.scalars(select(Lead).where(~Lead.inquiries.any())):
        db.delete(lead)
    db.flush()
    for contact in db.scalars(select(Contact).where(~select(Lead.id).where(Lead.contact_id == Contact.id).exists())):
        db.delete(contact)
    db.flush()
    return removed
