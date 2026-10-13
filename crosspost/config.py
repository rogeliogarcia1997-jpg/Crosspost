"""Application configuration.

All configuration comes from environment variables (optionally loaded from a
``.env`` file in development). Secrets are never hardcoded; the defaults below
are only safe, non-secret development values. ``validate_for_production``
refuses to start a production process with missing or placeholder secrets.
"""
from __future__ import annotations

from functools import lru_cache
from typing import Literal

from pydantic import Field, SecretStr, field_validator
from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    model_config = SettingsConfigDict(env_file=".env", env_prefix="", extra="ignore")

    # --- Core -------------------------------------------------------------
    app_env: Literal["development", "test", "production"] = "development"
    app_name: str = "Crosspost"
    public_base_url: str = "http://localhost:8000"
    secret_key: SecretStr = SecretStr("dev-insecure-secret-change-me")
    # Fernet key (urlsafe base64, 32 bytes) used to encrypt social tokens at rest.
    token_encryption_key: SecretStr | None = None
    database_url: str = "sqlite:///./data/local/crosspost.db"
    log_level: str = "INFO"
    log_format: Literal["json", "text"] = "json"
    sentry_dsn: SecretStr | None = None
    session_cookie_secure: bool = False
    session_max_age_seconds: int = 60 * 60 * 8

    # --- MLS --------------------------------------------------------------
    mls_provider: Literal["mock", "reso"] = "mock"
    mls_mock_feed_path: str = "data/mock_mls_feed.json"
    mls_sync_interval_minutes: int = 15
    reso_base_url: str | None = None  # e.g. https://api.example-mls.com/reso/odata
    reso_token_url: str | None = None  # OAuth2 client-credentials endpoint
    reso_client_id: str | None = None
    reso_client_secret: SecretStr | None = None
    reso_access_token: SecretStr | None = None  # static bearer token alternative
    reso_scope: str | None = None
    reso_page_size: int = 200
    reso_extra_filter: str | None = None  # appended to $filter with "and"
    reso_resource: str = "Property"
    reso_expand: str = "Media,OpenHouse"  # set empty if the server does not support $expand
    mls_request_timeout_seconds: float = 20.0
    mls_name: str = "the MLS"
    mls_disclaimer: str = (
        "Information deemed reliable but not guaranteed. Listing data provided by the MLS "
        "and subject to change without notice."
    )

    # --- Marketing authorization -----------------------------------------
    # own_office: only market listings whose listing office is in BROKERAGE_OFFICE_IDS
    # own_agent: only listings whose listing agent is in BROKERAGE_AGENT_IDS
    # flagged: only listings explicitly flagged as authorized in the dashboard
    marketing_scope: Literal["own_office", "own_agent", "flagged"] = "own_office"
    brokerage_office_ids: str = ""  # comma separated MLS office IDs
    brokerage_agent_ids: str = ""  # comma separated MLS agent IDs
    brokerage_name: str = ""
    brokerage_phone: str = ""
    brokerage_license: str = ""
    photos_per_post: int = 4
    display_timezone: str = "UTC"  # IANA zone used when rendering open house times
    post_attribution_suffix: str = ""  # extra disclosure appended to every post (e.g. license #)

    # --- Publishing defaults (overridable from the dashboard) ------------
    publish_mode_new_listing: Literal["immediate", "scheduled", "manual_approval"] = "manual_approval"
    publish_delay_minutes: int = 0
    announce_price_reductions: bool = True
    announce_open_houses: bool = True
    open_house_lead_hours: int = 48
    announce_status_changes: bool = False
    repost_interval_days: int = 0  # 0 disables reposting
    delete_posts_on_unavailable: bool = False
    enabled_platforms: str = "test"

    # --- Leads / privacy --------------------------------------------------
    lead_notification_emails: str = ""
    lead_rate_limit_per_10min: int = 5
    lead_form_min_seconds: int = 3
    privacy_contact_email: str = ""
    lead_retention_days: int = 0  # 0 keeps leads until erased; otherwise inquiries older than this are purged
    ip_hash_salt: SecretStr = SecretStr("dev-ip-salt")

    # --- Email ------------------------------------------------------------
    email_backend: Literal["console", "smtp"] = "console"
    smtp_host: str | None = None
    smtp_port: int = 587
    smtp_username: str | None = None
    smtp_password: SecretStr | None = None
    smtp_use_tls: bool = True
    email_from: str = "no-reply@example.com"

    # --- CRM --------------------------------------------------------------
    crm_provider: Literal["none", "webhook", "followupboss"] = "none"
    crm_webhook_url: str | None = None
    crm_webhook_secret: SecretStr | None = None
    followupboss_api_key: SecretStr | None = None
    followupboss_system: str = "Crosspost"
    followupboss_system_key: SecretStr | None = None

    # --- Social platform app credentials ---------------------------------
    social_request_timeout_seconds: float = 20.0
    meta_app_id: str | None = None
    meta_app_secret: SecretStr | None = None
    meta_graph_version: str = "v21.0"
    linkedin_client_id: str | None = None
    linkedin_client_secret: SecretStr | None = None
    linkedin_api_version: str = "202409"
    x_client_id: str | None = None
    x_client_secret: SecretStr | None = None
    pinterest_app_id: str | None = None
    pinterest_app_secret: SecretStr | None = None
    social_webhook_url: str | None = None
    social_webhook_secret: SecretStr | None = None

    # --- Worker -----------------------------------------------------------
    worker_poll_seconds: float = 2.0
    job_lock_timeout_seconds: int = 600
    job_max_attempts: int = 6
    job_backoff_base_seconds: int = 30
    job_backoff_max_seconds: int = 3600

    @field_validator("public_base_url")
    @classmethod
    def _strip_slash(cls, v: str) -> str:
        return v.rstrip("/")

    # --- helpers ------------------------------------------------------------
    @staticmethod
    def _csv(value: str) -> list[str]:
        return [item.strip() for item in value.split(",") if item.strip()]

    @property
    def office_ids(self) -> list[str]:
        return self._csv(self.brokerage_office_ids)

    @property
    def agent_ids(self) -> list[str]:
        return self._csv(self.brokerage_agent_ids)

    @property
    def enabled_platform_list(self) -> list[str]:
        return self._csv(self.enabled_platforms)

    @property
    def notification_recipients(self) -> list[str]:
        return self._csv(self.lead_notification_emails)

    @property
    def is_production(self) -> bool:
        return self.app_env == "production"

    def validate_for_production(self) -> list[str]:
        """Return a list of problems that must be fixed before running in production."""
        problems: list[str] = []
        if not self.is_production:
            return problems
        if "insecure" in self.secret_key.get_secret_value() or len(self.secret_key.get_secret_value()) < 32:
            problems.append("SECRET_KEY must be set to a random value of at least 32 characters")
        if not self.token_encryption_key:
            problems.append("TOKEN_ENCRYPTION_KEY must be set (Fernet key)")
        if self.ip_hash_salt.get_secret_value() == "dev-ip-salt":
            problems.append("IP_HASH_SALT must be set")
        if self.database_url.startswith("sqlite"):
            problems.append("DATABASE_URL should point to PostgreSQL in production")
        if not self.session_cookie_secure:
            problems.append("SESSION_COOKIE_SECURE must be true in production (HTTPS)")
        if not self.public_base_url.startswith("https://"):
            problems.append("PUBLIC_BASE_URL must use https in production")
        if self.mls_provider == "mock":
            problems.append("MLS_PROVIDER=mock is not allowed in production")
        if self.marketing_scope == "own_office" and not self.office_ids:
            problems.append("BROKERAGE_OFFICE_IDS is required when MARKETING_SCOPE=own_office")
        if self.marketing_scope == "own_agent" and not self.agent_ids:
            problems.append("BROKERAGE_AGENT_IDS is required when MARKETING_SCOPE=own_agent")
        if not self.brokerage_name:
            problems.append("BROKERAGE_NAME is required for listing attribution")
        return problems


@lru_cache
def get_settings() -> Settings:
    return Settings()


def reset_settings_cache() -> None:
    get_settings.cache_clear()
