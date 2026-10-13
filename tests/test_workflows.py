"""Acceptance tests 1-5, 7 and 8 from the project brief."""
from __future__ import annotations

from datetime import timedelta

from sqlalchemy import func, select

from crosspost.db import utcnow
from crosspost.jobs.queue import run_pending
from crosspost.models import Campaign, Job, Listing, ListingChange, Post, PublishAttempt, SocialAccount, SyncRun, TrackingLink
from crosspost.social import test_adapter

from .conftest import record


def count(db, model, *where):
    return db.scalar(select(func.count()).select_from(model).where(*where))


def far_future():
    return utcnow() + timedelta(days=1)


# --- Test 1: new listing -----------------------------------------------------
def test_new_listing_detected_content_generated_and_published(db, provider, connect, sync):
    connect("test")
    sync()
    listing = db.scalar(select(Listing).where(Listing.mls_id == "MOCK1001"))
    assert listing is not None and listing.status == "Active"
    assert listing.price == 525000
    assert [c.change_type for c in listing.changes] == ["new"]

    campaign = db.scalar(select(Campaign).where(Campaign.listing_id == listing.id))
    assert campaign is not None and campaign.status == "active"
    post = db.scalar(select(Post).where(Post.listing_id == listing.id, Post.kind == "new_listing"))
    assert post.status == "queued"
    # Content is built from actual MLS data only.
    assert "NEW LISTING | Discover your next home in Austin, TX!" in post.text
    assert "Priced at $525,000" in post.text and "3 bedrooms, 2 bathrooms, and 1,850 square feet" in post.text
    assert "• Open Floorplan" in post.text
    assert "Listing courtesy of Jordan Rivera, Example Realty Group." in post.text
    # Trackable link with campaign attribution.
    link = db.scalar(select(TrackingLink).where(TrackingLink.post_id == post.id))
    assert link and post.link_url == f"https://homes.example.com/t/{link.code}" and link.utm_campaign == campaign.code
    assert link.platform == "test" and post.link_url in post.text
    assert post.media and post.media[0]["url"].startswith("https://homes.example.com/static/mock/")

    run_pending()
    db.expire_all()
    post = db.get(Post, post.id)
    assert post.status == "published" and post.external_post_id.startswith("test-")
    assert any(p["idempotency_key"] == f"crosspost-post-{post.id}" for p in test_adapter.PUBLISHED)


def test_unauthorized_and_unavailable_listings_are_not_marketed(db, provider, connect, sync):
    connect("test")
    sync()
    other = db.scalar(select(Listing).where(Listing.mls_id == "MOCK2001"))  # other brokerage
    pending = db.scalar(select(Listing).where(Listing.mls_id == "MOCK1004"))
    assert other and pending
    assert count(db, Post, Post.listing_id == other.id) == 0
    assert count(db, Post, Post.listing_id == pending.id) == 0


def test_fair_housing_flagged_remarks_are_excluded_and_routed_to_review(db, provider, connect, sync):
    connect("test")
    sync()
    listing = db.scalar(select(Listing).where(Listing.mls_id == "MOCK1003"))
    post = db.scalar(select(Post).where(Post.listing_id == listing.id, Post.kind == "new_listing"))
    assert post.status == "pending_approval" and post.requires_approval
    assert "young family" not in post.text and "church" not in post.text
    assert any("fair housing" in w for w in post.warnings)
    # Address display opted out: no street address anywhere in the post.
    assert "Cedar Bend" not in post.text and "Georgetown, TX" in post.text


# --- Test 2: multiple platforms ----------------------------------------------
def test_platform_specific_versions(db, provider, connect, sync):
    connect("test")
    connect("facebook", "page-1")
    connect("x", "x-user-1")
    connect("instagram", "ig-1")
    connect("pinterest", "board-1")
    sync()
    listing = db.scalar(select(Listing).where(Listing.mls_id == "MOCK1001"))
    posts = {p.platform: p for p in db.scalars(select(Post).where(Post.listing_id == listing.id, Post.kind == "new_listing"))}
    assert set(posts) == {"test", "facebook", "x", "instagram", "pinterest"}

    from crosspost.social.registry import get_spec

    x, fb, ig, pin = posts["x"], posts["facebook"], posts["instagram"], posts["pinterest"]
    assert get_spec("x").measure(x.text) <= 280
    assert "Property highlights" not in x.text and len(x.media) <= 4
    assert "Property highlights" in fb.text and "View the property and request more information" in fb.text
    assert "link in our bio" in ig.text and len(ig.text) <= 2200 and len(ig.media) >= 1
    assert pin.title and len(pin.title) <= 100 and len(pin.text) <= 500 and len(pin.media) == 1
    # Each platform gets its own tracked link.
    assert len({p.link_url for p in posts.values()}) == len(posts)
    assert all(p.text != x.text for p in posts.values() if p is not x)
    for p in posts.values():
        assert "Example Realty Group" in p.text


def test_instagram_skips_listing_without_authorized_photos(db, provider, connect, sync, feed):
    for m in record(feed, "MOCK1001")["Media"]:
        m["Permission"] = ["Private"]
    connect("instagram", "ig-1")
    connect("test")
    sync()
    listing = db.scalar(select(Listing).where(Listing.mls_id == "MOCK1001"))
    ig = db.scalar(select(Post).where(Post.listing_id == listing.id, Post.platform == "instagram"))
    test_post = db.scalar(select(Post).where(Post.listing_id == listing.id, Post.platform == "test"))
    assert ig.status == "skipped" and "requires at least 1 photo" in ig.last_error
    assert test_post.status == "queued" and test_post.media == []


# --- Test 3: duplicate prevention ---------------------------------------------
def test_repeated_sync_creates_no_duplicates(db, provider, connect, sync):
    connect("test")
    sync()
    sync()
    sync(mode="full")
    sync(mls_id="MOCK1001")
    assert count(db, Listing, Listing.mls_id == "MOCK1001") == 1
    listing = db.scalar(select(Listing).where(Listing.mls_id == "MOCK1001"))
    assert count(db, Campaign, Campaign.listing_id == listing.id) == 1
    assert count(db, Post, Post.listing_id == listing.id) == 1
    assert count(db, ListingChange, ListingChange.listing_id == listing.id) == 1
    run_pending()
    run_pending()
    post = db.scalar(select(Post).where(Post.listing_id == listing.id))
    assert len([p for p in test_adapter.PUBLISHED if p["idempotency_key"] == f"crosspost-post-{post.id}"]) == 1
    assert len(test_adapter.PUBLISHED) == count(db, Post, Post.status == "published")


def test_duplicate_publish_jobs_do_not_double_post(db, provider, connect, sync):
    from crosspost.jobs.queue import enqueue

    connect("test")
    sync()
    listing = db.scalar(select(Listing).where(Listing.mls_id == "MOCK1001"))
    post = db.scalar(select(Post).where(Post.listing_id == listing.id))
    enqueue(db, "publish_post", {"post_id": post.id})
    enqueue(db, "publish_post", {"post_id": post.id})
    db.commit()
    run_pending()
    assert len([p for p in test_adapter.PUBLISHED if p["idempotency_key"] == f"crosspost-post-{post.id}"]) == 1
    assert count(db, PublishAttempt, PublishAttempt.post_id == post.id) == 1


# --- Test 4: price change --------------------------------------------------------
def test_price_reduction_triggers_configured_workflow(db, provider, connect, sync, feed):
    connect("test")
    sync()
    run_pending()
    rec = record(feed, "MOCK1001")
    rec["ListPrice"] = 499000
    rec["ModificationTimestamp"] = "2026-10-08T15:00:00Z"
    sync()
    listing = db.scalar(select(Listing).where(Listing.mls_id == "MOCK1001"))
    assert listing.price == 499000
    change = db.scalar(select(ListingChange).where(ListingChange.listing_id == listing.id, ListingChange.change_type == "price_change"))
    assert change.old_value == "525000.00" or float(change.old_value) == 525000
    post = db.scalar(select(Post).where(Post.listing_id == listing.id, Post.kind == "price_reduction"))
    assert post is not None and post.event_key == "price:499000"
    assert "PRICE IMPROVEMENT | Now $499,000" in post.text and "previously $525,000" in post.text
    # Same price seen again: no second announcement.
    sync()
    assert count(db, Post, Post.listing_id == listing.id, Post.kind == "price_reduction") == 1
    # A price increase is a change but not a "reduction" announcement.
    rec["ListPrice"] = 510000
    rec["ModificationTimestamp"] = "2026-10-09T15:00:00Z"
    sync()
    assert count(db, Post, Post.listing_id == listing.id, Post.kind == "price_reduction") == 1


def test_price_reduction_announcements_can_be_disabled(db, provider, connect, sync, feed):
    from crosspost.services.rules import get_rules, save_rules

    connect("test")
    sync()
    rules = get_rules(db)
    rules.announce_price_reductions = False
    save_rules(db, rules)
    db.commit()
    rec = record(feed, "MOCK1001")
    rec["ListPrice"] = 480000
    sync()
    assert count(db, Post, Post.kind == "price_reduction") == 0


# --- Test 5: status change ------------------------------------------------------------
def test_status_change_stops_promotion(db, provider, connect, sync, feed):
    from crosspost.services.rules import get_rules, save_rules

    connect("test")
    rules = get_rules(db)
    rules.new_listing_mode = "scheduled"
    rules.delay_minutes = 120
    save_rules(db, rules)
    db.commit()
    sync()
    listing = db.scalar(select(Listing).where(Listing.mls_id == "MOCK1001"))
    post = db.scalar(select(Post).where(Post.listing_id == listing.id))
    assert post.status == "scheduled"

    for status in ("Pending",):
        rec = record(feed, "MOCK1001")
        rec["StandardStatus"] = status
        rec["ModificationTimestamp"] = "2026-10-10T15:00:00Z"
    sync()
    db.expire_all()
    post = db.get(Post, post.id)
    assert post.status == "canceled" and "Pending" in post.last_error
    assert db.scalar(select(Campaign).where(Campaign.listing_id == listing.id)).status == "ended"

    # A price change while pending must not create a promotional post.
    rec["ListPrice"] = 450000
    sync()
    assert count(db, Post, Post.listing_id == listing.id, Post.kind == "price_reduction") == 0
    # Even if a stale job runs after the time passes, nothing is published.
    from crosspost.services.publishing import release_due_scheduled_posts

    release_due_scheduled_posts(db, now=far_future())
    db.commit()
    run_pending()
    assert not [p for p in test_adapter.PUBLISHED if p["idempotency_key"] == f"crosspost-post-{post.id}"]


def test_queued_post_is_canceled_at_publish_time_if_listing_became_unavailable(db, provider, connect, sync):
    connect("test")
    sync()
    listing = db.scalar(select(Listing).where(Listing.mls_id == "MOCK1001"))
    post = db.scalar(select(Post).where(Post.listing_id == listing.id))
    assert post.status == "queued"
    # Simulate the status change landing between queueing and publishing.
    listing.status = "Closed"
    db.commit()
    run_pending()
    db.expire_all()
    assert db.get(Post, post.id).status == "canceled"
    assert not [p for p in test_adapter.PUBLISHED if p["idempotency_key"] == f"crosspost-post-{post.id}"]


def test_listing_removed_from_feed_is_treated_as_unavailable(db, provider, connect, sync, feed):
    connect("test")
    sync()
    provider.set_records([r for r in feed if r["ListingId"] != "MOCK1001"])
    sync(mode="full")
    listing = db.scalar(select(Listing).where(Listing.mls_id == "MOCK1001"))
    assert listing.status == "Removed From Feed" and not listing.is_available
    assert db.scalar(select(Post).where(Post.listing_id == listing.id)).status == "canceled"


def test_empty_feed_does_not_mass_remove(db, provider, connect, sync):
    sync()
    provider.set_records([])
    sync(mode="full")
    assert count(db, Listing, Listing.status == "Active") == 4


def test_status_announcement_requires_approval_and_never_says_available(db, provider, connect, sync, feed):
    from crosspost.services.rules import get_rules, save_rules

    connect("test")
    rules = get_rules(db)
    rules.announce_status_changes = True
    save_rules(db, rules)
    db.commit()
    sync()
    run_pending()
    record(feed, "MOCK1001")["StandardStatus"] = "Closed"
    sync()
    post = db.scalar(select(Post).where(Post.kind == "status_update"))
    assert post.status == "pending_approval"
    assert post.text.startswith("SOLD |") and "is now sold" in post.text
    assert "showing" not in post.text.lower() and "for sale" not in post.text.lower()


def test_back_on_market(db, provider, connect, sync, feed):
    connect("test")
    sync()
    run_pending()
    rec = record(feed, "MOCK1001")
    rec["StandardStatus"] = "Pending"
    sync()
    rec["StandardStatus"] = "Active"
    sync()
    listing = db.scalar(select(Listing).where(Listing.mls_id == "MOCK1001"))
    assert listing.campaign.status == "active"
    post = db.scalar(select(Post).where(Post.listing_id == listing.id, Post.kind == "back_on_market"))
    assert post and post.text.startswith("BACK ON THE MARKET")


def test_open_house_scheduled_relative_to_event(db, provider, connect, sync):
    connect("test")
    sync()
    listing = db.scalar(select(Listing).where(Listing.mls_id == "MOCK1002"))
    oh = db.scalar(select(Post).where(Post.listing_id == listing.id, Post.kind == "open_house"))
    assert oh.status == "scheduled"
    assert oh.scheduled_for.isoformat().startswith("2026-12-17T19:00")  # 48h before start
    assert oh.text.startswith("OPEN HOUSE | Sat, Dec 19, 7:00 PM–9:00 PM")


# --- Test 7: API failure and retries --------------------------------------------
def test_rate_limit_is_retried_without_duplicate_posts(db, provider, connect, sync):
    connect("test")
    connect("test", "sandbox-2")
    test_adapter.FAILURES.extend(["rate_limit", "transient"])
    sync(mls_id="MOCK1001")
    posts = list(db.scalars(select(Post)))
    assert len(posts) == 2 and all(p.status == "queued" for p in posts)

    run_pending()  # first publish hits the rate limit, second a transient 503
    db.expire_all()
    assert sorted(p.status for p in db.scalars(select(Post))) == ["retry_scheduled", "retry_scheduled"]
    jobs = list(db.scalars(select(Job).where(Job.type == "publish_post")))
    assert all(j.status == "queued" and j.attempts == 1 for j in jobs)
    rate_limited = next(j for j in jobs if "rate limited" in (j.last_error or ""))
    assert rate_limited.run_at > utcnow() + timedelta(seconds=20)  # honours Retry-After
    assert test_adapter.PUBLISHED == []

    run_pending(now=far_future())
    db.expire_all()
    posts = list(db.scalars(select(Post)))
    assert all(p.status == "published" and p.attempts == 2 for p in posts)
    keys = [p["idempotency_key"] for p in test_adapter.PUBLISHED]
    assert sorted(keys) == sorted(f"crosspost-post-{p.id}" for p in posts)
    outcomes = sorted(a.outcome for a in db.scalars(select(PublishAttempt)))
    assert outcomes == ["rate_limited", "success", "success", "transient_error"]
    # Nothing left to run; no duplicates on further passes.
    assert run_pending(now=far_future()) == 0
    assert len(test_adapter.PUBLISHED) == 2


def test_permanent_failure_is_not_retried(db, provider, connect, sync):
    connect("test")
    test_adapter.FAILURES.append("permanent")
    sync()
    run_pending()
    db.expire_all()
    failed = list(db.scalars(select(Post).where(Post.status == "failed")))
    assert len(failed) == 1 and failed[0].attempts == 1 and failed[0].error_category == "permanent"


def test_retries_give_up_after_max_attempts(db, provider, connect, sync):
    connect("test", fail_mode="transient")
    sync(mls_id="MOCK1001")
    for _ in range(6):
        run_pending(now=far_future())
    db.expire_all()
    post = db.scalar(select(Post))
    assert post.status == "failed" and post.attempts == 4
    assert test_adapter.PUBLISHED == []


def test_uncertain_outcome_is_held_for_verification(db, provider, connect, sync):
    connect("test")
    test_adapter.FAILURES.append("timeout_uncertain")
    sync(mls_id="MOCK1001")
    run_pending()
    run_pending(now=far_future())
    db.expire_all()
    post = db.scalar(select(Post))
    assert post.status == "needs_verification" and post.attempts == 1


# --- Test 8: expired token ---------------------------------------------------------
def test_expired_token_blocks_without_repeated_retries(db, provider, connect, sync, feed):
    second = dict(record(feed, "MOCK1001"), ListingId="MOCK1005", ListingKey="MOCK-K-1005")
    provider.set_records([record(feed, "MOCK1001"), second])
    acct_id = connect("test")
    test_adapter.FAILURES.append("auth_expired")
    sync()
    assert count(db, Post, Post.status == "queued") == 2
    run_pending()
    for _ in range(3):
        run_pending(now=far_future())
    db.expire_all()
    account = db.get(SocialAccount, acct_id)
    assert account.status == "reauth_required" and "expired" in account.last_error
    posts = list(db.scalars(select(Post)))
    assert len(posts) == 2 and all(p.status == "blocked_auth" for p in posts)
    # Only the first post reached the platform; the other was held without calling it.
    assert count(db, PublishAttempt) == 1
    assert count(db, Job, Job.type == "publish_post", Job.status == "queued") == 0
    assert any("re-authorization" in str(m["Subject"]) for m in __import__("crosspost.services.notifications", fromlist=["OUTBOX"]).OUTBOX)

    # Reconnecting releases the blocked posts, which then publish exactly once.
    connect("test", token="new-token")
    run_pending()
    db.expire_all()
    assert all(p.status == "published" for p in db.scalars(select(Post)))
    assert len(test_adapter.PUBLISHED) == 2


def test_expired_token_detected_by_health_check(db, provider, connect):
    from crosspost.services.accounts import check_account

    acct_id = connect("test", fail_mode="auth_expired")
    account = db.get(SocialAccount, acct_id)
    check_account(db, account)
    assert account.status == "reauth_required"
    account.token_expires_at = utcnow() - timedelta(minutes=1)
    account.status = "connected"
    check_account(db, account)
    assert account.status == "reauth_required" and account.last_error == "Access token expired"


# --- MLS failures -------------------------------------------------------------------
def test_mls_failures_are_recorded_and_classified(db, provider):
    from crosspost.jobs.queue import enqueue

    provider.fail_with = "auth"
    enqueue(db, "mls_sync", {"triggered_by": "manual"})
    db.commit()
    run_pending()
    run = db.scalar(select(SyncRun).order_by(SyncRun.id.desc()))
    assert run.status == "auth_error"
    assert db.scalar(select(Job).where(Job.type == "mls_sync")).status == "dead"  # not retried

    provider.fail_with = "rate_limit"
    enqueue(db, "mls_sync", {"triggered_by": "manual"}, idempotency_key="rl")
    db.commit()
    run_pending()
    job = db.scalar(select(Job).where(Job.idempotency_key == "rl"))
    assert job.status == "queued" and job.attempts == 1  # retried later

    provider.fail_with = None
    run_pending(now=far_future())
    db.expire_all()
    assert db.scalar(select(Job).where(Job.idempotency_key == "rl")).status == "succeeded"
    assert count(db, Listing) == 5


def test_scheduled_sync_backs_off_after_auth_failure(db, provider):
    from crosspost.jobs.queue import enqueue

    provider.fail_with = "auth"
    enqueue(db, "mls_sync", {"triggered_by": "manual"})
    db.commit()
    run_pending()
    enqueue(db, "mls_sync", {"triggered_by": "schedule"})
    db.commit()
    run_pending()
    assert count(db, SyncRun) == 1


def test_post_stuck_in_publishing_is_held_for_verification(db, provider, connect, sync):
    from crosspost.services.publishing import flag_stuck_publishing

    connect("test")
    sync(mls_id="MOCK1001")
    post = db.scalar(select(Post))
    post.status = "publishing"  # simulate a worker that died mid-request
    db.commit()
    assert flag_stuck_publishing(db) == 0  # still within the lock timeout
    assert flag_stuck_publishing(db, now=far_future()) == 1
    db.commit()
    run_pending(now=far_future())
    db.expire_all()
    assert db.get(Post, post.id).status == "needs_verification"
    assert test_adapter.PUBLISHED == []
