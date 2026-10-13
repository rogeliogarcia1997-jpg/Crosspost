"""Generic outbound-webhook adapter and the manual publishing workflow.

* ``webhook``: POSTs a signed JSON payload to an automation service you
  control (Zapier, Make, n8n, a scheduling tool) for platforms that the
  application does not integrate with directly. The receiver must return a
  2xx response, optionally with ``{"id": "...", "url": "..."}``.
* ``manual``: for destinations that do not permit automated publishing
  (personal Facebook profiles, Facebook Groups, Instagram personal accounts,
  Nextdoor, etc.). Content is generated and placed in the queue as
  "ready for manual posting"; a team member copies it, posts it, and records
  the post URL in the dashboard.
"""
from __future__ import annotations

import hashlib
import hmac
import json

from ..config import get_settings
from .base import (
    AccountContext,
    NotConfiguredError,
    PermanentPublishError,
    PlatformSpec,
    PublishResult,
    RenderedPost,
    SocialAdapter,
)


class WebhookAdapter(SocialAdapter):
    spec = PlatformSpec(
        platform="webhook",
        display_name="Automation Webhook",
        style="long",
        max_text=5000,
        max_photos=10,
        max_hashtags=5,
        oauth=False,
        account_requirements="An HTTPS endpoint you control (SOCIAL_WEBHOOK_URL or the account's URL setting).",
        limitations="Delivery confirmation depends on the receiving automation; platform rules still apply downstream.",
        account_id_label="Destination name",
    )

    def publish(self, account: AccountContext, post: RenderedPost, idempotency_key: str) -> PublishResult:
        s = get_settings()
        url = account.settings.get("url") or s.social_webhook_url
        if not url:
            raise NotConfiguredError("no webhook URL configured")
        payload = {
            "idempotency_key": idempotency_key,
            "destination": account.external_account_id,
            "kind": post.kind,
            "text": post.text,
            "title": post.title,
            "link": post.link,
            "hashtags": post.hashtags,
            "media": [{"url": m.url, "alt": m.alt} for m in post.media],
        }
        raw = json.dumps(payload, sort_keys=True).encode()
        headers = {"Content-Type": "application/json", "Idempotency-Key": idempotency_key}
        secret = account.access_token or (s.social_webhook_secret.get_secret_value() if s.social_webhook_secret else None)
        if secret:
            headers["X-Crosspost-Signature"] = "sha256=" + hmac.new(secret.encode(), raw, hashlib.sha256).hexdigest()
        resp = self.request("POST", url, content=raw, headers=headers)
        self.raise_for_response(resp)
        try:
            body = resp.json()
        except ValueError:
            body = {}
        return PublishResult(str(body.get("id") or idempotency_key), body.get("url"))


class ManualAdapter(SocialAdapter):
    spec = PlatformSpec(
        platform="manual",
        display_name="Manual Posting",
        style="long",
        max_text=5000,
        max_photos=10,
        max_hashtags=5,
        automated_publishing=False,
        oauth=False,
        account_requirements="None. A team member posts the generated content and records the post URL.",
        limitations="Not automated. Use for destinations whose terms or APIs do not allow automated publishing.",
        account_id_label="Destination (e.g. Nextdoor, Facebook Group name)",
    )

    def publish(self, account: AccountContext, post: RenderedPost, idempotency_key: str) -> PublishResult:
        raise PermanentPublishError("manual destinations are published by a person, not the API")
