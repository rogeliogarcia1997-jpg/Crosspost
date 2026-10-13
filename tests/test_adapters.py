"""Platform adapters exercised against mocked HTTP (no live credentials needed)."""
from __future__ import annotations

import json
from urllib.parse import parse_qs

import httpx
import pytest
from sqlalchemy import select

from crosspost.jobs.queue import run_pending
from crosspost.models import Post, SocialAccount
from crosspost.social.base import (
    AccountContext,
    AuthExpiredError,
    MediaItem,
    PermanentPublishError,
    RateLimitError,
    RenderedPost,
    TransientPublishError,
)
from crosspost.social.registry import get_adapter, set_http_client_override

PHOTO = "https://homes.example.com/static/mock/home-1.svg"


class Recorder:
    def __init__(self, routes):
        self.routes = routes  # list of (method, substring, response or callable)
        self.calls: list[httpx.Request] = []

    def __call__(self, request: httpx.Request) -> httpx.Response:
        self.calls.append(request)
        for method, needle, resp in self.routes:
            if request.method == method and needle in str(request.url):
                return resp(request) if callable(resp) else resp
        return httpx.Response(404, json={"error": "unmocked " + str(request.url)})

    def client(self) -> httpx.Client:
        return httpx.Client(transport=httpx.MockTransport(self))


def post(n_photos=2, text="Hello #RealEstate https://homes.example.com/t/abc", platform="x"):
    return RenderedPost(platform=platform, kind="new_listing", text=text, link="https://homes.example.com/t/abc",
                        title="Title", media=[MediaItem(PHOTO, f"alt {i}") for i in range(n_photos)])


def ctx(platform, ext="acct-1", token="tok"):
    return AccountContext(platform=platform, external_account_id=ext, access_token=token)


def adapter(platform, rec):
    a = get_adapter(platform)
    a._client = rec.client()
    return a


def form(request):
    return {k: v[0] for k, v in parse_qs(request.content.decode()).items()}


# --- Facebook ---------------------------------------------------------------------------
def test_facebook_multi_photo_post():
    counter = iter(range(100))
    rec = Recorder([
        ("POST", "/page-1/photos", lambda r: httpx.Response(200, json={"id": f"ph{next(counter)}"})),
        ("POST", "/page-1/feed", httpx.Response(200, json={"id": "page-1_999"})),
    ])
    result = adapter("facebook", rec).publish(ctx("facebook", "page-1"), post(2), "k1")
    assert result.external_id == "page-1_999" and result.url == "https://www.facebook.com/page-1_999"
    photos = [form(c) for c in rec.calls if "/photos" in str(c.url)]
    assert [p["url"] for p in photos] == [PHOTO, PHOTO] and all(p["published"] == "false" for p in photos)
    feed = form(rec.calls[-1])
    assert feed["message"].startswith("Hello") and json.loads(feed["attached_media[1]"]) == {"media_fbid": "ph1"}
    assert "/v21.0/" in str(rec.calls[-1].url)


@pytest.mark.parametrize("status,body,exc", [
    (400, {"error": {"code": 190, "message": "Error validating access token"}}, AuthExpiredError),
    (403, {"error": {"code": 200, "message": "Permissions error"}}, AuthExpiredError),
    (400, {"error": {"code": 4, "message": "Application request limit reached"}}, RateLimitError),
    (500, {"error": {"code": 2, "message": "Service temporarily unavailable", "is_transient": True}}, TransientPublishError),
    (400, {"error": {"code": 100, "message": "Invalid parameter"}}, PermanentPublishError),
])
def test_facebook_error_mapping(status, body, exc):
    rec = Recorder([("POST", "/feed", httpx.Response(status, json=body))])
    with pytest.raises(exc):
        adapter("facebook", rec).publish(ctx("facebook", "page-1"), post(0), "k")


def test_post_timeout_marks_possible_publication():
    def boom(request):
        raise httpx.ReadTimeout("timed out", request=request)

    rec = Recorder([("POST", "/feed", boom)])
    with pytest.raises(TransientPublishError) as info:
        adapter("facebook", rec).publish(ctx("facebook", "page-1"), post(0), "k")
    assert info.value.may_have_published is True


def test_facebook_token_never_logged_in_errors():
    rec = Recorder([("POST", "/feed", httpx.Response(400, json={"error": {"code": 100, "message": "bad access_token=SECRET123"}}))])
    with pytest.raises(PermanentPublishError) as info:
        adapter("facebook", rec).publish(ctx("facebook", "page-1", token="SECRET123"), post(0), "k")
    assert "SECRET123" not in str(info.value)


# --- Instagram ------------------------------------------------------------------------------
def test_instagram_carousel_flow():
    ids = iter(["child1", "child2", "carousel"])
    rec = Recorder([
        ("POST", "/ig-1/media_publish", httpx.Response(200, json={"id": "media-1"})),
        ("POST", "/ig-1/media", lambda r: httpx.Response(200, json={"id": next(ids)})),
        ("GET", "/carousel", httpx.Response(200, json={"status_code": "FINISHED"})),
        ("GET", "/media-1", httpx.Response(200, json={"permalink": "https://www.instagram.com/p/xyz/"})),
    ])
    result = adapter("instagram", rec).publish(ctx("instagram", "ig-1"), post(2), "k")
    assert result.external_id == "media-1" and result.url == "https://www.instagram.com/p/xyz/"
    container = form([c for c in rec.calls if c.method == "POST"][2])
    assert container["media_type"] == "CAROUSEL" and container["children"] == "child1,child2"
    assert form(rec.calls[0])["is_carousel_item"] == "true"


def test_instagram_requires_photo():
    with pytest.raises(PermanentPublishError):
        adapter("instagram", Recorder([])).publish(ctx("instagram"), post(0), "k")


# --- X -------------------------------------------------------------------------------------------
def test_x_upload_and_tweet():
    rec = Recorder([
        ("GET", "/static/mock/", httpx.Response(200, content=b"<svg/>", headers={"content-type": "image/svg+xml"})),
        ("POST", "/2/media/upload", httpx.Response(200, json={"data": {"id": "777"}})),
        ("POST", "/2/tweets", httpx.Response(201, json={"data": {"id": "1850"}})),
    ])
    result = adapter("x", rec).publish(ctx("x"), post(1), "k")
    assert result.external_id == "1850" and result.url.endswith("/status/1850")
    body = json.loads(rec.calls[-1].content)
    assert body["media"] == {"media_ids": ["777"]} and body["text"].startswith("Hello")
    assert rec.calls[-1].headers["Authorization"] == "Bearer tok"


def test_x_rate_limit_uses_reset_header():
    import time

    rec = Recorder([("POST", "/2/tweets", httpx.Response(429, headers={"x-rate-limit-reset": str(int(time.time()) + 120)}))])
    with pytest.raises(RateLimitError) as info:
        adapter("x", rec).publish(ctx("x"), post(0), "k")
    assert 100 < info.value.retry_after <= 121


def test_x_unauthorized_is_auth_error():
    rec = Recorder([("POST", "/2/tweets", httpx.Response(401, json={"title": "Unauthorized"}))])
    with pytest.raises(AuthExpiredError):
        adapter("x", rec).publish(ctx("x"), post(0), "k")


# --- LinkedIn ------------------------------------------------------------------------------------
def test_linkedin_image_post_and_little_text():
    rec = Recorder([
        ("POST", "/rest/images", httpx.Response(200, json={"value": {"uploadUrl": "https://upload.example/u1", "image": "urn:li:image:1"}})),
        ("GET", "/static/mock/", httpx.Response(200, content=b"img")),
        ("PUT", "upload.example", httpx.Response(201)),
        ("POST", "/rest/posts", httpx.Response(201, headers={"x-restli-id": "urn:li:share:42"})),
    ])
    p = post(1, text="NEW LISTING | Austin (TX) #AustinHomes [3 bd]")
    result = adapter("linkedin", rec).publish(ctx("linkedin", "urn:li:organization:5"), p, "k")
    assert result.external_id == "urn:li:share:42"
    req = rec.calls[-1]
    body = json.loads(req.content)
    assert req.headers["LinkedIn-Version"] and req.headers["X-Restli-Protocol-Version"] == "2.0.0"
    assert body["author"] == "urn:li:organization:5" and body["content"]["media"]["id"] == "urn:li:image:1"
    assert body["commentary"] == r"NEW LISTING \| Austin \(TX\) {hashtag|\#|AustinHomes} \[3 bd\]"


def test_linkedin_forbidden_requires_reauth():
    rec = Recorder([("POST", "/rest/posts", httpx.Response(403, json={"message": "Not enough permissions"}))])
    with pytest.raises(AuthExpiredError):
        adapter("linkedin", rec).publish(ctx("linkedin", "urn:li:organization:5"), post(0), "k")


# --- Pinterest -------------------------------------------------------------------------------------
def test_pinterest_pin():
    rec = Recorder([("POST", "/v5/pins", httpx.Response(201, json={"id": "pin9"}))])
    result = adapter("pinterest", rec).publish(ctx("pinterest", "board-1"), post(1), "k")
    body = json.loads(rec.calls[0].content)
    assert result.url == "https://www.pinterest.com/pin/pin9/"
    assert body["board_id"] == "board-1" and body["link"] == "https://homes.example.com/t/abc"
    assert body["media_source"] == {"source_type": "image_url", "url": PHOTO} and body["title"] == "Title"


# --- Webhook ----------------------------------------------------------------------------------------
def test_webhook_signed_payload(monkeypatch):
    import hashlib
    import hmac

    rec = Recorder([("POST", "hooks.example", httpx.Response(200, json={"id": "z1", "url": "https://x/1"}))])
    a = adapter("webhook", rec)
    account = AccountContext("webhook", "zapier", "shh", settings={"url": "https://hooks.example/in"})
    result = a.publish(account, post(1), "idem-1")
    req = rec.calls[0]
    expected = "sha256=" + hmac.new(b"shh", req.content, hashlib.sha256).hexdigest()
    assert req.headers["X-Crosspost-Signature"] == expected and req.headers["Idempotency-Key"] == "idem-1"
    assert result.external_id == "z1"


# --- through the publishing engine -------------------------------------------------------------------
def test_engine_publishes_through_facebook_adapter(db, provider, connect, sync):
    rec = Recorder([
        ("POST", "/photos", lambda r: httpx.Response(200, json={"id": "ph"})),
        ("POST", "/feed", httpx.Response(200, json={"id": "page-1_1"})),
    ])
    set_http_client_override(rec.client())
    connect("facebook", "page-1", token="page-token")
    sync(mls_id="MOCK1001")
    run_pending()
    p = db.scalar(select(Post))
    assert p.status == "published" and p.external_post_id == "page-1_1"
    feed = form([c for c in rec.calls if "/feed" in str(c.url)][0])
    assert feed["access_token"] == "page-token" and "NEW LISTING" in feed["message"]
    assert len([c for c in rec.calls if "/photos" in str(c.url)]) == 3  # 3 authorized photos


def test_engine_expired_meta_token_blocks_account(db, provider, connect, sync):
    rec = Recorder([("POST", "/photos", httpx.Response(400, json={"error": {"code": 190, "message": "Session has expired"}}))])
    set_http_client_override(rec.client())
    acct = connect("facebook", "page-1")
    sync(mls_id="MOCK1001")
    run_pending()
    db.expire_all()
    assert db.scalar(select(Post)).status == "blocked_auth"
    assert db.get(SocialAccount, acct).status == "reauth_required"


def test_oauth_authorize_redirect_and_callback(db, client, login, monkeypatch):
    from crosspost.config import reset_settings_cache

    monkeypatch.setenv("X_CLIENT_ID", "client-abc")
    monkeypatch.setenv("X_CLIENT_SECRET", "client-secret")
    reset_settings_cache()
    csrf = login("admin")
    resp = client.post("/admin/oauth/x/start", data={"csrf_token": csrf, "external_account_id": "u1", "display_name": "Our X"})
    assert resp.status_code == 303
    location = resp.headers["location"]
    assert location.startswith("https://x.com/i/oauth2/authorize?")
    qs = parse_qs(location.split("?", 1)[1])
    assert qs["code_challenge_method"] == ["S256"] and "tweet.write" in qs["scope"][0]
    assert qs["redirect_uri"] == ["https://homes.example.com/admin/oauth/x/callback"]

    rec = Recorder([("POST", "/2/oauth2/token", httpx.Response(200, json={
        "access_token": "AT", "refresh_token": "RT", "expires_in": 7200, "scope": "tweet.read tweet.write users.read media.write offline.access"}))])
    set_http_client_override(rec.client())
    assert client.get(f"/admin/oauth/x/callback?code=c1&state=wrong").status_code == 400
    # state was consumed by the failed attempt; start again
    resp = client.post("/admin/oauth/x/start", data={"csrf_token": csrf, "external_account_id": "u1", "display_name": "Our X"})
    state = parse_qs(resp.headers["location"].split("?", 1)[1])["state"][0]
    resp = client.get(f"/admin/oauth/x/callback?code=c1&state={state}")
    assert resp.status_code == 303
    account = db.scalar(select(SocialAccount).where(SocialAccount.platform == "x"))
    assert account.status == "connected" and account.token_expires_at is not None
    token_req = form(rec.calls[0])
    assert token_req["code_verifier"] and token_req["grant_type"] == "authorization_code"
    assert rec.calls[0].headers["Authorization"].startswith("Basic ")
