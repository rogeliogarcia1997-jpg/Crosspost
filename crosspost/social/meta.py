"""Meta platforms: Facebook Pages, Instagram professional accounts, Threads.

Requirements (all require a Meta app that has passed App Review for the
listed permissions, and a Business/Creator account linked as described):

* Facebook Pages: ``pages_manage_posts``, ``pages_read_engagement``,
  ``pages_show_list``. Posts are made with a Page access token.
* Instagram (Content Publishing API): professional (Business or Creator)
  account connected to a Facebook Page; ``instagram_basic``,
  ``instagram_content_publish``. Images must be publicly reachable JPEG URLs.
  Links in captions are not clickable. Meta enforces a rolling 24-hour
  API publishing limit per account.
* Threads: ``threads_basic``, ``threads_content_publish``.

API versions and limits change; verify against current Meta documentation.
"""
from __future__ import annotations

import time
from typing import Any

import httpx

from ..config import get_settings
from .base import (
    AccountContext,
    AuthExpiredError,
    CredentialStatus,
    NotConfiguredError,
    OAuthConfig,
    PermanentPublishError,
    PlatformSpec,
    PublishResult,
    RateLimitError,
    RenderedPost,
    SocialAdapter,
    TokenSet,
    TransientPublishError,
    _safe_excerpt,
)

# Graph API error codes: https://developers.facebook.com/docs/graph-api/guides/error-handling
_AUTH_CODES = {102, 190, 10, 200, 299}  # invalid/expired token, missing permission
_RATE_CODES = {4, 17, 32, 613, 80001, 80002, 80006}
_TRANSIENT_CODES = {1, 2}


class MetaGraphAdapter(SocialAdapter):
    base_url = "https://graph.facebook.com"

    @property
    def graph(self) -> str:
        return f"{self.base_url}/{get_settings().meta_graph_version}"

    def _check(self, resp: httpx.Response) -> dict[str, Any]:
        if resp.status_code < 400:
            return resp.json()
        try:
            err = resp.json().get("error", {})
        except ValueError:
            err = {}
        code = err.get("code")
        excerpt = _safe_excerpt(resp)
        if resp.status_code == 429 or code in _RATE_CODES:
            raise RateLimitError(f"Meta rate limit: {excerpt}", retry_after=None, http_status=resp.status_code)
        if code in _AUTH_CODES or resp.status_code == 401:
            raise AuthExpiredError(f"Meta authorization error: {excerpt}", http_status=resp.status_code)
        if code in _TRANSIENT_CODES or err.get("is_transient") or resp.status_code >= 500:
            raise TransientPublishError(f"Meta transient error: {excerpt}", http_status=resp.status_code)
        raise PermanentPublishError(f"Meta rejected request: {excerpt}", http_status=resp.status_code)

    def oauth_config(self) -> OAuthConfig | None:
        s = get_settings()
        if not (s.meta_app_id and s.meta_app_secret):
            return None
        return OAuthConfig(
            authorize_url=f"https://www.facebook.com/{s.meta_graph_version}/dialog/oauth",
            token_url=f"{self.graph}/oauth/access_token",
            client_id=s.meta_app_id,
            client_secret=s.meta_app_secret.get_secret_value(),
            scopes=self.spec.required_scopes,
            scope_separator=",",
        )

    def refresh(self, account: AccountContext) -> TokenSet | None:
        return None  # Page tokens derived from long-lived user tokens do not expire; reconnect instead.

    def _long_lived_user_token(self, short_token: str) -> str:
        s = get_settings()
        if not (s.meta_app_id and s.meta_app_secret):
            raise NotConfiguredError("META_APP_ID / META_APP_SECRET not configured")
        resp = self.request("GET", f"{self.graph}/oauth/access_token", params={
            "grant_type": "fb_exchange_token",
            "client_id": s.meta_app_id,
            "client_secret": s.meta_app_secret.get_secret_value(),
            "fb_exchange_token": short_token,
        })
        return self._check(resp)["access_token"]

    def check_credentials(self, account: AccountContext) -> CredentialStatus:
        if not account.access_token:
            return CredentialStatus(False, "no token")
        try:
            self._check(self.request("GET", f"{self.graph}/{account.external_account_id}",
                                     params={"fields": "id", "access_token": account.access_token}))
        except AuthExpiredError as exc:
            return CredentialStatus(False, str(exc))
        return CredentialStatus(True, "ok")


class FacebookPageAdapter(MetaGraphAdapter):
    spec = PlatformSpec(
        platform="facebook",
        display_name="Facebook Page",
        style="long",
        max_text=5000,
        max_photos=10,
        max_hashtags=3,
        supports_delete=True,
        required_scopes=("pages_show_list", "pages_manage_posts", "pages_read_engagement"),
        account_requirements="A Facebook Page you administer and a Meta app approved (App Review) for the listed permissions.",
        limitations="Personal profiles and Groups cannot be posted to via the API; use the Manual workflow for those.",
        account_id_label="Facebook Page ID",
    )

    def finalize_connection(self, tokens: TokenSet, external_account_id: str) -> tuple[TokenSet, dict[str, Any]]:
        user_token = self._long_lived_user_token(tokens.access_token)
        body = self._check(self.request("GET", f"{self.graph}/{external_account_id}",
                                        params={"fields": "access_token,name", "access_token": user_token}))
        if "access_token" not in body:
            raise AuthExpiredError("Page token not returned; confirm you administer this Page and granted pages_manage_posts")
        return TokenSet(body["access_token"], None, None, tokens.scopes), {"page_name": body.get("name")}

    def publish(self, account: AccountContext, post: RenderedPost, idempotency_key: str) -> PublishResult:
        page = account.external_account_id
        token = account.access_token
        if not token:
            raise AuthExpiredError("missing Page access token")
        attached = []
        for item in post.media[: self.spec.max_photos]:
            photo = self._check(self.request("POST", f"{self.graph}/{page}/photos", data={
                "url": item.url, "published": "false", "alt_text_custom": item.alt, "access_token": token,
            }))
            attached.append(photo["id"])
        data: dict[str, Any] = {"message": post.text, "access_token": token}
        if attached:
            for i, media_id in enumerate(attached):
                data[f"attached_media[{i}]"] = f'{{"media_fbid":"{media_id}"}}'
        elif post.link:
            data["link"] = post.link
        body = self._check(self.request("POST", f"{self.graph}/{page}/feed", data=data))
        post_id = body["id"]
        return PublishResult(post_id, f"https://www.facebook.com/{post_id}", body)

    def delete(self, account: AccountContext, external_id: str) -> None:
        self._check(self.request("DELETE", f"{self.graph}/{external_id}", params={"access_token": account.access_token}))


class InstagramAdapter(MetaGraphAdapter):
    spec = PlatformSpec(
        platform="instagram",
        display_name="Instagram (Professional)",
        style="caption",
        max_text=2200,
        max_photos=10,
        min_photos=1,
        max_hashtags=10,
        link_clickable=False,
        supports_delete=False,
        required_scopes=("instagram_basic", "instagram_content_publish", "pages_show_list", "pages_read_engagement"),
        account_requirements="Instagram Business or Creator account linked to a Facebook Page; Meta App Review for content publishing.",
        limitations="Links in captions are not clickable (posts direct buyers to the link in bio, served by /listings). "
                    "At least one photo is required. Personal accounts cannot publish via the API. "
                    "The API cannot delete posts.",
        account_id_label="Instagram User ID",
    )
    status_poll_attempts = 5
    status_poll_seconds = 2.0

    def finalize_connection(self, tokens: TokenSet, external_account_id: str) -> tuple[TokenSet, dict[str, Any]]:
        return TokenSet(self._long_lived_user_token(tokens.access_token), None, None, tokens.scopes), {}

    def _wait_ready(self, container_id: str, token: str) -> None:
        for _ in range(self.status_poll_attempts):
            body = self._check(self.request("GET", f"{self.graph}/{container_id}",
                                            params={"fields": "status_code", "access_token": token}))
            status = body.get("status_code")
            if status in (None, "FINISHED", "PUBLISHED"):
                return
            if status in ("ERROR", "EXPIRED"):
                raise PermanentPublishError(f"Instagram media container {status}")
            time.sleep(self.status_poll_seconds)
        raise TransientPublishError("Instagram media container not ready")

    def publish(self, account: AccountContext, post: RenderedPost, idempotency_key: str) -> PublishResult:
        ig = account.external_account_id
        token = account.access_token
        if not token:
            raise AuthExpiredError("missing Instagram access token")
        if not post.media:
            raise PermanentPublishError("Instagram requires at least one photo")
        media = post.media[: self.spec.max_photos]
        if len(media) == 1:
            container = self._check(self.request("POST", f"{self.graph}/{ig}/media", data={
                "image_url": media[0].url, "caption": post.text, "alt_text": media[0].alt, "access_token": token,
            }))["id"]
        else:
            children = [
                self._check(self.request("POST", f"{self.graph}/{ig}/media", data={
                    "image_url": m.url, "is_carousel_item": "true", "alt_text": m.alt, "access_token": token,
                }))["id"]
                for m in media
            ]
            container = self._check(self.request("POST", f"{self.graph}/{ig}/media", data={
                "media_type": "CAROUSEL", "children": ",".join(children), "caption": post.text, "access_token": token,
            }))["id"]
        self._wait_ready(container, token)
        media_id = self._check(self.request("POST", f"{self.graph}/{ig}/media_publish",
                                            data={"creation_id": container, "access_token": token}))["id"]
        permalink = None
        try:
            permalink = self._check(self.request("GET", f"{self.graph}/{media_id}",
                                                 params={"fields": "permalink", "access_token": token})).get("permalink")
        except (TransientPublishError, PermanentPublishError, RateLimitError):
            pass  # published successfully; permalink is best-effort
        return PublishResult(media_id, permalink)


class ThreadsAdapter(MetaGraphAdapter):
    base_url = "https://graph.threads.net"
    spec = PlatformSpec(
        platform="threads",
        display_name="Threads",
        style="short",
        max_text=500,
        max_photos=10,
        max_hashtags=1,
        supports_delete=False,
        required_scopes=("threads_basic", "threads_content_publish"),
        account_requirements="Threads profile and a Meta app approved for the Threads API.",
        limitations="500 character limit; one topic tag per post.",
        account_id_label="Threads User ID",
    )

    @property
    def graph(self) -> str:
        return f"{self.base_url}/v1.0"

    def oauth_config(self) -> OAuthConfig | None:
        s = get_settings()
        if not (s.meta_app_id and s.meta_app_secret):
            return None
        return OAuthConfig(
            authorize_url="https://threads.net/oauth/authorize",
            token_url=f"{self.base_url}/oauth/access_token",
            client_id=s.meta_app_id,
            client_secret=s.meta_app_secret.get_secret_value(),
            scopes=self.spec.required_scopes,
            scope_separator=",",
        )

    def publish(self, account: AccountContext, post: RenderedPost, idempotency_key: str) -> PublishResult:
        uid, token = account.external_account_id, account.access_token
        if not token:
            raise AuthExpiredError("missing Threads access token")
        media = post.media[: self.spec.max_photos]
        if not media:
            data = {"media_type": "TEXT", "text": post.text, "access_token": token}
        elif len(media) == 1:
            data = {"media_type": "IMAGE", "image_url": media[0].url, "text": post.text, "access_token": token}
        else:
            children = [
                self._check(self.request("POST", f"{self.graph}/{uid}/threads", data={
                    "media_type": "IMAGE", "image_url": m.url, "is_carousel_item": "true", "access_token": token,
                }))["id"]
                for m in media
            ]
            data = {"media_type": "CAROUSEL", "children": ",".join(children), "text": post.text, "access_token": token}
        container = self._check(self.request("POST", f"{self.graph}/{uid}/threads", data=data))["id"]
        thread_id = self._check(self.request("POST", f"{self.graph}/{uid}/threads_publish",
                                             data={"creation_id": container, "access_token": token}))["id"]
        return PublishResult(thread_id, None)
