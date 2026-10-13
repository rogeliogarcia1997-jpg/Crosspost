"""Social publishing adapter interface.

Each platform implements :class:`SocialAdapter`. The core publishing service
only depends on this interface, the :class:`PlatformSpec` content
requirements, and the error taxonomy below:

* :class:`RateLimitError`     - retry after the given delay
* :class:`AuthExpiredError`   - stop; account needs re-authorization (never retried)
* :class:`TransientPublishError` - retry with exponential backoff, unless the
  request may already have been accepted (``may_have_published``), in which
  case the post is held for manual verification to avoid duplicates
* :class:`PermanentPublishError` - do not retry (validation / policy errors)
"""
from __future__ import annotations

import re
from abc import ABC, abstractmethod
from dataclasses import dataclass, field
from datetime import datetime
from typing import Any

import httpx

from ..config import get_settings


class PublishError(Exception):
    def __init__(self, message: str, *, http_status: int | None = None):
        super().__init__(message)
        self.http_status = http_status


class RateLimitError(PublishError):
    def __init__(self, message: str, retry_after: float | None = None, **kw: Any):
        super().__init__(message, **kw)
        self.retry_after = retry_after


class AuthExpiredError(PublishError):
    pass


class TransientPublishError(PublishError):
    def __init__(self, message: str, may_have_published: bool = False, **kw: Any):
        super().__init__(message, **kw)
        self.may_have_published = may_have_published


class PermanentPublishError(PublishError):
    pass


class NotConfiguredError(PublishError):
    """Platform app credentials are missing from the environment."""


_URL_RE = re.compile(r"https?://\S+")


@dataclass(frozen=True)
class PlatformSpec:
    platform: str
    display_name: str
    style: str  # long | caption | short | pin
    max_text: int
    max_photos: int
    min_photos: int = 0
    max_hashtags: int = 5
    link_clickable: bool = True
    url_length: int | None = None  # platforms that count every URL as a fixed length
    title_max: int | None = None
    supports_delete: bool = False
    automated_publishing: bool = True
    oauth: bool = True
    required_scopes: tuple[str, ...] = ()
    account_requirements: str = ""
    limitations: str = ""
    account_id_label: str = "Account ID"

    def measure(self, text: str) -> int:
        if self.url_length is None:
            return len(text)
        return len(_URL_RE.sub("x" * self.url_length, text))


@dataclass
class MediaItem:
    url: str
    alt: str = ""


@dataclass
class RenderedPost:
    platform: str
    kind: str
    text: str
    link: str | None
    title: str | None = None
    media: list[MediaItem] = field(default_factory=list)
    hashtags: list[str] = field(default_factory=list)
    warnings: list[str] = field(default_factory=list)
    requires_review: bool = False


@dataclass
class AccountContext:
    platform: str
    external_account_id: str
    access_token: str | None
    settings: dict[str, Any] = field(default_factory=dict)
    refresh_token: str | None = None


@dataclass
class PublishResult:
    external_id: str
    url: str | None = None
    raw: dict[str, Any] = field(default_factory=dict)


@dataclass
class CredentialStatus:
    ok: bool
    message: str = ""
    expires_at: datetime | None = None
    granted_scopes: list[str] | None = None


@dataclass
class OAuthConfig:
    authorize_url: str
    token_url: str
    client_id: str
    client_secret: str | None
    scopes: tuple[str, ...]
    use_pkce: bool = False
    scope_separator: str = " "
    token_auth: str = "body"  # body | basic


@dataclass
class TokenSet:
    access_token: str
    refresh_token: str | None = None
    expires_at: datetime | None = None
    scopes: list[str] | None = None


class SocialAdapter(ABC):
    spec: PlatformSpec

    def __init__(self, client: httpx.Client | None = None):
        self._client = client

    @property
    def client(self) -> httpx.Client:
        if self._client is None:
            self._client = httpx.Client(timeout=get_settings().social_request_timeout_seconds)
        return self._client

    # --- content ---------------------------------------------------------
    def validate(self, post: RenderedPost) -> list[str]:
        errors = []
        if self.spec.measure(post.text) > self.spec.max_text:
            errors.append(f"text exceeds {self.spec.max_text} characters")
        if len(post.media) < self.spec.min_photos:
            errors.append(f"{self.spec.display_name} requires at least {self.spec.min_photos} photo(s)")
        if len(post.media) > self.spec.max_photos:
            errors.append(f"{self.spec.display_name} allows at most {self.spec.max_photos} photos")
        if self.spec.title_max and post.title and len(post.title) > self.spec.title_max:
            errors.append(f"title exceeds {self.spec.title_max} characters")
        return errors

    # --- publishing --------------------------------------------------------
    @abstractmethod
    def publish(self, account: AccountContext, post: RenderedPost, idempotency_key: str) -> PublishResult:
        ...

    def delete(self, account: AccountContext, external_id: str) -> None:
        raise PermanentPublishError(f"{self.spec.display_name} adapter does not support deleting posts")

    def check_credentials(self, account: AccountContext) -> CredentialStatus:
        return CredentialStatus(ok=bool(account.access_token), message="token present" if account.access_token else "no token")

    def fetch_metrics(self, account: AccountContext, external_id: str) -> dict[str, Any] | None:
        """Platform-reported metrics, or None when not available via an authorized API."""
        return None

    # --- OAuth ------------------------------------------------------------
    def oauth_config(self) -> OAuthConfig | None:
        return None

    def refresh(self, account: AccountContext) -> TokenSet | None:
        """Refresh an access token when the platform supports it."""
        cfg = self.oauth_config()
        if not cfg or not account.refresh_token:
            return None
        data = {"grant_type": "refresh_token", "refresh_token": account.refresh_token}
        return self._token_request(cfg, data)

    def exchange_code(self, code: str, redirect_uri: str, code_verifier: str | None) -> TokenSet:
        cfg = self.oauth_config()
        if not cfg:
            raise NotConfiguredError(f"{self.spec.display_name} OAuth is not configured")
        data = {"grant_type": "authorization_code", "code": code, "redirect_uri": redirect_uri}
        if code_verifier:
            data["code_verifier"] = code_verifier
        return self._token_request(cfg, data)

    def _token_request(self, cfg: OAuthConfig, data: dict[str, str]) -> TokenSet:
        auth = None
        if cfg.token_auth == "basic":
            auth = (cfg.client_id, cfg.client_secret or "")
        else:
            data = {**data, "client_id": cfg.client_id}
            if cfg.client_secret:
                data["client_secret"] = cfg.client_secret
        try:
            resp = self.client.post(cfg.token_url, data=data, auth=auth)
        except httpx.HTTPError as exc:
            raise TransientPublishError(f"token endpoint unreachable: {type(exc).__name__}") from exc
        if resp.status_code in (400, 401, 403):
            raise AuthExpiredError(f"token request rejected ({resp.status_code})", http_status=resp.status_code)
        if resp.status_code >= 400:
            raise TransientPublishError(f"token endpoint error ({resp.status_code})", http_status=resp.status_code)
        body = resp.json()
        expires_at = None
        if body.get("expires_in"):
            from datetime import timedelta

            from ..db import utcnow

            expires_at = utcnow() + timedelta(seconds=int(body["expires_in"]))
        scope = body.get("scope")
        scopes = scope.replace(",", " ").split() if isinstance(scope, str) else None
        return TokenSet(body["access_token"], body.get("refresh_token"), expires_at, scopes)

    def finalize_connection(self, tokens: TokenSet, external_account_id: str) -> tuple[TokenSet, dict[str, Any]]:
        """Platform-specific post-OAuth step (e.g. exchanging a user token for a Page token)."""
        return tokens, {}

    # --- shared HTTP error mapping ----------------------------------------
    def raise_for_response(self, resp: httpx.Response, *, auth_codes: tuple[int, ...] = (401,)) -> None:
        status = resp.status_code
        if status < 400:
            return
        excerpt = _safe_excerpt(resp)
        if status == 429:
            ra = resp.headers.get("Retry-After") or resp.headers.get("x-rate-limit-reset-after")
            retry_after = float(ra) if ra and ra.replace(".", "", 1).isdigit() else None
            raise RateLimitError(f"rate limited: {excerpt}", retry_after=retry_after, http_status=status)
        if status in auth_codes:
            raise AuthExpiredError(f"authorization rejected: {excerpt}", http_status=status)
        if status >= 500:
            raise TransientPublishError(f"server error: {excerpt}", http_status=status)
        raise PermanentPublishError(f"request rejected: {excerpt}", http_status=status)

    def request(self, method: str, url: str, **kwargs: Any) -> httpx.Response:
        """HTTP call with timeout/connection errors mapped to TransientPublishError.

        A read timeout on a write request is reported with ``may_have_published``
        because the platform may have accepted the post before the timeout.
        """
        try:
            return self.client.request(method, url, **kwargs)
        except httpx.ConnectError as exc:
            raise TransientPublishError("connection failed") from exc
        except httpx.ConnectTimeout as exc:
            raise TransientPublishError("connection timed out") from exc
        except httpx.TimeoutException as exc:
            raise TransientPublishError("request timed out", may_have_published=method.upper() == "POST") from exc
        except httpx.HTTPError as exc:
            raise TransientPublishError(f"http error: {type(exc).__name__}", may_have_published=method.upper() == "POST") from exc


def _safe_excerpt(resp: httpx.Response, limit: int = 300) -> str:
    from ..logging_setup import scrub_text

    try:
        body = resp.json()
        err = body.get("error") if isinstance(body, dict) else None
        if isinstance(err, dict):
            text = f"{resp.status_code} {err.get('type', '')} {err.get('code', '')} {err.get('message', '')}".strip()
        elif isinstance(body, dict) and (body.get("message") or body.get("detail") or body.get("title")):
            text = f"{resp.status_code} {body.get('title') or ''} {body.get('message') or body.get('detail') or ''}".strip()
        else:
            text = f"{resp.status_code} {str(body)[:limit]}"
    except ValueError:
        text = f"{resp.status_code} {resp.text[:limit]}"
    return scrub_text(text)[:limit]
