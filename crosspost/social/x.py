"""X (Twitter) API v2 adapter.

Uses OAuth 2.0 Authorization Code with PKCE (user context). Scopes:
``tweet.read tweet.write users.read media.write offline.access``. Write
access and volume depend on the developer account's access tier (paid tiers
are required for most production use). Verify endpoints and limits against
current X developer documentation before going live.
"""
from __future__ import annotations

import time

from ..config import get_settings
from .base import (
    AccountContext,
    AuthExpiredError,
    CredentialStatus,
    OAuthConfig,
    PermanentPublishError,
    PlatformSpec,
    PublishResult,
    RateLimitError,
    RenderedPost,
    SocialAdapter,
)

API = "https://api.x.com/2"


class XAdapter(SocialAdapter):
    spec = PlatformSpec(
        platform="x",
        display_name="X",
        style="short",
        max_text=280,
        max_photos=4,
        max_hashtags=2,
        url_length=23,
        supports_delete=True,
        required_scopes=("tweet.read", "tweet.write", "users.read", "media.write", "offline.access"),
        account_requirements="X developer project with an access tier that permits posting, and OAuth 2.0 enabled.",
        limitations="280 characters (links count as 23); up to 4 images; posting quotas depend on the paid access tier.",
        account_id_label="X user ID",
    )

    def oauth_config(self) -> OAuthConfig | None:
        s = get_settings()
        if not s.x_client_id:
            return None
        return OAuthConfig(
            authorize_url="https://x.com/i/oauth2/authorize",
            token_url=f"{API}/oauth2/token",
            client_id=s.x_client_id,
            client_secret=s.x_client_secret.get_secret_value() if s.x_client_secret else None,
            scopes=self.spec.required_scopes,
            use_pkce=True,
            token_auth="basic" if s.x_client_secret else "body",
        )

    def _rate_limit_check(self, resp) -> None:
        if resp.status_code == 429:
            reset = resp.headers.get("x-rate-limit-reset")
            retry_after = max(float(reset) - time.time(), 1.0) if reset and reset.isdigit() else None
            raise RateLimitError("X rate limit reached", retry_after=retry_after, http_status=429)

    def _upload(self, token: str, url: str) -> str:
        image = self.request("GET", url)
        if image.status_code >= 400:
            raise PermanentPublishError(f"could not fetch listing photo ({image.status_code})")
        resp = self.request(
            "POST", f"{API}/media/upload", headers={"Authorization": f"Bearer {token}"},
            data={"media_category": "tweet_image"},
            files={"media": ("photo.jpg", image.content, image.headers.get("content-type", "image/jpeg"))},
        )
        self._rate_limit_check(resp)
        self.raise_for_response(resp)
        data = resp.json().get("data") or resp.json()
        return str(data.get("id") or data.get("media_id_string") or data.get("media_id"))

    def publish(self, account: AccountContext, post: RenderedPost, idempotency_key: str) -> PublishResult:
        token = account.access_token
        if not token:
            raise AuthExpiredError("missing X access token")
        body: dict = {"text": post.text}
        media_ids = [self._upload(token, m.url) for m in post.media[: self.spec.max_photos]]
        if media_ids:
            body["media"] = {"media_ids": media_ids}
        resp = self.request("POST", f"{API}/tweets", headers={"Authorization": f"Bearer {token}"}, json=body)
        self._rate_limit_check(resp)
        self.raise_for_response(resp)
        tweet_id = resp.json()["data"]["id"]
        return PublishResult(tweet_id, f"https://x.com/i/web/status/{tweet_id}")

    def delete(self, account: AccountContext, external_id: str) -> None:
        resp = self.request("DELETE", f"{API}/tweets/{external_id}", headers={"Authorization": f"Bearer {account.access_token}"})
        self._rate_limit_check(resp)
        self.raise_for_response(resp)

    def check_credentials(self, account: AccountContext) -> CredentialStatus:
        if not account.access_token:
            return CredentialStatus(False, "no token")
        resp = self.request("GET", f"{API}/users/me", headers={"Authorization": f"Bearer {account.access_token}"})
        if resp.status_code == 401:
            return CredentialStatus(False, "X token expired or revoked")
        return CredentialStatus(True, "ok")
