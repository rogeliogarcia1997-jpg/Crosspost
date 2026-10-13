# Crosspost — MLS listing distribution & buyer lead generation

Crosspost pulls listings from an authorized MLS data source and generates marketing posts for each platform from the MLS data. It publishes them to connected social accounts, or holds them for approval first. Each post carries a tracked link to a mobile-friendly landing page, and buyer inquiries from that page are attributed back to the platform and campaign that produced them.

> **Status.** The full workflow runs end to end against a **mock MLS feed** and a **sandbox publishing adapter**. Every live integration is implemented against its documented API but **has not been run against a live account**. Each one needs credentials and, in most cases, platform approval. See [What needs credentials or approval](#what-needs-credentials-or-approval).

---

## Contents
- [How it works](#how-it-works)
- [Architecture](#architecture)
- [Quick start (development)](#quick-start-development)
- [Running tests](#running-tests)
- [Configuration](#configuration)
- [MLS integration](#mls-integration)
- [Social platform setup](#social-platform-setup)
- [Publishing rules](#publishing-rules)
- [Lead capture, privacy & attribution](#lead-capture-privacy--attribution)
- [Analytics: what is and isn't measured](#analytics-what-is-and-isnt-measured)
- [Security](#security)
- [Compliance safeguards](#compliance-safeguards)
- [Deployment](#deployment)
- [What needs credentials or approval](#what-needs-credentials-or-approval)
- [Extending](#extending)

---

## How it works

1. **Sync.** The worker polls the MLS provider every `MLS_SYNC_INTERVAL_MINUTES`, using incremental `ModificationTimestamp` sync. Each record is normalized and compared with the stored copy. The sync detects new listings, price changes, status changes, added or removed open houses, photo changes, detail changes, and listings removed from the feed. Each change is recorded in the listing's sync history.
2. **Authorize.** A listing is marketed only if:
   - internet display is allowed (`InternetEntireListingDisplayYN`), and
   - it passes `MARKETING_SCOPE`. By default that means it is your brokerage's own listing; an administrator can also authorize or block a listing explicitly.

   Only photos whose MLS media permission allows redistribution are used.
3. **Campaign.** One campaign is created per listing. Each post gets its own tracking link (`/t/<code>`).
4. **Content.** Each platform adapter's `PlatformSpec` (length, photo count, hashtag count, link clickability, title limit) shapes that platform's version of the post. Copy comes only from MLS fields. The MLS remarks and features are screened for fair-housing language: flagged text is omitted and the post goes to manual review.
5. **Publish.** Depending on the rules, a post is published immediately, scheduled, or held for approval. Publishing runs on a durable job queue. Rate limits and transient errors are retried with backoff. Expired tokens block the account's posts until it is reconnected; they are never retried blindly. Duplicate posts are prevented at several layers (see below).
6. **Stop when unavailable.** When a listing leaves `Active` (pending, sold, withdrawn, removed from the feed…):
   - every pending promotional post is canceled and the campaign ends;
   - the publish step re-checks status immediately before every API call;
   - posts that were already published are deleted only if `delete_posts_on_unavailable` is enabled **and** the platform's API supports deletion.
7. **Capture leads.** The landing page records the visit with its attribution. The inquiry form validates input, applies spam and rate controls, and creates a contact, a lead (one per buyer and listing) and an inquiry with full attribution. It then notifies the configured recipients and optionally pushes the lead to a CRM.

## Architecture

```
crosspost/
  config.py            Settings from environment variables (+ production validation)
  db.py, models.py     SQLAlchemy 2 models (PostgreSQL in production, SQLite for dev/tests)
  security.py          scrypt password hashing, Fernet token encryption, DB-backed rate limiting
  logging_setup.py     JSON structured logs with secret/PII redaction; optional Sentry
  mls/                 MLS provider interface, RESO Web API provider, mock provider, RESO field mapping
  social/              Adapter interface + Facebook, Instagram, Threads, LinkedIn, X, Pinterest,
                       webhook, manual and sandbox adapters
  services/
    sync.py            Listing synchronization & change detection
    workflow.py        Event-driven reactions to listing changes
    content.py         Platform-specific content generation
    compliance.py      Marketing authorization + fair-housing screen
    campaigns.py       Campaigns and tracking links
    publishing.py      Post state machine: plan / approve / publish / retry / cancel
    accounts.py        Social account connection, token health, re-authorization
    leads.py           Lead validation, spam controls, attribution
    notifications.py   Email (console or SMTP)
    analytics.py       Measured metrics only
    rules.py           Publishing rules (env defaults, editable in the dashboard)
  crm/clients.py       CRM layer: signed webhook, Follow Up Boss
  jobs/                Durable DB-backed queue, handlers, periodic scheduler
  web/                 FastAPI app: public pages, dashboard, auth, OAuth
migrations/            Alembic migrations
tests/                 pytest suite (mock MLS + mocked HTTP; no live credentials)
```

**Processes:** the `web` process (uvicorn) and one or more `worker` processes (`python -m crosspost.cli worker`). The worker runs queued jobs and the periodic scheduler: MLS sync, scheduled posts, account health checks, reposts and cleanup. Jobs are database rows, so queued work survives restarts and outages. Several workers can run safely side by side: jobs and posts are claimed with conditional `UPDATE`s, and periodic jobs are de-duplicated with time-bucket idempotency keys.

**Duplicate-post prevention:**
- A unique constraint covers (campaign, account, kind, event key).
- A post is atomically claimed (`queued → publishing`) before the API call.
- Published posts are never re-sent.
- An outcome that may have succeeded — a timeout after the request was sent, or a worker that died mid-request — moves the post to `needs_verification` instead of retrying it.
- Each request carries an idempotency key, passed to adapters that support one.

## Quick start (development)

Requires Python 3.11+ (developed on 3.13).

```bash
python -m venv .venv && . .venv/bin/activate
pip install -r requirements-dev.txt
cp .env.example .env            # edit: at minimum BROKERAGE_OFFICE_IDS=OFF100 and BROKERAGE_NAME for the mock feed
python -m crosspost.cli migrate
python -m crosspost.cli create-user you@example.com --role admin      # prompts for a password
python -m crosspost.cli connect-test-account                          # sandbox publishing adapter
python -m crosspost.cli sync                                          # import the mock feed
uvicorn crosspost.main:app --reload                                   # http://localhost:8000/admin
python -m crosspost.cli worker                                        # in a second terminal
```

The mock feed (`data/mock_mls_feed.json`, RESO field names) contains:
- three listings from office `OFF100`, one of which has fair-housing-flagged remarks and a withheld address;
- one listing from another brokerage, which is never marketed;
- one pending listing.

Edit the file and run `sync` again to simulate price drops, status changes and open houses.

Useful commands:

| Command | Purpose |
|---|---|
| `python -m crosspost.cli sync [--mode full] [--mls-id X]` | Run a sync now |
| `python -m crosspost.cli run-jobs` | Process all due jobs once |
| `python -m crosspost.cli worker` | Long-running worker + scheduler |
| `python -m crosspost.cli check-config` | Validate config for `APP_ENV` |
| `python -m crosspost.cli generate-key` | New `TOKEN_ENCRYPTION_KEY` |

## Running tests

```bash
pytest                                   # 131 tests, SQLite
TEST_DATABASE_URL=postgresql+psycopg://user:pass@localhost/disposable_db pytest   # same suite on PostgreSQL (drops all tables)
```

The suite covers all nine acceptance tests in the brief:

| Brief | Tests |
|---|---|
| 1 New listing | `test_workflows.py::test_new_listing_detected_content_generated_and_published` |
| 2 Multiple platforms | `test_workflows.py::test_platform_specific_versions`, `test_adapters.py` |
| 3 Duplicate prevention | `test_repeated_sync_creates_no_duplicates`, `test_duplicate_publish_jobs_do_not_double_post` |
| 4 Price change | `test_price_reduction_triggers_configured_workflow` (+ disabled-rule test) |
| 5 Status change | `test_status_change_stops_promotion`, `test_queued_post_is_canceled_at_publish_time…`, `test_listing_removed_from_feed…` |
| 6 Lead capture | `test_leads_web.py::test_lead_capture_end_to_end` |
| 7 API failure | `test_rate_limit_is_retried_without_duplicate_posts`, `test_permanent_failure_is_not_retried`, `test_retries_give_up_after_max_attempts`, `test_uncertain_outcome_is_held_for_verification` |
| 8 Account auth | `test_expired_token_blocks_without_repeated_retries`, `test_expired_token_detected_by_health_check`, `test_engine_expired_meta_token_blocks_account` |
| 9 Security | `test_security.py` (access control, CSRF, validation, spam, rate limits, token encryption, log redaction, headers) |

`test_mls_content.py::test_migrations_match_models` fails if the models and the Alembic migrations drift apart. CI (`.github/workflows/tests.yml`) runs the suite on SQLite and PostgreSQL and checks the migrations on every push.

## Configuration

All configuration comes from environment variables. `.env.example` lists every variable, with placeholders only. Highlights:

| Variable | Purpose |
|---|---|
| `APP_ENV` | `development` / `test` / `production`. Production refuses to start with insecure settings. |
| `PUBLIC_BASE_URL` | Base URL for tracked links and landing pages (https in production) |
| `SECRET_KEY`, `TOKEN_ENCRYPTION_KEY`, `IP_HASH_SALT` | Session signing, social-token encryption at rest, IP hashing |
| `DATABASE_URL` | `postgresql+psycopg://…` in production |
| `MLS_PROVIDER` | `mock` or `reso` (+ `RESO_*` credentials) |
| `MARKETING_SCOPE`, `BROKERAGE_OFFICE_IDS`, `BROKERAGE_AGENT_IDS` | Which listings may be marketed |
| `BROKERAGE_NAME`, `BROKERAGE_LICENSE`, `POST_ATTRIBUTION_SUFFIX`, `MLS_DISCLAIMER` | Attribution and disclosures |
| `PUBLISH_MODE_NEW_LISTING`, `ENABLED_PLATFORMS`, … | Defaults for publishing rules (editable in **Settings**) |
| `LEAD_NOTIFICATION_EMAILS`, `EMAIL_BACKEND`, `SMTP_*` | Lead notifications |
| `CRM_PROVIDER`, `CRM_WEBHOOK_*`, `FOLLOWUPBOSS_*` | CRM delivery |
| `META_*`, `LINKEDIN_*`, `X_*`, `PINTEREST_*` | OAuth app credentials per platform |

## MLS integration

`crosspost/mls/base.py` defines the interface. `MLSProvider.fetch_listings(modified_since)` returns a `FetchResult` of `NormalizedListing`s, and `fetch_listing(mls_id)` returns one listing. Errors are classified as `MLSAuthError` (not retried; admins are notified, and scheduled syncs back off for an hour), `MLSRateLimitError` (honours `Retry-After`), `MLSTransientError` (exponential backoff) and `MLSDataError` (the record is skipped).

- **RESO Web API** (`MLS_PROVIDER=reso`): OData `$filter`/`$orderby`/`$expand`, follows `@odata.nextLink`, and authenticates with OAuth2 client credentials or a static bearer token. The token is refreshed once on a 401, and request timeouts apply. Vendors differ in `$expand` support and in field availability. Check your vendor's `$metadata`, and set `RESO_EXPAND=` (empty) if `$expand` is unsupported.
- **Mock** (`MLS_PROVIDER=mock`): reads RESO-shaped JSON through the same mapping code. It is clearly labelled as development data and is rejected in production.
- **Safety:** an empty "full snapshot" never mass-removes listings. Out-of-order records (an older `ModificationTimestamp`) are ignored. No scraping.

## Social platform setup

The **Social Accounts** page shows each platform's requirements, permissions and limitations. Connect an account through OAuth (once the platform's app credentials are set) or by entering a token manually. Tokens are stored encrypted.

| Platform | Adapter | What you need | Notable limits |
|---|---|---|---|
| Facebook Pages | `facebook` | Meta app with App Review for `pages_manage_posts`, `pages_read_engagement`, `pages_show_list`; Page admin | Personal profiles and Groups are not API-publishable → use `manual` |
| Instagram | `instagram` | Business/Creator account linked to a Page; `instagram_basic`, `instagram_content_publish` | ≥1 photo, public JPEG URLs, links not clickable (posts point to the link in bio → `/listings`), rolling 24h API publishing cap, no API delete |
| Threads | `threads` | `threads_basic`, `threads_content_publish` | 500 characters |
| LinkedIn | `linkedin` | Community Management API approval (`w_organization_social`) for company pages | Tokens ~60 days; refresh tokens only for approved partners |
| X | `x` | Developer project on a tier that allows posting; OAuth 2.0 PKCE | 280 characters (links = 23), 4 images, paid-tier quotas |
| Pinterest | `pinterest` | Approved app (Standard access), `pins:write`, `boards:read` | 1 image, title 100 / description 500 characters |
| Automation webhook | `webhook` | Any HTTPS endpoint (Zapier, Make, n8n, a scheduler) | Signed JSON (`X-Crosspost-Signature`) |
| Manual | `manual` | Nothing | The alternative workflow when automation is not allowed: content is generated, a person posts it and records the URL |
| Sandbox | `test` | Nothing | Development only |

OAuth redirect URI to register with each platform: `{PUBLIC_BASE_URL}/admin/oauth/{platform}/callback`.

## Publishing rules

Configured under **Settings**, with defaults from the environment:
- new listings: immediate, scheduled (+ delay) or manual approval;
- price-reduction posts (on/off and mode; only decreases trigger them);
- open-house posts (published N hours before the event, canceled if the open house is removed or has passed);
- "under contract" and "sold" posts (off by default; **always** require approval);
- reposting after N days;
- deleting published posts when a listing becomes unavailable;
- the platforms that receive automatic posts.

From a listing page, marketers can also generate posts for specific accounts.

## Lead capture, privacy & attribution

- The landing page is `/p/<slug>`, and `/listings` is the link-in-bio index. The form collects name, email, optional phone, preferred contact method, the property and an optional message.
- **Consent:** marketing email consent is a separate, optional checkbox and is never needed to submit an inquiry. SMS consent is required only if the buyer chooses text as their contact method. Consent timestamps and the consent text version are stored.
- **Spam and abuse controls:**
  - a honeypot field (bot submissions are silently discarded);
  - a signed form token with a minimum fill time;
  - limits on the number of links in a message;
  - DB-backed rate limits per hashed IP and per email;
  - de-duplication of repeat submissions within 10 minutes.
- **Personal data:**
  - IP addresses are stored only as salted HMAC hashes;
  - viewers cannot see lead contact details;
  - lead views and exports are audit-logged;
  - CSV exports are protected against formula injection;
  - logs redact emails, tokens and other secrets.
- **Attribution:** the click on `/t/<code>` redirects to the landing page with UTM parameters and the tracking code. A signed cookie keeps the attribution for 30 days, and every lead and inquiry records platform, campaign, tracking link, UTM values, referrer and timestamp.
- **Privacy requests:** an admin can erase a buyer's data from the lead page. This deletes their contact details and every lead and inquiry for them, across all listings; the audit entry records no personal data. Copies already sent to a CRM or by email must be deleted there.
- **Retention:** set `LEAD_RETENTION_DAYS` to purge inquiries older than N days, and any leads and contacts left with none. The daily cleanup job does this; `0` keeps leads until erased.
- Replace `templates/public/privacy.html` with a counsel-approved notice before going live.

## Analytics: what is and isn't measured

**Measured by this application:**
- clicks on our tracked links (bots excluded by user agent);
- landing page visits;
- leads and inquiries;
- qualified leads (statuses your team sets);
- conversion rates derived from these.

**Not collected and shown as "not available":** platform impressions, reach, engagement and platform-reported clicks. These need insights permissions that this integration does not include. Nothing is estimated.

## Security

- Authentication with scrypt-hashed passwords. Sessions are invalidated when a password changes or a user is deactivated.
- Roles: `viewer` (dashboards only, no lead contact details), `marketer` (approve posts, manage leads, trigger syncs) and `admin` (accounts, settings, users).
- CSRF tokens on every dashboard form.
- Login rate limiting and open-redirect protection.
- Strict CSP and security headers; HSTS when `SESSION_COOKIE_SECURE=true`.
- Social tokens are encrypted with Fernet. Disconnecting an account deletes its tokens.
- `APP_ENV=production` refuses to start without real secrets, PostgreSQL, HTTPS, secure cookies and a non-mock MLS provider.
- Secrets live only in environment variables, and logs redact them.

## Compliance safeguards

These reduce risk but do not replace review by your broker or counsel and your MLS's rules.

- Only authorized listings are marketed; other brokerages' listings are excluded by default.
- Internet-display and address-display opt-outs are honoured.
- Only photos with redistribution permission are used.
- Every post carries brokerage and agent attribution. If attribution is unavailable, the post is not generated.
- Landing pages show the MLS disclaimer, the "last updated" date and Equal Housing Opportunity.
- Generated copy never references people or demographics, and MLS text is screened for fair-housing issues.
- Listing details are never invented.
- A listing is never promoted as available once its status says otherwise.

## Deployment

**Render:** `render.yaml` is a Render Blueprint for the web service, worker and PostgreSQL. Follow [docs/deploy-render.md](docs/deploy-render.md).

**Docker Compose** (PostgreSQL + migrations + web + worker):

```bash
cp .env.example .env    # fill in production values; APP_ENV=production, POSTGRES_PASSWORD, etc.
docker compose up -d --build
docker compose run --rm web python -m crosspost.cli create-user you@yourbrokerage.com --role admin
```

**Any container platform:**
1. Build the `Dockerfile` image.
2. Run `python -m crosspost.cli migrate` as a release step.
3. Run the web command (default) behind an HTTPS reverse proxy.
4. Run at least one worker with `python -m crosspost.cli worker`.
5. Point `/healthz` at your health checks.
6. Set `SENTRY_DSN` (and `pip install sentry-sdk`) for error monitoring.

Logs are JSON on stdout.

**Production checklist:** `python -m crosspost.cli check-config` passes. Then:
- TLS is terminated at the proxy;
- the database has backups and encryption at rest;
- the privacy notice is replaced;
- `MLS_DISCLAIMER` and attribution are reviewed with your MLS;
- platform apps are approved;
- `PUBLISH_MODE_NEW_LISTING=manual_approval` is kept until the content has been reviewed.

## What needs credentials or approval

| Integration | Status | What it needs |
|---|---|---|
| RESO Web API MLS feed | Implemented; tested against mocked HTTP | MLS/vendor data license + API credentials; field and `$expand` validation against the vendor's metadata |
| Facebook Pages / Instagram / Threads | Implemented; tested against mocked HTTP | Meta app, App Review for the listed permissions, Business verification |
| LinkedIn | Implemented; tested against mocked HTTP | Community Management API approval |
| X | Implemented; tested against mocked HTTP | Developer account on a tier that permits posting; verify media upload endpoint for your tier |
| Pinterest | Implemented; tested against mocked HTTP | Standard API access approval |
| Follow Up Boss CRM | Implemented; tested against mocked HTTP | API key + registered system key; validate with a test account |
| Webhook CRM / webhook publishing | Working | Your endpoint |
| SMTP email | Implemented | SMTP credentials (console backend in development) |
| Platform insights (impressions/engagement) | **Not implemented** | Additional permissions; intentionally reported as unavailable |

API versions and limits change. Check each adapter against the platform's current documentation before going live.

## Extending

- **New MLS feed:** subclass `MLSProvider`, return `NormalizedListing`s (reuse `mls/mapping.py` for RESO-shaped data), and register it in `mls/registry.py`.
- **New social platform:** subclass `SocialAdapter` with a `PlatformSpec`, implement `publish` (plus `delete`, `check_credentials` and `oauth_config` where supported), raise the shared error types, and add it to `social/registry.py`. Content generation, the queue, retries, approvals and analytics then work without further changes.
- **New CRM:** subclass `CRMClient` in `crm/clients.py` and register it in `get_crm_client`.
- **Schema changes:** edit `models.py`, then run `alembic revision --autogenerate -m "..."`.
