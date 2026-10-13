"""Acceptance test 9: input validation, access control, secret handling, PII protection."""
from __future__ import annotations

import re

import pytest
from sqlalchemy import func, select

from crosspost.jobs.queue import run_pending
from crosspost.models import Inquiry, Lead, Listing, SocialAccount

from .conftest import PASSWORD, csrf_from


def form_token(html: str) -> str:
    return re.search(r'name="form_token" value="([^"]+)"', html).group(1)


@pytest.fixture
def listing(db, provider, connect, sync):
    connect("test")
    sync()
    run_pending()
    return db.scalar(select(Listing).where(Listing.mls_id == "MOCK1001"))


def submit(client, listing, **overrides):
    page = client.get(f"/p/{listing.slug}")
    data = {"form_token": form_token(page.text), "full_name": "Sam Buyer", "email": "sam@example.org",
            "preferred_contact_method": "email", "message": "Hello"}
    data.update(overrides)
    return client.post(f"/p/{listing.slug}/inquire", data=data)


# --- access control ----------------------------------------------------------------
@pytest.mark.parametrize("path", ["/admin", "/admin/leads", "/admin/leads/export.csv", "/admin/accounts", "/admin/settings"])
def test_admin_requires_login(client, path):
    resp = client.get(path)
    assert resp.status_code == 303 and resp.headers["location"].startswith("/login")


def test_viewer_cannot_see_lead_records(db, listing, client, login):
    submit(client, listing)
    lead = db.scalar(select(Lead))
    login("viewer")
    assert client.get("/admin/leads").status_code == 403
    assert client.get(f"/admin/leads/{lead.id}").status_code == 403
    assert client.get("/admin/leads/export.csv").status_code == 403
    overview = client.get("/admin")
    assert overview.status_code == 200 and "Sam Buyer" not in overview.text and "sam@example.org" not in overview.text


def test_marketer_cannot_administer(client, login):
    csrf = login("marketer")
    assert client.get("/admin/settings").status_code == 403
    assert client.get("/admin/users").status_code == 403
    resp = client.post("/admin/accounts/connect", data={"csrf_token": csrf, "platform": "test", "external_account_id": "x",
                                                        "display_name": "x", "access_token": "t"})
    assert resp.status_code == 403
    resp = client.post("/admin/users", data={"csrf_token": csrf, "email": "e@x.com", "password": PASSWORD, "role": "admin"})
    assert resp.status_code == 403


def test_viewer_cannot_change_posts(db, listing, client, login):
    from crosspost.models import Post

    csrf = login("viewer")
    post = db.scalar(select(Post))
    assert client.post(f"/admin/posts/{post.id}/retry", data={"csrf_token": csrf}).status_code == 403


def test_csrf_required_for_state_changes(client, login):
    login("admin")
    resp = client.post("/admin/sync", data={"mode": "full"})
    assert resp.status_code == 403
    resp = client.post("/admin/sync", data={"mode": "full", "csrf_token": "forged"})
    assert resp.status_code == 403


def test_login_failures_and_rate_limit(client, make_user):
    email = make_user("admin")
    csrf = csrf_from(client.get("/login").text)
    assert client.post("/login", data={"email": email, "password": "wrong", "csrf_token": csrf}).status_code == 401
    for _ in range(10):
        client.post("/login", data={"email": email, "password": "wrong", "csrf_token": csrf})
    resp = client.post("/login", data={"email": email, "password": PASSWORD, "csrf_token": csrf})
    assert resp.status_code == 429


def test_open_redirect_blocked_after_login(client, make_user):
    email = make_user("admin")
    csrf = csrf_from(client.get("/login").text)
    resp = client.post("/login", data={"email": email, "password": PASSWORD, "csrf_token": csrf, "next": "https://evil.example"})
    assert resp.headers["location"] == "/admin"


def test_deactivated_user_loses_access(db, client, login):
    from crosspost.models import User

    login("marketer")
    user = db.scalar(select(User).where(User.role == "marketer"))
    user.is_active = False
    db.commit()
    assert client.get("/admin").status_code == 303


# --- lead form validation and spam controls -------------------------------------------
@pytest.mark.parametrize("overrides,message", [
    ({"email": "not-an-email"}, "email"),
    ({"full_name": "x"}, "at least 2"),
    ({"full_name": "<script>alert(1)</script>"}, "valid name"),
    ({"preferred_contact_method": "text", "phone": "512-555-0100"}, "agree to receive text"),
    ({"preferred_contact_method": "phone"}, "phone number"),
    ({"phone": "call me maybe"}, "valid phone"),
    ({"message": "http://a.com http://b.com http://c.com"}, "remove links"),
])
def test_lead_form_validation(db, listing, client, overrides, message):
    resp = submit(client, listing, **overrides)
    assert resp.status_code == 422 and message in resp.text
    assert db.scalar(select(func.count()).select_from(Lead)) == 0


def test_marketing_consent_is_optional_and_separate(db, listing, client):
    assert submit(client, listing).status_code == 303
    lead = db.scalar(select(Lead))
    assert lead.marketing_consent is False and lead.sms_consent is False
    assert submit(client, listing, email="opt@example.org", marketing_consent="yes").status_code == 303
    assert db.scalar(select(Lead).where(Lead.marketing_consent.is_(True))) is not None


def test_honeypot_silently_discards(db, listing, client):
    resp = submit(client, listing, website="http://spam.example")
    assert resp.status_code == 303
    assert db.scalar(select(func.count()).select_from(Lead)) == 0


def test_missing_or_forged_form_token_rejected(db, listing, client):
    resp = submit(client, listing, form_token="forged")
    assert resp.status_code == 422 and db.scalar(select(func.count()).select_from(Lead)) == 0


def test_too_fast_submission_rejected(db, listing, client, monkeypatch):
    from crosspost.config import reset_settings_cache

    monkeypatch.setenv("LEAD_FORM_MIN_SECONDS", "30")
    reset_settings_cache()
    assert submit(client, listing).status_code == 422


def test_lead_submissions_rate_limited_per_ip(db, listing, client):
    for i in range(5):
        assert submit(client, listing, email=f"buyer{i}@example.org").status_code == 303
    resp = submit(client, listing, email="buyer99@example.org")
    assert resp.status_code == 429
    assert db.scalar(select(func.count()).select_from(Inquiry)) == 5


def test_unauthorized_listing_has_no_public_page(db, listing, client):
    other = db.scalar(select(Listing).where(Listing.mls_id == "MOCK2001"))
    assert client.get(f"/p/{other.slug}").status_code == 404
    assert client.get("/t/doesnotexist").status_code == 404


# --- secrets and PII handling ------------------------------------------------------------
def test_social_tokens_encrypted_at_rest(db, connect):
    from crosspost.services.accounts import account_context

    acct_id = connect("facebook", "page-1", token="EAAB-super-secret-token")
    account = db.get(SocialAccount, acct_id)
    assert "super-secret" not in (account.access_token_enc or "")
    assert account_context(account).access_token == "EAAB-super-secret-token"


def test_disconnect_wipes_tokens(db, connect, client, login):
    acct_id = connect("test")
    csrf = login("admin")
    assert client.post(f"/admin/accounts/{acct_id}/disconnect", data={"csrf_token": csrf}).status_code == 303
    db.expire_all()
    account = db.get(SocialAccount, acct_id)
    assert account.status == "disconnected" and account.access_token_enc is None and account.refresh_token_enc is None


def test_accounts_page_never_shows_tokens(db, connect, client, login):
    connect("facebook", "page-1", token="EAAB-super-secret-token")
    login("admin")
    page = client.get("/admin/accounts")
    assert "EAAB-super-secret-token" not in page.text


def test_logging_redacts_secrets_and_pii():
    from crosspost.logging_setup import redact, scrub_text

    assert redact({"access_token": "abc", "nested": {"email": "a@b.co"}, "ok": 1}) == {
        "access_token": "[REDACTED]", "nested": {"email": "[REDACTED]"}, "ok": 1}
    text = scrub_text("Authorization: Bearer abc.def-ghi for jane@example.com ?access_token=zzz&x=1")
    assert "abc.def" not in text and "jane@example.com" not in text and "zzz" not in text


def test_production_config_rejects_insecure_defaults(monkeypatch):
    from crosspost.config import Settings

    problems = Settings(app_env="production", secret_key="dev-insecure-secret-change-me", public_base_url="http://x",
                        database_url="sqlite:///x.db", token_encryption_key=None, ip_hash_salt="dev-ip-salt",
                        session_cookie_secure=False, mls_provider="mock").validate_for_production()
    joined = " ".join(problems)
    for needle in ("SECRET_KEY", "TOKEN_ENCRYPTION_KEY", "IP_HASH_SALT", "PostgreSQL", "SESSION_COOKIE_SECURE",
                   "https", "MLS_PROVIDER=mock"):
        assert needle in joined


def test_security_headers(client):
    resp = client.get("/listings")
    assert resp.headers["X-Frame-Options"] == "DENY"
    assert "default-src 'self'" in resp.headers["Content-Security-Policy"]
    assert resp.headers["X-Content-Type-Options"] == "nosniff"


def test_passwords_are_hashed(db, make_user):
    from crosspost.models import User
    from crosspost.security import verify_password

    make_user("admin")
    user = db.scalar(select(User))
    assert PASSWORD not in user.password_hash and user.password_hash.startswith("scrypt$")
    assert verify_password(PASSWORD, user.password_hash) and not verify_password("nope", user.password_hash)


def test_no_secrets_in_repository():
    """Guard against committed credentials in config examples and docs."""
    from pathlib import Path

    root = Path(__file__).resolve().parents[1]
    example = (root / ".env.example").read_text()
    for line in example.splitlines():
        if "=" in line and not line.startswith("#"):
            key, _, value = line.partition("=")
            value = value.split("#")[0].strip()
            if any(s in key for s in ("SECRET", "TOKEN", "PASSWORD", "API_KEY", "SALT")):
                assert value.strip() in ("", "change-me") or value.startswith("<"), key


# --- privacy requests and retention ----------------------------------------------------------
def test_admin_can_erase_buyer_data(db, listing, client, login):
    from crosspost.models import AuditLog, Contact

    submit(client, listing)
    lead = db.scalar(select(Lead))
    csrf = login("marketer")
    assert client.post(f"/admin/leads/{lead.id}/erase", data={"csrf_token": csrf, "confirm": "ERASE"}).status_code == 403
    client.post("/logout", data={"csrf_token": csrf})
    csrf = login("admin")
    client.post(f"/admin/leads/{lead.id}/erase", data={"csrf_token": csrf, "confirm": "nope"})
    assert db.scalar(select(func.count()).select_from(Lead)) == 1
    resp = client.post(f"/admin/leads/{lead.id}/erase", data={"csrf_token": csrf, "confirm": "ERASE"})
    assert resp.status_code == 303
    db.expire_all()
    for model in (Lead, Inquiry, Contact):
        assert db.scalar(select(func.count()).select_from(model)) == 0
    entry = db.scalar(select(AuditLog).where(AuditLog.action == "contact_erased"))
    assert entry and "sam@example.org" not in str(entry.details)


def test_retention_purge(db, listing, client):
    from datetime import timedelta

    from crosspost.db import utcnow
    from crosspost.models import Contact
    from crosspost.services.leads import purge_expired_leads

    submit(client, listing)
    submit(client, listing, email="recent@example.org")
    old = db.scalar(select(Inquiry).join(Lead).join(Contact).where(Contact.email == "sam@example.org"))
    old.submitted_at = utcnow() - timedelta(days=400)
    db.commit()
    assert purge_expired_leads(db, 0) == 0
    assert purge_expired_leads(db, 365) == 1
    db.commit()
    assert [c.email for c in db.scalars(select(Contact))] == ["recent@example.org"]
    assert db.scalar(select(func.count()).select_from(Lead)) == 1


# --- hosting compatibility ---------------------------------------------------------------
@pytest.mark.parametrize("url,expected", [
    ("postgres://u:p@h:5432/db", "postgresql+psycopg://u:p@h:5432/db"),
    ("postgresql://u:p@h/db", "postgresql+psycopg://u:p@h/db"),
    ("postgresql+psycopg://u:p@h/db", "postgresql+psycopg://u:p@h/db"),
    ("sqlite:///x.db", "sqlite:///x.db"),
])
def test_platform_database_urls_use_psycopg3(url, expected):
    from crosspost.config import Settings

    assert Settings(database_url=url).database_url == expected


def test_any_random_secret_works_as_token_encryption_key(monkeypatch):
    from cryptography.fernet import Fernet

    from crosspost.config import reset_settings_cache
    from crosspost.security import decrypt_secret, encrypt_secret

    for key in ("Zm9vYmFyYmF6cXV4cXV1eHF1dXhxdXV4cXV1eHF1dXg=-not-fernet-but-random", Fernet.generate_key().decode()):
        monkeypatch.setenv("TOKEN_ENCRYPTION_KEY", key)
        reset_settings_cache()
        token = encrypt_secret("page-token")
        assert token and "page-token" not in token and decrypt_secret(token) == "page-token"


def test_worker_refuses_insecure_production_config(monkeypatch):
    from crosspost import cli
    from crosspost.config import reset_settings_cache

    monkeypatch.setenv("APP_ENV", "production")
    reset_settings_cache()
    with pytest.raises(SystemExit) as info:
        cli.cmd_worker(None)
    assert "Refusing to start in production" in str(info.value)
