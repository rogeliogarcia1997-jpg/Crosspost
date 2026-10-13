"""Background job handlers."""
from __future__ import annotations

from typing import Any

from sqlalchemy import select
from sqlalchemy.orm import Session

from ..crm.clients import CRMError, get_crm_client
from ..db import utcnow
from ..logging_setup import get_logger, log_event
from ..mls.registry import get_provider
from ..models import CRMSync, Inquiry, Job, SocialAccount
from ..security import purge_rate_limit_hits
from ..services import publishing
from ..services.accounts import check_account
from ..services.notifications import EmailSendError, notify_admins, notify_new_lead
from ..services.rules import get_rules
from ..services.sync import SyncError, recent_auth_failure, run_sync
from ..services.workflow import repost_scan
from ..social.base import PublishError
from .queue import PermanentJobError, RetryLater, handler

log = get_logger(__name__)


@handler("mls_sync")
def mls_sync(db: Session, payload: dict[str, Any]) -> None:
    provider = get_provider()
    if payload.get("triggered_by") == "schedule" and recent_auth_failure(db, provider.name):
        log_event(log, "mls_sync_skipped_auth_failure")
        return  # don't hammer the MLS with bad credentials; a manual sync retries immediately
    try:
        run_sync(db, provider, mode=payload.get("mode", "incremental"), mls_id=payload.get("mls_id"),
                 triggered_by=payload.get("triggered_by"))
    except SyncError as exc:
        if not exc.retryable:
            notify_admins(db, "MLS credentials need attention", str(exc))
            db.commit()
            raise PermanentJobError(str(exc)) from exc
        raise RetryLater(str(exc), retry_after=exc.retry_after) from exc


@handler("publish_post")
def publish_post(db: Session, payload: dict[str, Any]) -> None:
    outcome = publishing.execute_publish(db, int(payload["post_id"]))
    if outcome.status == "retry_scheduled":
        # The post's state is already committed; ask the queue to run this job again later.
        raise RetryLater(outcome.message, retry_after=outcome.retry_after)
    if outcome.status == "blocked_auth":
        notify_admins(db, "Social account needs re-authorization",
                      "A post could not be published because the account's authorization expired. "
                      "Reconnect it from Social Accounts; blocked posts will resume automatically.")
        db.commit()


@handler("delete_post")
def delete_post(db: Session, payload: dict[str, Any]) -> None:
    try:
        publishing.execute_delete(db, int(payload["post_id"]))
    except PublishError as exc:
        raise PermanentJobError(str(exc)) from exc


@handler("scan_scheduled_posts")
def scan_scheduled_posts(db: Session, payload: dict[str, Any]) -> None:
    publishing.release_due_scheduled_posts(db)


@handler("repost_scan")
def repost(db: Session, payload: dict[str, Any]) -> None:
    repost_scan(db, get_rules(db))


@handler("check_social_accounts")
def check_social_accounts(db: Session, payload: dict[str, Any]) -> None:
    for account in db.scalars(select(SocialAccount).where(SocialAccount.status.in_(("connected", "reauth_required")))):
        before = account.status
        check_account(db, account)
        if before != "reauth_required" and account.status == "reauth_required":
            notify_admins(db, f"{account.platform} account needs re-authorization",
                          f"The account '{account.display_name}' must be reconnected: {account.last_error}")


@handler("notify_lead")
def notify_lead(db: Session, payload: dict[str, Any]) -> None:
    inquiry = db.get(Inquiry, int(payload["inquiry_id"]))
    if inquiry is None:
        return
    try:
        notify_new_lead(db, inquiry.lead, inquiry)
    except EmailSendError as exc:
        raise RetryLater(str(exc)) from exc


@handler("crm_push")
def crm_push(db: Session, payload: dict[str, Any]) -> None:
    client = get_crm_client()
    inquiry = db.get(Inquiry, int(payload["inquiry_id"]))
    if client is None or inquiry is None:
        return
    sync = db.scalar(select(CRMSync).where(CRMSync.inquiry_id == inquiry.id, CRMSync.provider == client.name))
    if sync and sync.status == "sent":
        return
    if sync is None:
        sync = CRMSync(lead_id=inquiry.lead_id, inquiry_id=inquiry.id, provider=client.name)
        db.add(sync)
    try:
        sync.external_id = client.push_inquiry(inquiry.lead, inquiry)
        sync.status, sync.synced_at, sync.last_error = "sent", utcnow(), None
    except CRMError as exc:
        sync.status, sync.last_error = "failed", str(exc)
        db.commit()
        if exc.retryable:
            raise RetryLater(str(exc)) from exc
        raise PermanentJobError(str(exc)) from exc


@handler("cleanup")
def cleanup(db: Session, payload: dict[str, Any]) -> None:
    purge_rate_limit_hits(db)
    from datetime import timedelta

    from sqlalchemy import delete

    db.execute(delete(Job).where(Job.status == "succeeded", Job.finished_at < utcnow() - timedelta(days=14)))
