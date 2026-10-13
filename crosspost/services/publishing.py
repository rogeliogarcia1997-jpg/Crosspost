"""Publishing engine: post planning, approval, publishing, retries and cancellation.

Post lifecycle::

    pending_approval ──approve──▶ scheduled ──due──▶ queued ──▶ publishing ──▶ published
          │                                            ▲            │
          └─reject─▶ rejected                          │            ├─rate limit / transient─▶ retry_scheduled ─┐
                                                       └────────────┼───────────────────────────────────────────┘
    manual_ready ──(person posts, records URL)──▶ published         ├─auth expired─▶ blocked_auth (until reconnected)
    skipped  (platform requirements not met)                        ├─timeout after send─▶ needs_verification
    canceled (listing no longer available / account disconnected)   └─permanent / max attempts─▶ failed

Duplicate prevention:
* ``posts`` has a unique (campaign, account, kind, event_key) constraint, so
  re-syncing the same listing never plans the same post twice.
* A post moves to ``publishing`` through a conditional UPDATE; only one worker
  can claim it, and a published post is never sent again.
* Ambiguous outcomes (timeouts after the request was sent) are not retried
  automatically; they wait for a person to verify.
"""
from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timedelta
from typing import Any

from sqlalchemy import select, update
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session

from ..config import get_settings
from ..db import utcnow
from ..jobs.queue import enqueue
from ..logging_setup import get_logger, log_event, scrub_text
from ..models import Listing, Post, PublishAttempt, SocialAccount, User
from ..social.base import (
    AuthExpiredError,
    NotConfiguredError,
    PermanentPublishError,
    PublishError,
    RateLimitError,
    TransientPublishError,
)
from ..social.registry import get_adapter
from .accounts import account_context, mark_reauth_required
from .campaigns import create_tracking_link, get_or_create_campaign, tracking_url
from .compliance import marketing_authorization
from .content import PROMOTIONAL_KINDS, ContentError, render_post
from .rules import PublishingRules

log = get_logger(__name__)

OPEN_STATUSES = {"pending_approval", "scheduled", "queued", "retry_scheduled", "blocked_auth", "manual_ready", "needs_verification"}
RETRYABLE_BY_USER = {"failed", "needs_verification", "blocked_auth"}


# --------------------------------------------------------------------------- planning
def eligible_accounts(db: Session, rules: PublishingRules) -> list[SocialAccount]:
    return list(db.scalars(
        select(SocialAccount).where(
            SocialAccount.enabled.is_(True),
            SocialAccount.status.in_(("connected", "reauth_required")),
            SocialAccount.platform.in_(rules.enabled_platforms),
        ).order_by(SocialAccount.id)
    ))


def render_into(db: Session, post: Post, listing: Listing) -> list[str]:
    """(Re)generate post content. Returns validation errors (empty when publishable)."""
    adapter = get_adapter(post.platform)
    link = create_tracking_link(db, get_or_create_campaign(db, listing), listing, post)
    rendered = render_post(listing, adapter.spec, post.kind, tracking_url(link), post.context or {})
    post.text = rendered.text
    post.title = rendered.title
    post.link_url = rendered.link
    post.media = [{"url": m.url, "alt": m.alt} for m in rendered.media]
    post.hashtags = rendered.hashtags
    post.warnings = rendered.warnings
    post.listing_version = listing.content_hash
    if rendered.requires_review:
        post.requires_approval = True
    return adapter.validate(rendered)


def _initial_status(post: Post, account: SocialAccount, mode: str, scheduled_for: datetime | None,
                    now: datetime, rules: PublishingRules) -> str:
    adapter = get_adapter(post.platform)
    if post.requires_approval or mode == "manual_approval":
        post.requires_approval = True
        post.scheduled_for = scheduled_for
        return "pending_approval"
    if not adapter.spec.automated_publishing:
        return "manual_ready"
    if mode == "scheduled" and scheduled_for is None:
        scheduled_for = now + timedelta(minutes=rules.delay_minutes)
    if scheduled_for and scheduled_for > now:
        post.scheduled_for = scheduled_for
        return "scheduled"
    if account.status == "reauth_required":
        return "blocked_auth"
    return "queued"


def plan_posts(
    db: Session,
    listing: Listing,
    kind: str,
    event_key: str,
    rules: PublishingRules,
    *,
    context: dict[str, Any] | None = None,
    scheduled_for: datetime | None = None,
    now: datetime | None = None,
    account_ids: list[int] | None = None,
) -> list[Post]:
    """Create one post per eligible account for a listing event (idempotent)."""
    now = now or utcnow()
    campaign = get_or_create_campaign(db, listing)
    created: list[Post] = []
    for account in eligible_accounts(db, rules):
        if account_ids is not None and account.id not in account_ids:
            continue
        exists = db.scalar(select(Post.id).where(
            Post.campaign_id == campaign.id, Post.social_account_id == account.id,
            Post.kind == kind, Post.event_key == event_key))
        if exists:
            continue
        post = Post(
            campaign_id=campaign.id, listing_id=listing.id, social_account_id=account.id,
            platform=account.platform, kind=kind, event_key=event_key, status="draft",
            context=context or {}, requires_approval=False,
        )
        try:
            with db.begin_nested():
                db.add(post)
                db.flush()
        except IntegrityError:
            continue  # planned concurrently by another worker
        try:
            errors = render_into(db, post, listing)
        except ContentError as exc:
            post.status, post.error_category, post.last_error = "failed", "content", str(exc)
            created.append(post)
            continue
        if errors:
            post.status, post.error_category = "skipped", "requirements"
            post.last_error = "; ".join(errors)
            created.append(post)
            continue
        post.status = _initial_status(post, account, rules.mode_for(kind), scheduled_for, now, rules)
        if post.status == "queued":
            _enqueue_publish(db, post)
        created.append(post)
        log_event(log, "post_planned", post_id=post.id, platform=post.platform, kind=kind, status=post.status)
    return created


def _enqueue_publish(db: Session, post: Post, run_at: datetime | None = None) -> None:
    post.status = "queued"
    db.flush()
    enqueue(db, "publish_post", {"post_id": post.id}, run_at=run_at)


# --------------------------------------------------------------------------- publishing
@dataclass
class PublishOutcome:
    status: str
    retry_after: float | None = None
    message: str = ""


def _still_promotable(db: Session, post: Post, now: datetime) -> str | None:
    """Return a cancellation reason if the post must not be published."""
    listing = post.listing
    if post.kind in PROMOTIONAL_KINDS and not listing.is_available:
        return f"Listing status is {listing.status}; promotional post canceled"
    if post.kind in PROMOTIONAL_KINDS and post.campaign.status != "active":
        return "Campaign has ended"
    auth = marketing_authorization(listing)
    if not auth.authorized:
        return f"Marketing no longer authorized: {auth.reason}"
    if post.kind == "open_house":
        oh = (post.context or {}).get("open_house") or {}
        end = oh.get("end")
        if end and datetime.fromisoformat(end) <= now:
            return "Open house has already ended"
        keys = {o.get("key") for o in listing.open_houses or []}
        if oh.get("key") and oh.get("key") not in keys:
            return "Open house is no longer listed in the MLS"
    return None


def execute_publish(db: Session, post_id: int, now: datetime | None = None) -> PublishOutcome:
    now = now or utcnow()
    settings = get_settings()
    post = db.get(Post, post_id)
    if post is None:
        return PublishOutcome("missing")
    if post.status not in ("queued", "retry_scheduled"):
        return PublishOutcome(post.status, message="not in a publishable state; nothing to do")

    reason = _still_promotable(db, post, now)
    if reason:
        post.status, post.last_error = "canceled", reason
        db.commit()
        return PublishOutcome("canceled", message=reason)

    account = post.social_account
    if not account.enabled or account.status == "disconnected":
        post.status, post.last_error = "canceled", "Account disconnected"
        db.commit()
        return PublishOutcome("canceled")
    if account.status == "reauth_required":
        post.status, post.last_error = "blocked_auth", "Account requires re-authorization"
        db.commit()
        return PublishOutcome("blocked_auth")

    if post.listing_version != post.listing.content_hash:
        try:
            errors = render_into(db, post, post.listing)
        except ContentError as exc:
            post.status, post.error_category, post.last_error = "failed", "content", str(exc)
            db.commit()
            return PublishOutcome("failed", message=str(exc))
        if errors:
            post.status, post.error_category, post.last_error = "skipped", "requirements", "; ".join(errors)
            db.commit()
            return PublishOutcome("skipped")
        if post.requires_approval:
            post.status = "pending_approval"
            post.approved_at = post.approved_by_id = None
            post.warnings = [*post.warnings, "Listing data changed after approval; content regenerated and needs re-approval"]
            db.commit()
            return PublishOutcome("pending_approval")

    # Claim the post atomically; a concurrent worker that loses this race does nothing.
    claimed = db.execute(
        update(Post).where(Post.id == post.id, Post.status.in_(("queued", "retry_scheduled")))
        .values(status="publishing", attempts=Post.attempts + 1)
    ).rowcount
    if claimed != 1:
        db.rollback()
        return PublishOutcome("skipped", message="claimed by another worker")
    db.refresh(post)
    attempt = PublishAttempt(post_id=post.id, attempt_no=post.attempts, started_at=now)
    db.add(attempt)
    db.commit()  # persist "publishing" before calling the platform

    adapter = get_adapter(post.platform)
    from ..social.base import MediaItem, RenderedPost

    rendered = RenderedPost(
        platform=post.platform, kind=post.kind, text=post.text, title=post.title, link=post.link_url,
        media=[MediaItem(m["url"], m.get("alt", "")) for m in post.media or []], hashtags=post.hashtags or [],
    )
    max_attempts = settings.job_max_attempts
    outcome: PublishOutcome
    try:
        result = adapter.publish(account_context(account), rendered, idempotency_key=f"crosspost-post-{post.id}")
        post.status, post.external_post_id, post.external_post_url = "published", result.external_id, result.url
        post.published_at, post.last_error, post.error_category = utcnow(), None, None
        attempt.outcome = "success"
        outcome = PublishOutcome("published")
    except RateLimitError as exc:
        attempt.outcome, attempt.http_status = "rate_limited", exc.http_status
        outcome = _retry_or_fail(post, exc, "rate_limit", max_attempts, exc.retry_after)
    except AuthExpiredError as exc:
        attempt.outcome, attempt.http_status = "auth_expired", exc.http_status
        mark_reauth_required(account, str(exc))
        post.status, post.error_category, post.last_error = "blocked_auth", "auth", scrub_text(str(exc))
        outcome = PublishOutcome("blocked_auth", message=str(exc))
    except NotConfiguredError as exc:
        attempt.outcome = "not_configured"
        post.status, post.error_category, post.last_error = "failed", "configuration", str(exc)
        outcome = PublishOutcome("failed", message=str(exc))
    except TransientPublishError as exc:
        attempt.http_status = exc.http_status
        if exc.may_have_published:
            attempt.outcome = "uncertain"
            post.status, post.error_category = "needs_verification", "uncertain"
            post.last_error = f"{exc}. The platform may have accepted the post; verify before retrying."
            outcome = PublishOutcome("needs_verification")
        else:
            attempt.outcome = "transient_error"
            outcome = _retry_or_fail(post, exc, "transient", max_attempts, None)
    except PermanentPublishError as exc:
        attempt.outcome, attempt.http_status = "permanent_error", exc.http_status
        post.status, post.error_category, post.last_error = "failed", "permanent", scrub_text(str(exc))
        outcome = PublishOutcome("failed", message=str(exc))
    except PublishError as exc:  # pragma: no cover - defensive
        attempt.outcome = "error"
        outcome = _retry_or_fail(post, exc, "transient", max_attempts, None)
    attempt.finished_at = utcnow()
    attempt.message = scrub_text(outcome.message or post.last_error or "")[:2000] or None
    db.commit()
    log_event(log, "post_publish_attempt", post_id=post.id, platform=post.platform, outcome=outcome.status,
              attempt=post.attempts)
    return outcome


def _retry_or_fail(post: Post, exc: Exception, category: str, max_attempts: int, retry_after: float | None) -> PublishOutcome:
    post.error_category, post.last_error = category, scrub_text(str(exc))
    if post.attempts >= max_attempts:
        post.status = "failed"
        return PublishOutcome("failed", message=f"gave up after {post.attempts} attempts: {exc}")
    post.status = "retry_scheduled"
    return PublishOutcome("retry_scheduled", retry_after=retry_after, message=str(exc))


# --------------------------------------------------------------------------- user actions
class ActionError(Exception):
    pass


def approve_post(db: Session, post: Post, user: User, now: datetime | None = None) -> None:
    now = now or utcnow()
    if post.status != "pending_approval":
        raise ActionError(f"Only posts awaiting approval can be approved (status: {post.status})")
    reason = _still_promotable(db, post, now)
    if reason:
        post.status, post.last_error = "canceled", reason
        raise ActionError(reason)
    post.approved_by_id, post.approved_at = user.id, now
    if not get_adapter(post.platform).spec.automated_publishing:
        post.status = "manual_ready"
    elif post.scheduled_for and post.scheduled_for > now:
        post.status = "scheduled"
    elif post.social_account.status == "reauth_required":
        post.status = "blocked_auth"
    else:
        _enqueue_publish(db, post)


def reject_post(post: Post, reason: str) -> None:
    if post.status not in ("pending_approval", "scheduled", "manual_ready"):
        raise ActionError(f"Post cannot be rejected in status {post.status}")
    post.status, post.rejected_reason = "rejected", reason[:500] or "Rejected"


def retry_post(db: Session, post: Post) -> None:
    if post.status not in RETRYABLE_BY_USER:
        raise ActionError(f"Post is not eligible for retry (status: {post.status})")
    if post.social_account.status != "connected":
        raise ActionError("Reconnect the social account before retrying")
    if post.error_category == "content":
        raise ActionError("Content could not be generated for this platform; fix the listing data first")
    post.last_error = post.error_category = None
    if post.attempts >= get_settings().job_max_attempts:
        post.attempts = 0
    _enqueue_publish(db, post)


def mark_manually_published(post: Post, url: str | None, user: User) -> None:
    if post.status not in ("manual_ready", "needs_verification"):
        raise ActionError(f"Post cannot be marked published in status {post.status}")
    post.status, post.external_post_url, post.published_at = "published", url, utcnow()
    post.approved_by_id = post.approved_by_id or user.id


def update_post_text(post: Post, text: str) -> None:
    if post.status not in ("pending_approval", "manual_ready"):
        raise ActionError("Only posts awaiting approval can be edited")
    spec = get_adapter(post.platform).spec
    if spec.measure(text) > spec.max_text:
        raise ActionError(f"Text exceeds {spec.max_text} characters for {spec.display_name}")
    if post.link_url and post.link_url not in text and spec.style != "pin":
        raise ActionError("The tracked listing link must remain in the post")
    post.text = text


# --------------------------------------------------------------------------- bulk transitions
def cancel_open_posts(db: Session, listing: Listing, reason: str, kinds: set[str] | None = None) -> int:
    kinds = kinds or PROMOTIONAL_KINDS
    posts = db.scalars(select(Post).where(
        Post.listing_id == listing.id, Post.status.in_(OPEN_STATUSES - {"needs_verification"}), Post.kind.in_(kinds)))
    count = 0
    for post in posts:
        post.status, post.last_error = "canceled", reason
        count += 1
    return count


def retract_published_posts(db: Session, listing: Listing, rules: PublishingRules) -> int:
    """Queue deletion of published promotional posts when policy allows and the platform supports it."""
    if not rules.delete_posts_on_unavailable:
        return 0
    count = 0
    for post in db.scalars(select(Post).where(Post.listing_id == listing.id, Post.status == "published",
                                              Post.kind.in_(PROMOTIONAL_KINDS))):
        if get_adapter(post.platform).spec.supports_delete and post.external_post_id:
            enqueue(db, "delete_post", {"post_id": post.id}, idempotency_key=f"delete_post:{post.id}")
            count += 1
    return count


def execute_delete(db: Session, post_id: int) -> str:
    post = db.get(Post, post_id)
    if post is None or post.status != "published" or not post.external_post_id:
        return "noop"
    adapter = get_adapter(post.platform)
    adapter.delete(account_context(post.social_account), post.external_post_id)
    post.status = "retracted"
    return "retracted"


def hold_posts_for_account(db: Session, account: SocialAccount, status: str, reason: str) -> int:
    count = 0
    for post in db.scalars(select(Post).where(Post.social_account_id == account.id,
                                              Post.status.in_(("queued", "retry_scheduled", "scheduled", "blocked_auth", "pending_approval")))):
        post.status, post.last_error = status, reason
        count += 1
    return count


def release_blocked_posts(db: Session, account: SocialAccount, now: datetime | None = None) -> int:
    now = now or utcnow()
    count = 0
    for post in db.scalars(select(Post).where(Post.social_account_id == account.id, Post.status == "blocked_auth")):
        post.last_error = post.error_category = None
        if post.scheduled_for and post.scheduled_for > now:
            post.status = "scheduled"
        else:
            _enqueue_publish(db, post)
        count += 1
    return count


def release_due_scheduled_posts(db: Session, now: datetime | None = None) -> int:
    now = now or utcnow()
    count = 0
    for post in db.scalars(select(Post).where(Post.status == "scheduled", Post.scheduled_for <= now)):
        if post.social_account.status == "reauth_required":
            post.status = "blocked_auth"
        else:
            _enqueue_publish(db, post)
        count += 1
    return count


def flag_stuck_publishing(db: Session, now: datetime | None = None) -> int:
    """Posts left in ``publishing`` by a worker that died mid-request may or may not have
    reached the platform. Never resend them automatically; hold them for verification."""
    now = now or utcnow()
    cutoff = now - timedelta(seconds=get_settings().job_lock_timeout_seconds)
    count = 0
    for post in db.scalars(select(Post).where(Post.status == "publishing", Post.updated_at < cutoff)):
        post.status, post.error_category = "needs_verification", "uncertain"
        post.last_error = ("The worker stopped while publishing. Check the platform before retrying "
                           "to avoid a duplicate post.")
        count += 1
    return count
