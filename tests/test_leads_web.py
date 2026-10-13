"""Acceptance test 6 (lead capture & attribution) and dashboard smoke tests."""
from __future__ import annotations

import re
from urllib.parse import parse_qs, urlparse

from sqlalchemy import func, select

from crosspost.jobs.queue import run_pending
from crosspost.models import Inquiry, Lead, LinkClick, Listing, PageVisit, Post, TrackingLink
from crosspost.services import notifications


def form_token(html: str) -> str:
    return re.search(r'name="form_token" value="([^"]+)"', html).group(1)


def hidden(html: str, name: str) -> str | None:
    m = re.search(rf'name="{name}" value="([^"]*)"', html)
    return m.group(1) if m else None


def published_listing(db, connect, sync):
    connect("test")
    sync()
    run_pending()
    listing = db.scalar(select(Listing).where(Listing.mls_id == "MOCK1001"))
    post = db.scalar(select(Post).where(Post.listing_id == listing.id, Post.status == "published"))
    link = db.scalar(select(TrackingLink).where(TrackingLink.post_id == post.id))
    return listing, post, link


def test_lead_capture_end_to_end(db, provider, connect, sync, client, login):
    listing, post, link = published_listing(db, connect, sync)

    # A buyer clicks the tracked link in the social post.
    resp = client.get(f"/t/{link.code}", headers={"User-Agent": "Mozilla/5.0 (iPhone)", "Referer": "https://m.facebook.com/"})
    assert resp.status_code == 302
    target = urlparse(resp.headers["location"])
    assert target.path == f"/p/{listing.slug}"
    qs = parse_qs(target.query)
    assert qs["tl"] == [link.code] and qs["utm_source"] == ["test"] and qs["utm_campaign"] == [link.utm_campaign]
    assert db.scalar(select(func.count()).select_from(LinkClick).where(LinkClick.is_bot.is_(False))) == 1

    # Landing page: mobile-friendly page with listing info, photos, attribution and the form.
    page = client.get(resp.headers["location"], headers={"User-Agent": "Mozilla/5.0 (iPhone)"})
    assert page.status_code == 200
    html = page.text
    assert 'name="viewport"' in html and "$525,000" in html and "1428 Juniper Hollow Dr" in html
    assert "Listing courtesy of Jordan Rivera, Example Realty Group" in html and "Equal Housing Opportunity" in html
    assert "/static/mock/home-1.svg" in html and 'property="og:image"' in html
    assert hidden(html, "tl") == link.code
    visit = db.scalar(select(PageVisit))
    assert visit.platform == "test" and visit.tracking_link_id == link.id and not visit.is_bot

    # The buyer submits an inquiry.
    data = {
        "form_token": form_token(html), "tl": link.code, "utm_source": "test", "utm_medium": "social",
        "utm_campaign": link.utm_campaign, "utm_content": link.utm_content, "website": "",
        "full_name": "Taylor Buyer", "email": "Taylor.Buyer@example.org", "phone": "512-555-0142",
        "preferred_contact_method": "phone", "message": "Is the garage finished? <b>thanks</b>",
    }
    resp = client.post(f"/p/{listing.slug}/inquire", data=data, headers={"Referer": resp.headers["location"]})
    assert resp.status_code == 303 and resp.headers["location"] == f"/p/{listing.slug}/thanks"

    lead = db.scalar(select(Lead))
    assert lead.listing_id == listing.id and lead.contact.email == "taylor.buyer@example.org"
    assert lead.source_platform == "test" and lead.campaign_id == link.campaign_id and lead.source_tracking_link_id == link.id
    assert lead.marketing_consent is False  # consent is never assumed
    inquiry = db.scalar(select(Inquiry))
    assert inquiry.utm_campaign == link.utm_campaign and inquiry.referrer_url and inquiry.submitted_at
    assert inquiry.ip_hash and "testclient" not in inquiry.ip_hash  # hashed, not raw

    # Re-submitting the same inquiry does not create duplicates.
    client.post(f"/p/{listing.slug}/inquire", data=data)
    assert db.scalar(select(func.count()).select_from(Inquiry)) == 1

    # The designated recipient is notified by the background job.
    run_pending()
    assert [m["To"] for m in notifications.OUTBOX] == ["leads@example.com"]
    assert "Taylor Buyer" in notifications.OUTBOX[0].get_content() and "MOCK1001" in notifications.OUTBOX[0]["Subject"]

    # The lead appears in the dashboard with attribution, and HTML is escaped.
    login("marketer")
    leads_page = client.get("/admin/leads")
    assert "Taylor Buyer" in leads_page.text and "mls-mock1001" in leads_page.text
    detail = client.get(f"/admin/leads/{lead.id}")
    assert "&lt;b&gt;thanks&lt;/b&gt;" in detail.text and "<b>thanks</b>" not in detail.text
    overview = client.get("/admin")
    assert "Taylor Buyer" in overview.text
    analytics = client.get("/admin/analytics")
    assert analytics.status_code == 200 and "not available" in analytics.text


def test_attribution_survives_via_cookie(db, provider, connect, sync, client):
    listing, post, link = published_listing(db, connect, sync)
    client.get(f"/p/{listing.slug}?utm_source=test&utm_medium=social&tl={link.code}")
    page = client.get(f"/p/{listing.slug}")  # buyer returns later via a clean URL
    assert hidden(page.text, "tl") == link.code


def test_direct_inquiry_without_tracking(db, provider, connect, sync, client):
    listing, _, _ = published_listing(db, connect, sync)
    page = client.get(f"/p/{listing.slug}")
    resp = client.post(f"/p/{listing.slug}/inquire", data={
        "form_token": form_token(page.text), "full_name": "Dana Direct", "email": "dana@example.org",
        "preferred_contact_method": "email"})
    assert resp.status_code == 303
    lead = db.scalar(select(Lead))
    assert lead.source_platform == "direct" and lead.campaign_id == listing.campaign.id


def test_unavailable_listing_page_does_not_advertise_availability(db, provider, connect, sync, client, feed):
    from .conftest import record

    listing, _, _ = published_listing(db, connect, sync)
    record(feed, "MOCK1001")["StandardStatus"] = "Closed"
    sync()
    page = client.get(f"/p/{listing.slug}")
    assert page.status_code == 200
    assert "no longer available (sold)" in page.text
    assert "$525,000" not in page.text and "Request information or a showing" not in page.text
    assert client.get("/listings").text.count(listing.slug) == 0


def test_link_in_bio_index_lists_marketed_listings(db, provider, connect, sync, client):
    published_listing(db, connect, sync)
    html = client.get("/listings").text
    assert "1428 Juniper Hollow Dr" in html and "Lamar" not in html  # other brokerage excluded


def test_dashboard_pages_render(db, provider, connect, sync, client, login):
    published_listing(db, connect, sync)
    login("admin")
    listing = db.scalar(select(Listing).where(Listing.mls_id == "MOCK1001"))
    post = db.scalar(select(Post))
    for path in ["/admin", "/admin/listings", "/admin/listings?q=Austin&status=Active&marketing=authorized",
                 f"/admin/listings/{listing.id}", "/admin/queue", "/admin/queue?group=published", f"/admin/posts/{post.id}",
                 "/admin/accounts", "/admin/leads", "/admin/analytics", "/admin/settings", "/admin/users", "/admin/jobs"]:
        resp = client.get(path)
        assert resp.status_code == 200, (path, resp.text[:500])


def test_manual_approval_flow_from_dashboard(db, provider, connect, sync, client, login):
    from crosspost.services.rules import get_rules, save_rules

    rules = get_rules(db)
    rules.new_listing_mode = "manual_approval"
    save_rules(db, rules)
    db.commit()
    connect("test")
    connect("manual", "Neighborhood group")
    sync(mls_id="MOCK1001")
    posts = {p.platform: p for p in db.scalars(select(Post))}
    assert all(p.status == "pending_approval" for p in posts.values())

    csrf = login("marketer")
    queue = client.get("/admin/queue")
    assert "NEW LISTING" in queue.text
    assert client.post(f"/admin/posts/{posts['test'].id}/approve", data={"csrf_token": csrf}).status_code == 303
    assert client.post(f"/admin/posts/{posts['manual'].id}/approve", data={"csrf_token": csrf}).status_code == 303
    db.expire_all()
    assert db.get(Post, posts["test"].id).status == "queued"
    assert db.get(Post, posts["manual"].id).status == "manual_ready"  # alternative workflow for non-API destinations
    run_pending()
    db.expire_all()
    assert db.get(Post, posts["test"].id).status == "published"
    resp = client.post(f"/admin/posts/{posts['manual'].id}/mark-published",
                       data={"csrf_token": csrf, "url": "https://nextdoor.example/p/1"})
    assert resp.status_code == 303
    db.expire_all()
    assert db.get(Post, posts["manual"].id).status == "published"


def test_reject_and_edit_post(db, provider, connect, sync, client, login):
    from crosspost.services.rules import get_rules, save_rules

    rules = get_rules(db)
    rules.new_listing_mode = "manual_approval"
    save_rules(db, rules)
    db.commit()
    connect("test")
    sync(mls_id="MOCK1001")
    post = db.scalar(select(Post))
    csrf = login("marketer")
    # Edits must keep the tracked link.
    client.post(f"/admin/posts/{post.id}/edit", data={"csrf_token": csrf, "text": "No link here"})
    db.expire_all()
    assert db.get(Post, post.id).text != "No link here"
    new_text = post.text.replace("NEW LISTING", "JUST LISTED")
    client.post(f"/admin/posts/{post.id}/edit", data={"csrf_token": csrf, "text": new_text})
    db.expire_all()
    assert db.get(Post, post.id).text.startswith("JUST LISTED")
    client.post(f"/admin/posts/{post.id}/reject", data={"csrf_token": csrf, "reason": "Wrong photo"})
    db.expire_all()
    assert db.get(Post, post.id).status == "rejected"


def test_select_platforms_and_trigger_sync_from_listing_page(db, provider, connect, sync, client, login):
    a = connect("test")
    b = connect("test", "sandbox-2")
    sync()
    listing = db.scalar(select(Listing).where(Listing.mls_id == "MOCK1001"))
    csrf = login("marketer")
    resp = client.post(f"/admin/listings/{listing.id}/publish", data={"csrf_token": csrf, "account_ids": [str(b)]})
    assert resp.status_code == 303
    reposts = list(db.scalars(select(Post).where(Post.listing_id == listing.id, Post.kind == "repost")))
    assert [p.social_account_id for p in reposts] == [b]
    resp = client.post(f"/admin/listings/{listing.id}/sync", data={"csrf_token": csrf})
    assert resp.status_code == 303
    assert run_pending() >= 1


def test_csv_export(db, provider, connect, sync, client, login):
    listing, _, _ = published_listing(db, connect, sync)
    page = client.get(f"/p/{listing.slug}")
    client.post(f"/p/{listing.slug}/inquire", data={
        "form_token": form_token(page.text), "full_name": "Eve Export", "email": "eve@example.org",
        "phone": "+1 512 555 0100", "preferred_contact_method": "email", "message": "=HYPERLINK(\"x\")"})
    login("marketer")
    resp = client.get("/admin/leads/export.csv")
    assert resp.status_code == 200 and resp.headers["content-type"].startswith("text/csv")
    assert "Eve Export" in resp.text and "'+1 512 555 0100" in resp.text  # formula-injection safe
