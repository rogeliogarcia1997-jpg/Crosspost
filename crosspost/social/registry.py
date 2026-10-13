"""Adapter registry. Add a platform by implementing SocialAdapter and registering it here."""
from __future__ import annotations

import httpx

from .base import PlatformSpec, SocialAdapter
from .linkedin import LinkedInAdapter
from .meta import FacebookPageAdapter, InstagramAdapter, ThreadsAdapter
from .pinterest import PinterestAdapter
from .test_adapter import TestAdapter
from .webhook import ManualAdapter, WebhookAdapter
from .x import XAdapter

ADAPTERS: dict[str, type[SocialAdapter]] = {
    cls.spec.platform: cls
    for cls in (
        TestAdapter, FacebookPageAdapter, InstagramAdapter, LinkedInAdapter, XAdapter,
        PinterestAdapter, ThreadsAdapter, WebhookAdapter, ManualAdapter,
    )
}

_http_override: httpx.Client | None = None


def set_http_client_override(client: httpx.Client | None) -> None:
    """Route all adapter HTTP traffic through the given client (tests use httpx.MockTransport)."""
    global _http_override
    _http_override = client


def get_adapter(platform: str) -> SocialAdapter:
    try:
        cls = ADAPTERS[platform]
    except KeyError as exc:
        raise KeyError(f"unknown platform {platform!r}") from exc
    return cls(client=_http_override)


def get_spec(platform: str) -> PlatformSpec:
    return ADAPTERS[platform].spec


def all_specs() -> list[PlatformSpec]:
    return [cls.spec for cls in ADAPTERS.values()]
