"""Administrative dashboard."""
from __future__ import annotations

import csv
import io
from datetime import timedelta

from fastapi import APIRouter, Depends, Form, HTTPException, Request
from fastapi.responses import RedirectResponse, StreamingResponse
from sqlalchemy import func, or_, select
from sqlalchemy.orm import Session

from ..config import get_settings
from ..db import get_db, utcnow
from ..jobs.queue import enqueue
from ..models import (
    LEAD_STATUSES,
    ROLES,
    Contact,
    CRMSync,
    Job,
    Lead,
    Listing,
    Post,
    SocialAccount,
    SyncRun,
    TrackingLink,
    User,
)
from ..security import hash_password, password_problems
from ..services import analytics
from ..services.accounts import check_account, connect_account, disconnect_account, missing_scopes
from ..services.compliance import marketing_authorization
from ..services.publishing import (
    ActionError,
    approve_post,
    mark_manually_published,
    plan_posts,
    reject_post,
    retry_post,
    update_post_text,
)
from ..services.rules import PUBLISH_MODES, get_rules, save_rules
from ..social.base import TokenSet
from ..social.registry import ADAPTERS, all_specs, get_adapter
from .deps import (
    ROLE_RANK,
    audit,
    flash,
    render,
    require_admin,
    require_marketer,
    require_viewer,
    verify_csrf,
)

router = APIRouter(prefix="/admin")
csrf = [Depends(verify_csrf)]

POST_STATUS_GROUPS = {
    "attention": ("failed", "needs_verification", "blocked_auth"),
    "awaiting": ("pending_approval", "manual_ready"),
    "scheduled": ("scheduled", "queued", "retry_scheduled", "publishing"),
    "published": ("published",),
    "closed": ("rejected", "canceled", "skipped", "retracted"),
}


def _back(request: Request, fallback: str) -> RedirectResponse:
    ref = request.headers.get("referer") or ""
    target = fallback
    base = str(request.base_url)
    if ref.startswith(base) and "/admin" in ref:
        target = "/" + ref[len(base):]
    return RedirectResponse(target, status_code=303)


def _can_view_pii(user: User) -> bool:
    return ROLE_RANK.get(user.role, 0) >= ROLE_RANK["marketer"]


# ----------------------------------------------------------------------------- overview
@router.get("")
def overview(request: Request, user: User = Depends(require_viewer), db: Session = Depends(get_db)):
    stats = analytics.overview(db)
    recent_leads = list(db.scalars(select(Lead).order_by(Lead.last_inquiry_at.desc()).limit(8))) if _can_view_pii(user) else []
    attention = list(db.scalars(select(Post).where(Post.status.in_(POST_STATUS_GROUPS["attention"])).order_by(Post.updated_at.desc()).limit(10)))
    dead_jobs = list(db.scalars(select(Job).where(Job.status == "dead").order_by(Job.id.desc()).limit(10)))
    reauth = list(db.scalars(select(SocialAccount).where(SocialAccount.status == "reauth_required")))
    runs = list(db.scalars(select(SyncRun).order_by(SyncRun.id.desc()).limit(5)))
    recent_posts = list(db.scalars(select(Post).where(Post.status == "published").order_by(Post.published_at.desc()).limit(8)))
    return render(request, "admin/overview.html", {
        "stats": stats, "recent_leads": recent_leads, "attention": attention, "dead_jobs": dead_jobs,
        "reauth": reauth, "runs": runs, "recent_posts": recent_posts, "unavailable": analytics.UNAVAILABLE_METRICS,
    })


# ----------------------------------------------------------------------------- listings
@router.get("/listings")
def listings(request: Request, q: str = "", status: str = "", marketing: str = "", user: User = Depends(require_viewer),
             db: Session = Depends(get_db)):
    stmt = select(Listing).order_by(Listing.mls_modified_at.desc().nulls_last(), Listing.id.desc())
    if q:
        like = f"%{q.strip()}%"
        stmt = stmt.where(or_(Listing.mls_id.ilike(like), Listing.street_address.ilike(like), Listing.city.ilike(like),
                              Listing.postal_code.ilike(like)))
    if status:
        stmt = stmt.where(Listing.status == status)
    rows = []
    for listing in db.scalars(stmt.limit(500)):
        auth = marketing_authorization(listing)
        if marketing == "authorized" and not auth.authorized:
            continue
        if marketing == "not_authorized" and auth.authorized:
            continue
        counts = dict(db.execute(select(Post.status, func.count()).where(Post.listing_id == listing.id).group_by(Post.status)).all())
        rows.append({"listing": listing, "auth": auth, "counts": counts})
    statuses = [s for (s,) in db.execute(select(Listing.status).distinct()).all()]
    last_run = db.scalar(select(SyncRun).order_by(SyncRun.id.desc()).limit(1))
    return render(request, "admin/listings.html", {"rows": rows, "q": q, "status": status, "marketing": marketing,
                                                   "statuses": sorted(statuses), "last_run": last_run})


@router.get("/listings/{listing_id}")
def listing_detail(listing_id: int, request: Request, user: User = Depends(require_viewer), db: Session = Depends(get_db)):
    listing = db.get(Listing, listing_id)
    if listing is None:
        raise HTTPException(404, "Listing not found.")
    posts = list(db.scalars(select(Post).where(Post.listing_id == listing.id).order_by(Post.id.desc())))
    accounts = list(db.scalars(select(SocialAccount).where(SocialAccount.enabled.is_(True),
                                                           SocialAccount.status.in_(("connected", "reauth_required")))))
    lead_count = db.scalar(select(func.count()).select_from(Lead).where(Lead.listing_id == listing.id))
    links = list(db.scalars(select(TrackingLink).where(TrackingLink.listing_id == listing.id)))
    return render(request, "admin/listing_detail.html", {
        "listing": listing, "auth": marketing_authorization(listing), "posts": posts, "accounts": accounts,
        "lead_count": lead_count, "links": links, "changes": list(reversed(listing.changes))[:50],
        "rules": get_rules(db),
    })


@router.post("/listings/{listing_id}/publish", dependencies=csrf)
async def listing_publish(listing_id: int, request: Request, user: User = Depends(require_marketer),
                          db: Session = Depends(get_db)):
    listing = db.get(Listing, listing_id)
    if listing is None:
        raise HTTPException(404, "Listing not found.")
    form = await request.form()
    account_ids = [int(a) for a in form.getlist("account_ids") if str(a).isdigit()]
    if not account_ids:
        flash(request, "Select at least one account.", "error")
        return _back(request, f"/admin/listings/{listing_id}")
    if not listing.is_available:
        flash(request, f"Listing status is {listing.status}; promotional posts are not allowed.", "error")
        return _back(request, f"/admin/listings/{listing_id}")
    auth = marketing_authorization(listing)
    if not auth.authorized:
        flash(request, f"Not authorized for marketing: {auth.reason}", "error")
        return _back(request, f"/admin/listings/{listing_id}")
    rules = get_rules(db)
    rules.enabled_platforms = sorted(ADAPTERS)  # explicit selection overrides the platform filter
    launched = db.scalar(select(Post.id).where(Post.listing_id == listing.id, Post.kind == "new_listing").limit(1))
    kind, key = ("repost", f"manual:{utcnow():%Y%m%d%H%M%S}") if launched else ("new_listing", "new")
    posts = plan_posts(db, listing, kind, key, rules, account_ids=account_ids)
    audit(db, user, "posts_planned", "listing", listing.id, kind=kind, accounts=account_ids)
    db.commit()
    flash(request, f"Created {len(posts)} post(s). Review them in the publishing queue.")
    return _back(request, f"/admin/listings/{listing_id}")


@router.post("/listings/{listing_id}/sync", dependencies=csrf)
def listing_sync(listing_id: int, request: Request, user: User = Depends(require_marketer), db: Session = Depends(get_db)):
    listing = db.get(Listing, listing_id)
    if listing is None:
        raise HTTPException(404, "Listing not found.")
    enqueue(db, "mls_sync", {"mls_id": listing.mls_id, "triggered_by": f"user:{user.id}"})
    audit(db, user, "sync_requested", "listing", listing.id)
    db.commit()
    flash(request, "Refresh from the MLS has been queued.")
    return _back(request, f"/admin/listings/{listing_id}")


@router.post("/sync", dependencies=csrf)
def full_sync(request: Request, mode: str = Form("incremental"), user: User = Depends(require_marketer),
              db: Session = Depends(get_db)):
    mode = mode if mode in ("incremental", "full") else "incremental"
    enqueue(db, "mls_sync", {"mode": mode, "triggered_by": f"user:{user.id}"})
    audit(db, user, "sync_requested", mode=mode)
    db.commit()
    flash(request, f"{mode.capitalize()} MLS synchronization queued.")
    return _back(request, "/admin/listings")


@router.post("/listings/{listing_id}/marketing", dependencies=csrf)
def listing_marketing(listing_id: int, request: Request, decision: str = Form(...), user: User = Depends(require_admin),
                      db: Session = Depends(get_db)):
    listing = db.get(Listing, listing_id)
    if listing is None:
        raise HTTPException(404, "Listing not found.")
    listing.marketing_override = {"allow": True, "block": False}.get(decision)
    audit(db, user, "marketing_override", "listing", listing.id, decision=decision)
    if listing.marketing_override is False:
        from ..services.publishing import cancel_open_posts

        cancel_open_posts(db, listing, "Marketing disabled by an administrator")
    db.commit()
    flash(request, "Marketing authorization updated.")
    return _back(request, f"/admin/listings/{listing_id}")


# ----------------------------------------------------------------------------- queue
@router.get("/queue")
def queue(request: Request, group: str = "awaiting", platform: str = "", user: User = Depends(require_viewer),
          db: Session = Depends(get_db)):
    statuses = POST_STATUS_GROUPS.get(group, POST_STATUS_GROUPS["awaiting"])
    stmt = select(Post).where(Post.status.in_(statuses)).order_by(Post.scheduled_for.asc().nulls_last(), Post.id.desc())
    if platform:
        stmt = stmt.where(Post.platform == platform)
    posts = list(db.scalars(stmt.limit(300)))
    counts = {g: db.scalar(select(func.count()).select_from(Post).where(Post.status.in_(s))) for g, s in POST_STATUS_GROUPS.items()}
    return render(request, "admin/queue.html", {"posts": posts, "group": group, "platform": platform, "counts": counts,
                                                "platforms": sorted(ADAPTERS)})


@router.get("/posts/{post_id}")
def post_detail(post_id: int, request: Request, user: User = Depends(require_viewer), db: Session = Depends(get_db)):
    post = db.get(Post, post_id)
    if post is None:
        raise HTTPException(404, "Post not found.")
    spec = get_adapter(post.platform).spec
    return render(request, "admin/post_detail.html", {"post": post, "spec": spec, "length": spec.measure(post.text)})


@router.post("/posts/{post_id}/{action}", dependencies=csrf)
async def post_action(post_id: int, action: str, request: Request, user: User = Depends(require_marketer),
                      db: Session = Depends(get_db)):
    post = db.get(Post, post_id)
    if post is None:
        raise HTTPException(404, "Post not found.")
    form = await request.form()
    try:
        if action == "approve":
            approve_post(db, post, user)
        elif action == "reject":
            reject_post(post, str(form.get("reason") or "Rejected by reviewer"))
        elif action == "retry":
            retry_post(db, post)
        elif action == "mark-published":
            url = str(form.get("url") or "").strip() or None
            if url and not url.startswith("https://"):
                raise ActionError("Post URL must start with https://")
            mark_manually_published(post, url, user)
        elif action == "edit":
            update_post_text(post, str(form.get("text") or ""))
        else:
            raise HTTPException(404, "Unknown action.")
        audit(db, user, f"post_{action}", "post", post.id)
        db.commit()
        flash(request, f"Post {action.replace('-', ' ')} succeeded.")
    except ActionError as exc:
        db.commit()  # persist any cancellation recorded while validating
        flash(request, str(exc), "error")
    return _back(request, f"/admin/posts/{post_id}")


# ----------------------------------------------------------------------------- jobs
@router.get("/jobs")
def jobs(request: Request, user: User = Depends(require_viewer), db: Session = Depends(get_db)):
    dead = list(db.scalars(select(Job).where(Job.status == "dead").order_by(Job.id.desc()).limit(100)))
    pending = list(db.scalars(select(Job).where(Job.status.in_(("queued", "running"))).order_by(Job.run_at).limit(100)))
    runs = list(db.scalars(select(SyncRun).order_by(SyncRun.id.desc()).limit(25)))
    return render(request, "admin/jobs.html", {"dead": dead, "pending": pending, "runs": runs})


@router.post("/jobs/{job_id}/retry", dependencies=csrf)
def job_retry(job_id: int, request: Request, user: User = Depends(require_marketer), db: Session = Depends(get_db)):
    job = db.get(Job, job_id)
    if job is None or job.status != "dead":
        raise HTTPException(404, "Job not found or not retryable.")
    job.status, job.attempts, job.run_at, job.finished_at = "queued", 0, utcnow(), None
    audit(db, user, "job_retry", "job", job.id, type=job.type)
    db.commit()
    flash(request, "Job re-queued.")
    return _back(request, "/admin/jobs")


# ----------------------------------------------------------------------------- social accounts
@router.get("/accounts")
def accounts(request: Request, user: User = Depends(require_viewer), db: Session = Depends(get_db)):
    rows = []
    for account in db.scalars(select(SocialAccount).order_by(SocialAccount.platform, SocialAccount.id)):
        rows.append({"account": account, "spec": get_adapter(account.platform).spec, "missing": missing_scopes(account)})
    catalog = [{"spec": spec, "oauth_ready": bool(get_adapter(spec.platform).oauth_config())} for spec in all_specs()]
    return render(request, "admin/accounts.html", {"rows": rows, "catalog": catalog,
                                                   "enabled_platforms": get_rules(db).enabled_platforms})


@router.post("/accounts/connect", dependencies=csrf)
def account_connect_manual(request: Request, platform: str = Form(...), external_account_id: str = Form(...),
                           display_name: str = Form(...), access_token: str = Form(""), webhook_url: str = Form(""),
                           user: User = Depends(require_admin), db: Session = Depends(get_db)):
    if platform not in ADAPTERS:
        raise HTTPException(400, "Unknown platform.")
    external_account_id, display_name = external_account_id.strip()[:200], display_name.strip()[:200]
    if not external_account_id or not display_name:
        flash(request, "Account ID and display name are required.", "error")
        return _back(request, "/admin/accounts")
    spec = get_adapter(platform).spec
    token = access_token.strip() or None
    if spec.automated_publishing and platform not in ("webhook",) and not token:
        flash(request, f"{spec.display_name} requires an access token (or use the OAuth connect button).", "error")
        return _back(request, "/admin/accounts")
    if platform == "manual":
        token = "manual"
    if platform == "webhook" and webhook_url and not webhook_url.startswith("https://"):
        flash(request, "Webhook URL must use https://", "error")
        return _back(request, "/admin/accounts")
    if platform == "webhook":
        token = token or None
    settings = {"url": webhook_url} if webhook_url else None
    account = connect_account(db, platform, external_account_id, display_name, TokenSet(token) if token else None, settings)
    if platform == "webhook" and not token:
        account.access_token_enc = None
    audit(db, user, "account_connected", "social_account", account.id, platform=platform, method="manual")
    db.commit()
    flash(request, f"Connected {spec.display_name}: {display_name}.")
    return _back(request, "/admin/accounts")


@router.post("/accounts/{account_id}/{action}", dependencies=csrf)
def account_action(account_id: int, action: str, request: Request, user: User = Depends(require_admin),
                   db: Session = Depends(get_db)):
    account = db.get(SocialAccount, account_id)
    if account is None:
        raise HTTPException(404, "Account not found.")
    if action == "check":
        status = check_account(db, account)
        flash(request, f"Status: {status}" + (f" — {account.last_error}" if account.last_error else ""))
    elif action == "disconnect":
        disconnect_account(db, account)
        flash(request, "Account disconnected and stored tokens deleted.")
    elif action == "toggle":
        if account.status == "disconnected":
            flash(request, "Reconnect the account first.", "error")
        else:
            account.enabled = not account.enabled
            flash(request, "Account " + ("enabled." if account.enabled else "paused."))
    else:
        raise HTTPException(404, "Unknown action.")
    audit(db, user, f"account_{action}", "social_account", account.id)
    db.commit()
    return _back(request, "/admin/accounts")


# ----------------------------------------------------------------------------- leads
def _lead_query(q: str, listing_id: str, platform: str, status: str):
    stmt = select(Lead).join(Contact).join(Listing).order_by(Lead.last_inquiry_at.desc())
    if q:
        like = f"%{q.strip()}%"
        stmt = stmt.where(or_(Contact.full_name.ilike(like), Contact.email.ilike(like), Contact.phone.ilike(like),
                              Listing.mls_id.ilike(like), Listing.street_address.ilike(like)))
    if listing_id.isdigit():
        stmt = stmt.where(Lead.listing_id == int(listing_id))
    if platform:
        stmt = stmt.where(Lead.source_platform == platform)
    if status:
        stmt = stmt.where(Lead.status == status)
    return stmt


@router.get("/leads")
def leads(request: Request, q: str = "", listing: str = "", platform: str = "", status: str = "",
          user: User = Depends(require_marketer), db: Session = Depends(get_db)):
    rows = list(db.scalars(_lead_query(q, listing, platform, status).limit(500)))
    platforms = [p for (p,) in db.execute(select(Lead.source_platform).distinct()).all()]
    listings_with_leads = list(db.scalars(select(Listing).where(Listing.id.in_(select(Lead.listing_id)))))
    return render(request, "admin/leads.html", {
        "leads": rows, "q": q, "listing": listing, "platform": platform, "status": status,
        "platforms": sorted(platforms), "statuses": LEAD_STATUSES, "listings": listings_with_leads,
        "crm_enabled": get_settings().crm_provider != "none",
    })


@router.get("/leads/export.csv")
def leads_export(request: Request, q: str = "", listing: str = "", platform: str = "", status: str = "",
                 user: User = Depends(require_marketer), db: Session = Depends(get_db)):
    rows = list(db.scalars(_lead_query(q, listing, platform, status)))
    audit(db, user, "leads_exported", count=len(rows), filters={"q": bool(q), "listing": listing, "platform": platform, "status": status})
    db.commit()

    def safe(value) -> str:
        text = "" if value is None else str(value)
        return "'" + text if text[:1] in ("=", "+", "-", "@", "\t", "\r") else text  # CSV formula injection

    buf = io.StringIO()
    writer = csv.writer(buf)
    writer.writerow(["lead_id", "created_at", "last_inquiry_at", "status", "name", "email", "phone", "preferred_contact",
                     "marketing_consent", "sms_consent", "mls_id", "address", "source_platform", "campaign", "inquiries"])
    for lead in rows:
        writer.writerow([safe(v) for v in (
            lead.id, lead.created_at.isoformat(), lead.last_inquiry_at.isoformat(), lead.status, lead.contact.full_name,
            lead.contact.email, lead.contact.phone, lead.preferred_contact_method, lead.marketing_consent, lead.sms_consent,
            lead.listing.mls_id, lead.listing.display_address, lead.source_platform,
            lead.campaign.code if lead.campaign else "", len(lead.inquiries),
        )])
    filename = f"leads-{utcnow():%Y%m%d-%H%M}.csv"
    return StreamingResponse(iter([buf.getvalue()]), media_type="text/csv",
                             headers={"Content-Disposition": f'attachment; filename="{filename}"', "Cache-Control": "no-store"})


@router.get("/leads/{lead_id}")
def lead_detail(lead_id: int, request: Request, user: User = Depends(require_marketer), db: Session = Depends(get_db)):
    lead = db.get(Lead, lead_id)
    if lead is None:
        raise HTTPException(404, "Lead not found.")
    audit(db, user, "lead_viewed", "lead", lead.id)
    db.commit()
    crm = list(db.scalars(select(CRMSync).where(CRMSync.lead_id == lead.id)))
    other = list(db.scalars(select(Lead).where(Lead.contact_id == lead.contact_id, Lead.id != lead.id)))
    return render(request, "admin/lead_detail.html", {"lead": lead, "statuses": LEAD_STATUSES, "crm": crm, "other_leads": other,
                                                      "crm_enabled": get_settings().crm_provider != "none"})


@router.post("/leads/{lead_id}/update", dependencies=csrf)
def lead_update(lead_id: int, request: Request, status: str = Form(...), notes: str = Form(""),
                user: User = Depends(require_marketer), db: Session = Depends(get_db)):
    lead = db.get(Lead, lead_id)
    if lead is None:
        raise HTTPException(404, "Lead not found.")
    if status not in LEAD_STATUSES:
        raise HTTPException(400, "Invalid status.")
    old = lead.status
    lead.status, lead.notes = status, notes[:5000] or None
    audit(db, user, "lead_updated", "lead", lead.id, old_status=old, new_status=status)
    db.commit()
    flash(request, "Lead updated.")
    return _back(request, f"/admin/leads/{lead_id}")


@router.post("/leads/{lead_id}/erase", dependencies=csrf)
def lead_erase(lead_id: int, request: Request, confirm: str = Form(""), user: User = Depends(require_admin),
               db: Session = Depends(get_db)):
    from ..services.leads import erase_contact

    lead = db.get(Lead, lead_id)
    if lead is None:
        raise HTTPException(404, "Lead not found.")
    if confirm != "ERASE":
        flash(request, 'Type ERASE to confirm deleting this buyer\'s data.', "error")
        return _back(request, f"/admin/leads/{lead_id}")
    removed = erase_contact(db, lead.contact)
    audit(db, user, "contact_erased", "lead", lead_id, leads_removed=removed)  # no PII in the audit trail
    db.commit()
    flash(request, f"Buyer data erased ({removed} lead record(s)).")
    return RedirectResponse("/admin/leads", status_code=303)


@router.post("/leads/{lead_id}/crm", dependencies=csrf)
def lead_crm(lead_id: int, request: Request, user: User = Depends(require_marketer), db: Session = Depends(get_db)):
    lead = db.get(Lead, lead_id)
    if lead is None or not lead.inquiries:
        raise HTTPException(404, "Lead not found.")
    if get_settings().crm_provider == "none":
        flash(request, "No CRM is configured (CRM_PROVIDER).", "error")
        return _back(request, f"/admin/leads/{lead_id}")
    inquiry = lead.inquiries[-1]
    existing = db.scalar(select(CRMSync).where(CRMSync.inquiry_id == inquiry.id))
    if existing and existing.status != "sent":
        existing.status = "pending"
    enqueue(db, "crm_push", {"inquiry_id": inquiry.id})
    audit(db, user, "lead_crm_push", "lead", lead.id)
    db.commit()
    flash(request, "Lead queued for CRM delivery.")
    return _back(request, f"/admin/leads/{lead_id}")


# ----------------------------------------------------------------------------- analytics
@router.get("/analytics")
def analytics_view(request: Request, days: int = 90, user: User = Depends(require_viewer), db: Session = Depends(get_db)):
    days = max(1, min(days, 3650))
    since = utcnow() - timedelta(days=days)
    return render(request, "admin/analytics.html", {
        "days": days, "platforms": analytics.by_platform(db, since), "listings": analytics.by_listing(db),
        "stats": analytics.overview(db, since), "strongest": analytics.strongest_sources(db, days),
        "unavailable": analytics.UNAVAILABLE_METRICS,
    })


# ----------------------------------------------------------------------------- settings & users
@router.get("/settings")
def settings_view(request: Request, user: User = Depends(require_admin), db: Session = Depends(get_db)):
    s = get_settings()
    return render(request, "admin/settings.html", {
        "rules": get_rules(db), "modes": PUBLISH_MODES, "platforms": sorted(ADAPTERS), "config": {
            "MLS provider": s.mls_provider, "Marketing scope": s.marketing_scope, "Brokerage office IDs": ", ".join(s.office_ids) or "—",
            "Brokerage name": s.brokerage_name or "—", "CRM provider": s.crm_provider, "Email backend": s.email_backend,
            "Lead notification recipients": len(s.notification_recipients), "Environment": s.app_env,
        },
    })


@router.post("/settings", dependencies=csrf)
async def settings_save(request: Request, user: User = Depends(require_admin), db: Session = Depends(get_db)):
    form = await request.form()
    rules = get_rules(db)

    def as_int(name: str, default: int) -> int:
        try:
            return int(str(form.get(name, default)))
        except ValueError:
            return -1

    rules.new_listing_mode = str(form.get("new_listing_mode", rules.new_listing_mode))
    rules.price_reduction_mode = str(form.get("price_reduction_mode", rules.price_reduction_mode))
    rules.open_house_mode = str(form.get("open_house_mode", rules.open_house_mode))
    rules.delay_minutes = as_int("delay_minutes", rules.delay_minutes)
    rules.open_house_lead_hours = as_int("open_house_lead_hours", rules.open_house_lead_hours)
    rules.repost_interval_days = as_int("repost_interval_days", rules.repost_interval_days)
    for flag in ("announce_price_reductions", "announce_open_houses", "announce_status_changes", "delete_posts_on_unavailable"):
        setattr(rules, flag, form.get(flag) == "on")
    rules.enabled_platforms = [p for p in form.getlist("enabled_platforms") if p in ADAPTERS]
    try:
        save_rules(db, rules)
    except ValueError as exc:
        flash(request, str(exc), "error")
        return _back(request, "/admin/settings")
    audit(db, user, "rules_updated")
    db.commit()
    flash(request, "Publishing rules saved.")
    return _back(request, "/admin/settings")


@router.get("/users")
def users(request: Request, user: User = Depends(require_admin), db: Session = Depends(get_db)):
    return render(request, "admin/users.html", {"users": list(db.scalars(select(User).order_by(User.id))), "roles": ROLES})


@router.post("/users", dependencies=csrf)
def user_create(request: Request, email: str = Form(...), password: str = Form(...), role: str = Form(...),
                user: User = Depends(require_admin), db: Session = Depends(get_db)):
    email = email.strip().lower()
    problems = password_problems(password)
    if role not in ROLES:
        problems.append("Invalid role.")
    if "@" not in email:
        problems.append("Invalid email.")
    if db.scalar(select(User).where(User.email == email)):
        problems.append("A user with that email exists.")
    if problems:
        flash(request, " ".join(problems), "error")
        return _back(request, "/admin/users")
    new = User(email=email, password_hash=hash_password(password), role=role)
    db.add(new)
    db.flush()
    audit(db, user, "user_created", "user", new.id, role=role)
    db.commit()
    flash(request, f"User {email} created.")
    return _back(request, "/admin/users")


@router.post("/users/{user_id}/update", dependencies=csrf)
def user_update(user_id: int, request: Request, role: str = Form(...), active: str = Form("off"),
                user: User = Depends(require_admin), db: Session = Depends(get_db)):
    target = db.get(User, user_id)
    if target is None or role not in ROLES:
        raise HTTPException(404, "User not found.")
    if target.id == user.id and (role != "admin" or active != "on"):
        flash(request, "You cannot remove your own admin access.", "error")
        return _back(request, "/admin/users")
    target.role, target.is_active = role, active == "on"
    audit(db, user, "user_updated", "user", target.id, role=role, active=target.is_active)
    db.commit()
    flash(request, "User updated.")
    return _back(request, "/admin/users")
