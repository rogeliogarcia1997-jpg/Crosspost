"""Pinterest API v5 adapter (creates a Pin on a board).

Requires an approved Pinterest app (trial access is limited to sandbox /
own-account use until Standard access is granted) and scopes
``boards:read pins:read pins:write``.
"""
from __future__ import annotations

from ..config import get_settings
from .base import (
    AccountContext,
    AuthExpiredError,
    CredentialStatus,
    OAuthConfig,
    PermanentPublishError,
    PlatformSpec,
    PublishResult,
    RenderedPost,
    SocialAdapter,
)

API = "https://api.pinterest.com/v5"


class PinterestAdapter(SocialAdapter):
    spec = PlatformSpec(
        platform="pinterest",
        display_name="Pinterest",
        style="pin",
        max_text=500,
        max_photos=1,
        min_photos=1,
        max_hashtags=3,
        title_max=100,
        supports_delete=True,
        required_scopes=("boards:read", "pins:read", "pins:write"),
        account_requirements="Pinterest business account and an app approved for Standard API access.",
        limitations="One image per Pin; title 100 characters, description 500 characters.",
        account_id_label="Board ID",
    )

    def oauth_config(self) -> OAuthConfig | None:
        s = get_settings()
        if not (s.pinterest_app_id and s.pinterest_app_secret):
            return None
        return OAuthConfig(
            authorize_url="https://www.pinterest.com/oauth/",
            token_url=f"{API}/oauth/token",
            client_id=s.pinterest_app_id,
            client_secret=s.pinterest_app_secret.get_secret_value(),
            scopes=self.spec.required_scopes,
            scope_separator=",",
            token_auth="basic",
        )

    def publish(self, account: AccountContext, post: RenderedPost, idempotency_key: str) -> PublishResult:
        if not account.access_token:
            raise AuthExpiredError("missing Pinterest access token")
        if not post.media:
            raise PermanentPublishError("Pinterest requires an image")
        body = {
            "board_id": account.external_account_id,
            "title": (post.title or "")[:100],
            "description": post.text[:500],
            "link": post.link,
            "alt_text": post.media[0].alt[:500],
            "media_source": {"source_type": "image_url", "url": post.media[0].url},
        }
        resp = self.request("POST", f"{API}/pins", headers={"Authorization": f"Bearer {account.access_token}"}, json=body)
        self.raise_for_response(resp)
        pin_id = resp.json()["id"]
        return PublishResult(pin_id, f"https://www.pinterest.com/pin/{pin_id}/")

    def delete(self, account: AccountContext, external_id: str) -> None:
        resp = self.request("DELETE", f"{API}/pins/{external_id}", headers={"Authorization": f"Bearer {account.access_token}"})
        self.raise_for_response(resp)

    def check_credentials(self, account: AccountContext) -> CredentialStatus:
        if not account.access_token:
            return CredentialStatus(False, "no token")
        resp = self.request("GET", f"{API}/user_account", headers={"Authorization": f"Bearer {account.access_token}"})
        if resp.status_code == 401:
            return CredentialStatus(False, "Pinterest token expired or revoked")
        return CredentialStatus(True, "ok")
