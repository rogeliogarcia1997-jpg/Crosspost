"""Sandbox adapter for development and automated tests.

Publishes nowhere. Every "published" post is appended to ``PUBLISHED`` and
failures can be injected by pushing failure names onto ``FAILURES`` (consumed
first-in, first-out) or by setting ``fail_mode`` in the account settings.
"""
from __future__ import annotations

import itertools

from .base import (
    AccountContext,
    AuthExpiredError,
    CredentialStatus,
    PermanentPublishError,
    PlatformSpec,
    PublishResult,
    RateLimitError,
    RenderedPost,
    SocialAdapter,
    TransientPublishError,
)

PUBLISHED: list[dict] = []
FAILURES: list[str] = []
_ids = itertools.count(1)


def reset() -> None:
    PUBLISHED.clear()
    FAILURES.clear()


class TestAdapter(SocialAdapter):
    __test__ = False  # not a pytest test class
    spec = PlatformSpec(
        platform="test",
        display_name="Test Sandbox",
        style="long",
        max_text=2000,
        max_photos=4,
        max_hashtags=3,
        supports_delete=True,
        oauth=False,
        account_requirements="None. Posts are recorded locally and never leave the application.",
        limitations="Development/testing only.",
        account_id_label="Sandbox name",
    )

    def _raise_injected(self, mode: str | None) -> None:
        if not mode:
            return
        if mode == "rate_limit":
            raise RateLimitError("sandbox: rate limited", retry_after=30, http_status=429)
        if mode == "auth_expired":
            raise AuthExpiredError("sandbox: token expired", http_status=401)
        if mode == "transient":
            raise TransientPublishError("sandbox: upstream 503", http_status=503)
        if mode == "timeout_uncertain":
            raise TransientPublishError("sandbox: timed out after sending", may_have_published=True)
        if mode == "permanent":
            raise PermanentPublishError("sandbox: content rejected", http_status=400)

    def publish(self, account: AccountContext, post: RenderedPost, idempotency_key: str) -> PublishResult:
        if not account.access_token:
            raise AuthExpiredError("sandbox: no token")
        self._raise_injected(FAILURES.pop(0) if FAILURES else account.settings.get("fail_mode"))
        errors = self.validate(post)
        if errors:
            raise PermanentPublishError("; ".join(errors))
        # The sandbox honours idempotency keys the way a well-behaved API would.
        for existing in PUBLISHED:
            if existing["idempotency_key"] == idempotency_key:
                return PublishResult(existing["id"], existing["url"])
        post_id = f"test-{next(_ids)}"
        record = {
            "id": post_id,
            "url": f"https://sandbox.invalid/posts/{post_id}",
            "account": account.external_account_id,
            "idempotency_key": idempotency_key,
            "text": post.text,
            "media": [m.url for m in post.media],
            "kind": post.kind,
        }
        PUBLISHED.append(record)
        return PublishResult(post_id, record["url"])

    def delete(self, account: AccountContext, external_id: str) -> None:
        for i, existing in enumerate(PUBLISHED):
            if existing["id"] == external_id:
                PUBLISHED.pop(i)
                return

    def check_credentials(self, account: AccountContext) -> CredentialStatus:
        if account.settings.get("fail_mode") == "auth_expired" or not account.access_token:
            return CredentialStatus(ok=False, message="sandbox token expired")
        return CredentialStatus(ok=True, message="sandbox ok")
