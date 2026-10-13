from __future__ import annotations

import copy
import json
import os
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
FEED = json.loads((ROOT / "data" / "mock_mls_feed.json").read_text())["value"]


@pytest.fixture(autouse=True)
def app_env(tmp_path, monkeypatch):
    env = {
        "APP_ENV": "test",
        "DATABASE_URL": f"sqlite:///{tmp_path}/test.db",
        "SECRET_KEY": "test-secret-key-that-is-long-enough-1234567890",
        "PUBLIC_BASE_URL": "https://homes.example.com",
        "BROKERAGE_OFFICE_IDS": "OFF100",
        "BROKERAGE_NAME": "Example Realty Group",
        "ENABLED_PLATFORMS": "test,facebook,x,instagram,linkedin,pinterest,threads,webhook,manual",
        "PUBLISH_MODE_NEW_LISTING": "immediate",
        "LEAD_FORM_MIN_SECONDS": "0",
        "LEAD_NOTIFICATION_EMAILS": "leads@example.com",
        "EMAIL_BACKEND": "console",
        "MLS_PROVIDER": "mock",
        "JOB_BACKOFF_BASE_SECONDS": "30",
        "JOB_MAX_ATTEMPTS": "4",
        "LOG_LEVEL": "WARNING",
        "META_GRAPH_VERSION": "v21.0",
    }
    for k, v in env.items():
        monkeypatch.setenv(k, v)
    from crosspost.config import reset_settings_cache

    reset_settings_cache()
    from crosspost import db as dbmod
    from crosspost.db import Base
    from crosspost import models  # noqa: F401

    engine = dbmod.init_engine(env["DATABASE_URL"])
    Base.metadata.create_all(engine)

    from crosspost.social import test_adapter
    from crosspost.services import notifications
    from crosspost.mls.registry import set_provider_override
    from crosspost.social.registry import set_http_client_override
    from crosspost.crm.clients import set_crm_override

    test_adapter.reset()
    notifications.OUTBOX.clear()
    yield
    set_provider_override(None)
    set_http_client_override(None)
    set_crm_override(None)
    engine.dispose()
    reset_settings_cache()


@pytest.fixture
def db():
    from crosspost.db import get_sessionmaker

    session = get_sessionmaker()()
    yield session
    session.close()


@pytest.fixture
def feed():
    return copy.deepcopy(FEED)


@pytest.fixture
def provider(feed):
    from crosspost.mls.mock import MockMLSProvider
    from crosspost.mls.registry import set_provider_override

    p = MockMLSProvider(records=feed, base_url="https://homes.example.com")
    set_provider_override(p)
    return p


def record(feed, mls_id):
    return next(r for r in feed if r["ListingId"] == mls_id)


@pytest.fixture
def connect():
    """Connect a social account: connect(platform, external_id, token='tok', **settings)."""
    from crosspost.db import session_scope
    from crosspost.services.accounts import connect_account
    from crosspost.social.base import TokenSet

    def _connect(platform="test", external_id="sandbox", token="tok", **settings):
        with session_scope() as s:
            acct = connect_account(s, platform, external_id, f"{platform} {external_id}",
                                   TokenSet(token) if token else None, settings or None)
            return acct.id

    return _connect


@pytest.fixture
def sync(provider):
    from crosspost.db import session_scope
    from crosspost.services.sync import run_sync

    def _sync(**kw):
        with session_scope() as s:
            run = run_sync(s, provider, **kw)
            return run.id

    return _sync


@pytest.fixture
def client():
    from fastapi.testclient import TestClient

    from crosspost.web.app import create_app

    with TestClient(create_app(), base_url="https://homes.example.com", follow_redirects=False) as c:
        yield c


PASSWORD = "Correct-Horse-Battery-9"


@pytest.fixture
def make_user():
    from crosspost.db import session_scope
    from crosspost.models import User
    from crosspost.security import hash_password

    def _make(role="admin", email=None):
        email = email or f"{role}@example.com"
        with session_scope() as s:
            s.add(User(email=email, password_hash=hash_password(PASSWORD), role=role))
        return email

    return _make


def csrf_from(html: str) -> str:
    import re

    match = re.search(r'name="csrf_token" value="([^"]+)"', html)
    assert match, "csrf token not found"
    return match.group(1)


@pytest.fixture
def login(client, make_user):
    def _login(role="admin"):
        email = make_user(role)
        page = client.get("/login")
        resp = client.post("/login", data={"email": email, "password": PASSWORD, "csrf_token": csrf_from(page.text),
                                           "next": "/admin"})
        assert resp.status_code == 303, resp.text
        return csrf_from(client.get("/admin").text)

    return _login
