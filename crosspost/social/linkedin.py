"""LinkedIn Posts API adapter.

Posting as an organization (company page) requires the Community Management
API product (``w_organization_social``), which LinkedIn grants after review.
Posting as a member requires ``w_member_social``. Images are uploaded through
the Images API before the post is created.
"""
from __future__ import annotations

import re
from typing import Any
from urllib.parse import quote

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
    TransientPublishError,
)

API = "https://api.linkedin.com/rest"
# Reserved characters in LinkedIn's "little text" commentary format.
_RESERVED = re.compile(r"([\\|{}@\[\]()<>#*_~])")
_HASHTAG = re.compile(r"(?<!\w)#(\w+)")


def to_little_text(text: str) -> str:
    """Escape reserved characters and convert #tags to LinkedIn hashtag templates."""
    placeholders: list[str] = []

    def keep(match: re.Match[str]) -> str:
        placeholders.append(match.group(1))
        return f"\x00{len(placeholders) - 1}\x00"

    text = _HASHTAG.sub(keep, text)
    text = _RESERVED.sub(r"\\\1", text)
    return re.sub(r"\x00(\d+)\x00", lambda m: "{hashtag|\\#|" + placeholders[int(m.group(1))] + "}", text)


class LinkedInAdapter(SocialAdapter):
    spec = PlatformSpec(
        platform="linkedin",
        display_name="LinkedIn",
        style="long",
        max_text=3000,
        max_photos=9,
        max_hashtags=3,
        supports_delete=True,
        required_scopes=("w_organization_social", "r_organization_social"),
        account_requirements="Company page admin access and LinkedIn approval for the Community Management API "
                             "(w_organization_social). Member posting uses w_member_social instead.",
        limitations="Refresh tokens are only issued to approved partners; otherwise reconnect when the token expires (~60 days).",
        account_id_label="Author URN (urn:li:organization:123)",
    )

    def _headers(self, token: str) -> dict[str, str]:
        return {
            "Authorization": f"Bearer {token}",
            "LinkedIn-Version": get_settings().linkedin_api_version,
            "X-Restli-Protocol-Version": "2.0.0",
        }

    def oauth_config(self) -> OAuthConfig | None:
        s = get_settings()
        if not (s.linkedin_client_id and s.linkedin_client_secret):
            return None
        return OAuthConfig(
            authorize_url="https://www.linkedin.com/oauth/v2/authorization",
            token_url="https://www.linkedin.com/oauth/v2/accessToken",
            client_id=s.linkedin_client_id,
            client_secret=s.linkedin_client_secret.get_secret_value(),
            scopes=self.spec.required_scopes,
        )

    def _upload_image(self, token: str, owner: str, url: str) -> str:
        init = self.request("POST", f"{API}/images?action=initializeUpload", headers=self._headers(token),
                            json={"initializeUploadRequest": {"owner": owner}})
        self.raise_for_response(init, auth_codes=(401, 403))
        value = init.json()["value"]
        image = self.request("GET", url)
        if image.status_code >= 400:
            raise PermanentPublishError(f"could not fetch listing photo ({image.status_code})")
        put = self.request("PUT", value["uploadUrl"], content=image.content,
                           headers={"Authorization": f"Bearer {token}"})
        self.raise_for_response(put, auth_codes=(401, 403))
        return value["image"]

    def publish(self, account: AccountContext, post: RenderedPost, idempotency_key: str) -> PublishResult:
        token, author = account.access_token, account.external_account_id
        if not token:
            raise AuthExpiredError("missing LinkedIn access token")
        body: dict[str, Any] = {
            "author": author,
            "commentary": to_little_text(post.text),
            "visibility": "PUBLIC",
            "distribution": {"feedDistribution": "MAIN_FEED", "targetEntities": [], "thirdPartyDistributionChannels": []},
            "lifecycleState": "PUBLISHED",
            "isReshareDisabledByAuthor": False,
        }
        media = post.media[: self.spec.max_photos]
        if media:
            images = [{"id": self._upload_image(token, author, m.url), "altText": m.alt[:300]} for m in media]
            body["content"] = {"media": images[0]} if len(images) == 1 else {"multiImage": {"images": images}}
        elif post.link:
            body["content"] = {"article": {"source": post.link, "title": (post.title or post.text.split("\n")[0])[:200]}}
        resp = self.request("POST", f"{API}/posts", headers=self._headers(token), json=body)
        self.raise_for_response(resp, auth_codes=(401, 403))
        urn = resp.headers.get("x-restli-id") or resp.headers.get("x-linkedin-id")
        if not urn:
            raise TransientPublishError("LinkedIn did not return a post id", may_have_published=True)
        return PublishResult(urn, f"https://www.linkedin.com/feed/update/{urn}")

    def delete(self, account: AccountContext, external_id: str) -> None:
        resp = self.request("DELETE", f"{API}/posts/{quote(external_id, safe='')}", headers=self._headers(account.access_token or ""))
        self.raise_for_response(resp, auth_codes=(401, 403))

    def check_credentials(self, account: AccountContext) -> CredentialStatus:
        if not account.access_token:
            return CredentialStatus(False, "no token")
        resp = self.request("GET", "https://api.linkedin.com/v2/userinfo", headers={"Authorization": f"Bearer {account.access_token}"})
        if resp.status_code == 401:
            return CredentialStatus(False, "LinkedIn token expired or revoked")
        return CredentialStatus(True, "ok")
