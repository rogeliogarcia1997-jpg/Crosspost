"""MLS mapping, RESO provider, content generation, compliance, queue, CRM and migrations."""
from __future__ import annotations

import json
from datetime import timedelta
from decimal import Decimal

import httpx
import pytest
from sqlalchemy import select

from crosspost.mls.base import MLSAuthError, MLSRateLimitError, MLSTransientError
from crosspost.mls.mapping import normalize_status, reso_to_normalized
from crosspost.mls.reso import RESOWebAPIProvider

from .conftest import FEED, record


# --- mapping ---------------------------------------------------------------------------------
@pytest.mark.parametrize("raw,expected", [("Active", "Active"), ("A", "Active"), ("Sold", "Closed"), ("contingent", "Active Under Contract"),
                                          ("Cancelled", "Canceled"), ("weird", "Unknown"), (None, "Unknown")])
def test_status_normalization(raw, expected):
    assert normalize_status(raw) == expected


def test_reso_mapping_fields_and_media_permissions():
    nl = reso_to_normalized(record(FEED, "MOCK1002"))
    assert nl.mls_id == "MOCK1002" and nl.unit_number == "4B" and nl.bathrooms_total == Decimal("2.5")
    assert [p.authorized for p in nl.photos] == [True, False]  # "Private" media is not redistributed
    assert "Pool" in nl.features and "1-car garage" in nl.features
    assert nl.open_houses[0].key == "OH-1002-1" and nl.open_houses[0].start.tzinfo is None
    nl3 = reso_to_normalized(record(FEED, "MOCK1003"))
    assert nl3.address_display_allowed is False and nl3.lot_size_sqft == 52272 and "Private pool" in nl3.features


def test_content_hash_ignores_timestamp_only_changes():
    a = reso_to_normalized(record(FEED, "MOCK1001"))
    b = reso_to_normalized({**record(FEED, "MOCK1001"), "ModificationTimestamp": "2026-10-09T00:00:00Z"})
    c = reso_to_normalized({**record(FEED, "MOCK1001"), "ListPrice": 1})
    assert a.content_hash() == b.content_hash() != c.content_hash()


# --- RESO provider ---------------------------------------------------------------------------------
def reso(handler, **kw):
    client = httpx.Client(transport=httpx.MockTransport(handler))
    defaults = dict(token_url="https://auth.example/token", client_id="id", client_secret="sec")
    defaults.update(kw)
    return RESOWebAPIProvider("https://api.example/odata", client=client, **defaults)


def test_reso_pagination_filter_and_token():
    calls = []

    def handler(request: httpx.Request):
        calls.append(request)
        if "token" in str(request.url):
            return httpx.Response(200, json={"access_token": "T1", "expires_in": 3600})
        if "skip" in str(request.url):
            return httpx.Response(200, json={"value": [record(FEED, "MOCK1002")]})
        return httpx.Response(200, json={"value": [record(FEED, "MOCK1001"), {"bad": "record"}],
                                         "@odata.nextLink": "https://api.example/odata/Property?$skip=2"})

    from datetime import datetime

    result = reso(handler).fetch_listings(modified_since=datetime(2026, 10, 1, 12, 0))
    assert [l.mls_id for l in result.listings] == ["MOCK1001", "MOCK1002"] and result.skipped_records == 1
    assert result.is_full_snapshot is False
    first = calls[1]
    assert first.headers["Authorization"] == "Bearer T1"
    assert "ModificationTimestamp gt 2026-10-01T11:59:00Z" in first.url.params["$filter"]
    assert first.url.params["$expand"] == "Media,OpenHouse"
    assert len([c for c in calls if "token" in str(c.url)]) == 1  # token cached


def test_reso_refreshes_token_once_on_401_then_fails():
    def handler(request):
        if "token" in str(request.url):
            return httpx.Response(200, json={"access_token": "T", "expires_in": 3600})
        return httpx.Response(401)

    with pytest.raises(MLSAuthError):
        reso(handler).fetch_listings()


@pytest.mark.parametrize("status,exc", [(429, MLSRateLimitError), (503, MLSTransientError)])
def test_reso_error_classification(status, exc):
    def handler(request):
        if "token" in str(request.url):
            return httpx.Response(200, json={"access_token": "T"})
        return httpx.Response(status, headers={"Retry-After": "30"})

    with pytest.raises(exc):
        reso(handler).fetch_listings()


def test_reso_requires_credentials():
    with pytest.raises(MLSAuthError):
        RESOWebAPIProvider("https://api.example/odata")


def test_reso_timeout_is_transient():
    def handler(request):
        raise httpx.ReadTimeout("slow", request=request)

    with pytest.raises(MLSTransientError):
        reso(handler, access_token="static", token_url=None, client_id=None, client_secret=None).fetch_listing("X")


# --- content -------------------------------------------------------------------------------------------
def make_listing(**overrides):
    from crosspost.models import Listing

    nl = reso_to_normalized({**record(FEED, "MOCK1001"), **overrides})
    listing = Listing(provider="mock", mls_id=nl.mls_id, slug="s", status=nl.status, content_hash=nl.content_hash())
    from crosspost.services.sync import _apply

    _apply(listing, nl)
    return listing


def test_x_post_fits_even_with_long_data():
    from crosspost.services.content import render_post
    from crosspost.social.registry import get_spec

    listing = make_listing(City="Llanfairpwllgwyngyllgogerychwyrndrobwllllantysiliogogogoch", ListOfficeName="A" * 60)
    spec = get_spec("x")
    post = render_post(listing, spec, "new_listing", "https://homes.example.com/t/abcdefgh")
    assert spec.measure(post.text) <= 280
    assert "Listing courtesy of " + "A" * 60 in post.text and "https://homes.example.com/t/abcdefgh" in post.text


def test_content_never_invents_missing_fields():
    from crosspost.services.content import render_post
    from crosspost.social.registry import get_spec

    listing = make_listing(BedroomsTotal=None, BathroomsTotalInteger=None, BathroomsFull=None, LivingArea=None,
                           InteriorFeatures=None, PatioAndPorchFeatures=None, GarageSpaces=None, LotSizeAcres=None,
                           YearBuilt=None, PublicRemarks=None)
    text = render_post(listing, get_spec("facebook"), "new_listing", "https://l/t/1").text
    assert "bedroom" not in text and "bathroom" not in text and "square feet" not in text
    assert "Property highlights" not in text
    assert "Priced at $525,000, this single-family home is located in Austin, TX." in text


def test_missing_attribution_blocks_generation(monkeypatch):
    from crosspost.config import reset_settings_cache
    from crosspost.services.content import ContentError, render_post
    from crosspost.social.registry import get_spec

    monkeypatch.setenv("BROKERAGE_NAME", "")
    reset_settings_cache()
    listing = make_listing(ListOfficeName=None)
    with pytest.raises(ContentError):
        render_post(listing, get_spec("facebook"), "new_listing", "https://l/t/1")


def test_generated_copy_passes_fair_housing_screen():
    from crosspost.services.compliance import fair_housing_screen
    from crosspost.services.content import render_post
    from crosspost.social.registry import all_specs

    listing = make_listing()
    for spec in all_specs():
        for kind in ("new_listing", "price_reduction", "repost", "back_on_market"):
            post = render_post(listing, spec, kind, "https://l/t/1", {"old_price": "550000"})
            assert not fair_housing_screen(post.text).flagged, (spec.platform, kind)
            assert spec.measure(post.text) <= spec.max_text


@pytest.mark.parametrize("text,flagged", [
    ("Perfect for a young family", True), ("No children allowed", True), ("Walking distance to church", True),
    ("Safe neighborhood", True), ("Adults only", True), ("No Section 8", True), ("Exclusive community", True),
    ("Family room with fireplace", False), ("Black appliances", False), ("Walking distance to parks", False),
    ("Great for entertaining", False),
])
def test_fair_housing_screen(text, flagged):
    from crosspost.services.compliance import fair_housing_screen

    assert fair_housing_screen(text).flagged is flagged


# --- job queue ---------------------------------------------------------------------------------------------
def test_queue_idempotency_and_stale_lock_recovery(db):
    from crosspost.db import utcnow
    from crosspost.jobs.queue import claim_next, enqueue, recover_stale_jobs
    from crosspost.models import Job

    a = enqueue(db, "cleanup", {}, idempotency_key="k1")
    b = enqueue(db, "cleanup", {}, idempotency_key="k1")
    db.commit()
    assert a.id == b.id
    job = claim_next(db, "w1")
    assert job.status == "running" and claim_next(db, "w2") is None
    assert recover_stale_jobs(db, now=utcnow() + timedelta(hours=1)) == 1
    assert db.get(Job, job.id).status == "queued"


def test_unknown_job_type_is_dead_lettered(db):
    from crosspost.jobs.queue import enqueue, run_pending
    from crosspost.models import Job

    enqueue(db, "nope", {})
    db.commit()
    run_pending()
    assert db.scalar(select(Job)).status == "dead"


def test_periodic_scheduling_is_idempotent(db):
    from crosspost.db import utcnow
    from crosspost.jobs.scheduler import schedule_periodic_jobs
    from crosspost.models import Job

    now = utcnow()
    schedule_periodic_jobs(db, now)
    schedule_periodic_jobs(db, now)
    db.commit()
    types = [j.type for j in db.scalars(select(Job))]
    assert sorted(types) == sorted(set(types)) and "mls_sync" in types


# --- CRM ----------------------------------------------------------------------------------------------------
def test_crm_push_via_webhook(db, provider, connect, sync, client, monkeypatch):
    import hashlib
    import hmac
    import re

    from crosspost.config import reset_settings_cache
    from crosspost.crm.clients import WebhookCRM, set_crm_override
    from crosspost.jobs.queue import run_pending
    from crosspost.models import CRMSync, Listing

    monkeypatch.setenv("CRM_PROVIDER", "webhook")
    monkeypatch.setenv("CRM_WEBHOOK_URL", "https://crm.example/hook")
    monkeypatch.setenv("CRM_WEBHOOK_SECRET", "crm-secret")
    reset_settings_cache()
    calls = []
    responses = iter([httpx.Response(503), httpx.Response(200, json={"id": "crm-1"})])

    def handler(request):
        calls.append(request)
        return next(responses)

    set_crm_override(WebhookCRM(httpx.Client(transport=httpx.MockTransport(handler))))
    connect("test")
    sync(mls_id="MOCK1001")
    listing = db.scalar(select(Listing))
    page = client.get(f"/p/{listing.slug}")
    token = re.search(r'name="form_token" value="([^"]+)"', page.text).group(1)
    client.post(f"/p/{listing.slug}/inquire", data={"form_token": token, "full_name": "Chris CRM", "email": "chris@example.org",
                                                     "preferred_contact_method": "email"})
    from crosspost.db import utcnow

    run_pending()
    run_pending(now=utcnow() + timedelta(days=1))
    db.expire_all()
    sync_row = db.scalar(select(CRMSync))
    assert sync_row.status == "sent" and sync_row.external_id == "crm-1" and len(calls) == 2
    payload = json.loads(calls[-1].content)
    assert payload["contact"]["email"] == "chris@example.org" and payload["listing"]["mls_id"] == "MOCK1001"
    assert calls[-1].headers["X-Crosspost-Signature"] == "sha256=" + hmac.new(b"crm-secret", calls[-1].content, hashlib.sha256).hexdigest()


# --- migrations -------------------------------------------------------------------------------------------------
def test_migrations_match_models(tmp_path, monkeypatch):
    from alembic import command
    from alembic.config import Config
    from alembic.util.exc import AutogenerateDiffsDetected

    from crosspost.config import reset_settings_cache

    monkeypatch.setenv("DATABASE_URL", f"sqlite:///{tmp_path}/mig.db")
    reset_settings_cache()
    cfg = Config("alembic.ini")
    command.upgrade(cfg, "head")
    try:
        command.check(cfg)
    except AutogenerateDiffsDetected as exc:  # pragma: no cover - failure path
        pytest.fail(f"models and migrations differ: {exc}")
    command.downgrade(cfg, "base")
