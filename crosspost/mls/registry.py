"""Construct the configured MLS provider."""
from __future__ import annotations

from ..config import Settings, get_settings
from .base import MLSProvider

_override: MLSProvider | None = None


def set_provider_override(provider: MLSProvider | None) -> None:
    """Used by tests and the CLI to inject a provider instance."""
    global _override
    _override = provider


def get_provider(settings: Settings | None = None) -> MLSProvider:
    if _override is not None:
        return _override
    settings = settings or get_settings()
    if settings.mls_provider == "mock":
        from .mock import MockMLSProvider

        return MockMLSProvider(feed_path=settings.mls_mock_feed_path, base_url=settings.public_base_url)
    if settings.mls_provider == "reso":
        from .reso import RESOWebAPIProvider

        return RESOWebAPIProvider(
            settings.reso_base_url or "",
            access_token=settings.reso_access_token.get_secret_value() if settings.reso_access_token else None,
            token_url=settings.reso_token_url,
            client_id=settings.reso_client_id,
            client_secret=settings.reso_client_secret.get_secret_value() if settings.reso_client_secret else None,
            scope=settings.reso_scope,
            resource=settings.reso_resource,
            expand=settings.reso_expand,
            page_size=settings.reso_page_size,
            extra_filter=settings.reso_extra_filter,
            timeout=settings.mls_request_timeout_seconds,
        )
    raise ValueError(f"unknown MLS provider {settings.mls_provider}")
