"""Social account connections: token storage, health checks, re-authorization."""
from __future__ import annotations

from datetime import datetime, timedelta
from typing import Any

from sqlalchemy import select
from sqlalchemy.orm import Session

from ..db import utcnow
from ..logging_setup import get_logger, log_event
from ..models import SocialAccount
from ..security import decrypt_secret, encrypt_secret
from ..social.base import AccountContext, AuthExpiredError, PublishError, TokenSet
from ..social.registry import get_adapter

log = get_logger(__name__)

ACTIVE_STATUSES = {"connected"}


def account_context(account: SocialAccount) -> AccountContext:
    return AccountContext(
        platform=account.platform,
        external_account_id=account.external_account_id,
        access_token=decrypt_secret(account.access_token_enc),
        refresh_token=decrypt_secret(account.refresh_token_enc),
        settings=dict(account.settings or {}),
    )


def connect_account(
    db: Session,
    platform: str,
    external_account_id: str,
    display_name: str,
    tokens: TokenSet | None,
    settings: dict[str, Any] | None = None,
) -> SocialAccount:
    get_adapter(platform)  # validates platform
    account = db.scalar(select(SocialAccount).where(
        SocialAccount.platform == platform, SocialAccount.external_account_id == external_account_id))
    if account is None:
        account = SocialAccount(platform=platform, external_account_id=external_account_id, display_name=display_name)
        db.add(account)
    account.display_name = display_name
    account.status = "connected"
    account.enabled = True
    account.last_error = None
    if tokens:
        account.access_token_enc = encrypt_secret(tokens.access_token)
        account.refresh_token_enc = encrypt_secret(tokens.refresh_token)
        account.token_expires_at = tokens.expires_at
        account.granted_scopes = " ".join(tokens.scopes) if tokens.scopes else account.granted_scopes
    if settings is not None:
        account.settings = {**(account.settings or {}), **settings}
    db.flush()
    # Posts that were blocked waiting for re-authorization can resume.
    from .publishing import release_blocked_posts

    release_blocked_posts(db, account)
    log_event(log, "social_account_connected", platform=platform, account_id=account.id)
    return account


def disconnect_account(db: Session, account: SocialAccount) -> None:
    from .publishing import hold_posts_for_account

    account.status = "disconnected"
    account.enabled = False
    account.access_token_enc = None
    account.refresh_token_enc = None
    account.token_expires_at = None
    hold_posts_for_account(db, account, "canceled", "Account disconnected")
    log_event(log, "social_account_disconnected", platform=account.platform, account_id=account.id)


def mark_reauth_required(account: SocialAccount, reason: str) -> None:
    account.status = "reauth_required"
    account.last_error = reason[:1000]
    log_event(log, "social_account_reauth_required", platform=account.platform, account_id=account.id)


def missing_scopes(account: SocialAccount) -> list[str]:
    spec = get_adapter(account.platform).spec
    if not spec.required_scopes or account.granted_scopes is None:
        return []
    granted = set(account.granted_scopes.split())
    return [s for s in spec.required_scopes if s not in granted]


def check_account(db: Session, account: SocialAccount, now: datetime | None = None) -> str:
    """Validate credentials; refresh if possible; mark re-authorization when needed."""
    now = now or utcnow()
    account.last_checked_at = now
    if account.status == "disconnected":
        return account.status
    adapter = get_adapter(account.platform)
    ctx = account_context(account)
    expiring = account.token_expires_at is not None and account.token_expires_at <= now + timedelta(hours=1)
    if expiring:
        try:
            tokens = adapter.refresh(ctx)
        except PublishError as exc:
            tokens = None
            log_event(log, "token_refresh_failed", platform=account.platform, account_id=account.id, error=str(exc))
        if tokens:
            account.access_token_enc = encrypt_secret(tokens.access_token)
            if tokens.refresh_token:
                account.refresh_token_enc = encrypt_secret(tokens.refresh_token)
            account.token_expires_at = tokens.expires_at
            ctx = account_context(account)
        elif account.token_expires_at <= now:
            mark_reauth_required(account, "Access token expired")
            return account.status
    try:
        status = adapter.check_credentials(ctx)
    except AuthExpiredError as exc:
        mark_reauth_required(account, str(exc))
        return account.status
    except PublishError as exc:
        account.last_error = f"Health check failed: {exc}"[:1000]
        return account.status
    if not status.ok:
        mark_reauth_required(account, status.message or "Credential check failed")
    elif account.status == "reauth_required":
        account.status = "connected"
        account.last_error = None
        from .publishing import release_blocked_posts

        release_blocked_posts(db, account)
    return account.status
