"""Public routes: listing landing pages, lead capture, tracked links, link-in-bio index."""
from __future__ import annotations

import re
import secrets

from fastapi import APIRouter, Depends, HTTPException, Request
from fastapi.responses import JSONResponse, RedirectResponse
from itsdangerous import BadSignature, URLSafeSerializer
from sqlalchemy import select, text
from sqlalchemy.orm import Session

from ..config import get_settings
from ..db import get_db
from ..models import Campaign, LinkClick, Listing, PageVisit, TrackingLink
from ..security import hash_ip
from ..services.campaigns import destination_url
from ..services.compliance import marketing_authorization
from ..services.content import fmt_open_house, listing_facts
from ..services.leads import CONSENT_TEXT_VERSION, Attribution, issue_form_token, submit_inquiry
from .deps import client_ip, render

router = APIRouter()

BOT_RE = re.compile(r"bot|crawl|spider|slurp|facebookexternalhit|facebookcatalog|preview|pinterest|linkedin|"
                    r"whatsapp|telegram|discord|curl|wget|python-requests|httpx|headless", re.I)
ATTR_COOKIE = "cp_attr"
VISITOR_COOKIE = "cp_vid"
UTM_KEYS = ("utm_source", "utm_medium", "utm_campaign", "utm_content", "utm_term")


def _is_bot(request: Request) -> bool:
    return bool(BOT_RE.search(request.headers.get("user-agent", "")))


def _attr_serializer() -> URLSafeSerializer:
    return URLSafeSerializer(get_settings().secret_key.get_secret_value(), salt="attribution")


def _public_listing(db: Session, slug: str) -> Listing:
    listing = db.scalar(select(Listing).where(Listing.slug == slug))
    if listing is None or not listing.internet_display_allowed or not marketing_authorization(listing).authorized:
        raise HTTPException(status_code=404, detail="Listing not found.")
    return listing


@router.get("/healthz")
def healthz(db: Session = Depends(get_db)):
    db.execute(text("SELECT 1"))
    return JSONResponse({"status": "ok"})


@router.get("/t/{code}")
def tracked_redirect(code: str, request: Request, db: Session = Depends(get_db)):
    link = db.scalar(select(TrackingLink).where(TrackingLink.code == code[:32]))
    if link is None:
        raise HTTPException(status_code=404, detail="Link not found.")
    listing = db.get(Listing, link.listing_id)
    db.add(LinkClick(tracking_link_id=link.id, referrer=(request.headers.get("referer") or "")[:500] or None,
                     ip_hash=hash_ip(client_ip(request)), is_bot=_is_bot(request)))
    db.commit()
    return RedirectResponse(destination_url(link, listing), status_code=302)


@router.get("/listings")
def listing_index(request: Request, db: Session = Depends(get_db)):
    """Public index of currently marketed listings (usable as an Instagram link-in-bio page)."""
    listings = [
        l for l in db.scalars(select(Listing).join(Campaign).where(Campaign.status == "active", Listing.status == "Active")
                              .order_by(Listing.mls_created_at.desc()))
        if l.internet_display_allowed and marketing_authorization(l).authorized
    ]
    return render(request, "public/index.html", {"listings": listings})


def _landing_context(request: Request, listing: Listing, attribution: dict, errors=None, form=None) -> dict:
    facts = listing_facts(listing)
    open_houses = [{"label": fmt_open_house(o), **o} for o in listing.open_houses or []] if listing.is_available else []
    return {
        "listing": listing,
        "facts": facts,
        "photos": listing.authorized_photos,
        "open_houses": open_houses,
        "form_token": issue_form_token(listing.id),
        "attribution": attribution,
        "errors": errors or [],
        "form": form or {},
        "consent_version": CONSENT_TEXT_VERSION,
        "canonical": f"{get_settings().public_base_url}/p/{listing.slug}",
    }


@router.get("/p/{slug}")
def landing_page(slug: str, request: Request, db: Session = Depends(get_db)):
    listing = _public_listing(db, slug)
    params = request.query_params
    attribution = {k: params.get(k) for k in UTM_KEYS if params.get(k)}
    if params.get("tl"):
        attribution["tl"] = params.get("tl")
    if not attribution:
        try:
            saved = _attr_serializer().loads(request.cookies.get(ATTR_COOKIE, ""))
            if saved.get("listing") == listing.id:
                attribution = saved.get("a", {})
        except BadSignature:
            attribution = {}

    link = None
    if attribution.get("tl"):
        link = db.scalar(select(TrackingLink).where(TrackingLink.code == attribution["tl"][:32]))
        if link and link.listing_id != listing.id:
            link = None
    visitor = request.cookies.get(VISITOR_COOKIE) or secrets.token_urlsafe(12)
    if params:  # count arrivals, not reloads of the clean URL from the attribution cookie
        db.add(PageVisit(
            listing_id=listing.id, campaign_id=link.campaign_id if link else (listing.campaign.id if listing.campaign else None),
            tracking_link_id=link.id if link else None,
            platform=link.platform if link else (attribution.get("utm_source") or "direct")[:40],
            utm_source=attribution.get("utm_source"), utm_medium=attribution.get("utm_medium"),
            utm_campaign=attribution.get("utm_campaign"), referrer=(request.headers.get("referer") or "")[:500] or None,
            visitor_id=visitor, ip_hash=hash_ip(client_ip(request)), is_bot=_is_bot(request),
        ))
    elif not request.cookies.get(VISITOR_COOKIE):
        db.add(PageVisit(listing_id=listing.id, campaign_id=listing.campaign.id if listing.campaign else None,
                         platform="direct", referrer=(request.headers.get("referer") or "")[:500] or None,
                         visitor_id=visitor, ip_hash=hash_ip(client_ip(request)), is_bot=_is_bot(request)))
    db.commit()
    response = render(request, "public/landing.html", _landing_context(request, listing, attribution))
    secure = get_settings().session_cookie_secure
    response.set_cookie(VISITOR_COOKIE, visitor, max_age=60 * 60 * 24 * 30, httponly=True, samesite="lax", secure=secure)
    if attribution:
        response.set_cookie(ATTR_COOKIE, _attr_serializer().dumps({"listing": listing.id, "a": attribution}),
                            max_age=60 * 60 * 24 * 30, httponly=True, samesite="lax", secure=secure)
    return response


@router.post("/p/{slug}/inquire")
async def inquire(slug: str, request: Request, db: Session = Depends(get_db)):
    listing = _public_listing(db, slug)
    form = await request.form()
    raw = {
        "full_name": str(form.get("full_name", ""))[:200],
        "email": str(form.get("email", ""))[:254],
        "phone": str(form.get("phone", ""))[:40] or None,
        "preferred_contact_method": str(form.get("preferred_contact_method", "email")),
        "message": str(form.get("message", ""))[:4000] or None,
        "marketing_consent": form.get("marketing_consent") == "yes",
        "sms_consent": form.get("sms_consent") == "yes",
    }
    attribution = Attribution(
        tracking_code=str(form.get("tl") or "") or None,
        **{k: (str(form.get(k) or "") or None) for k in UTM_KEYS},
        referrer=request.headers.get("referer"),
        landing_url=str(request.url_for("landing_page", slug=slug)),
    )
    result = submit_inquiry(db, listing, raw, attribution, ip=client_ip(request), form_token=str(form.get("form_token") or ""),
                            honeypot=str(form.get("website") or ""))
    db.commit()
    if result.status in ("created", "duplicate"):
        return RedirectResponse(f"/p/{slug}/thanks", status_code=303)
    if result.status == "spam" and not result.errors:
        return RedirectResponse(f"/p/{slug}/thanks", status_code=303)  # don't reveal honeypot detection
    attr = {k: v for k, v in {"tl": attribution.tracking_code, **{k: getattr(attribution, k) for k in UTM_KEYS}}.items() if v}
    code = 429 if result.status == "rate_limited" else 422
    return render(request, "public/landing.html",
                  _landing_context(request, listing, attr, errors=result.errors, form=raw), status_code=code)


@router.get("/p/{slug}/thanks")
def thanks(slug: str, request: Request, db: Session = Depends(get_db)):
    listing = _public_listing(db, slug)
    return render(request, "public/thanks.html", {"listing": listing})


@router.get("/privacy")
def privacy(request: Request):
    return render(request, "public/privacy.html", {"consent_version": CONSENT_TEXT_VERSION})
