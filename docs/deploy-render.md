# Deploying to Render

`render.yaml` (a Render Blueprint) creates three resources:

| Resource | Type | What it runs |
|---|---|---|
| `crosspost-web` | Web service (Docker) | Dashboard, landing pages, tracked links. Runs database migrations before each deploy (`preDeployCommand`). Health check: `/healthz`. |
| `crosspost-worker` | Background worker (Docker) | Job queue and scheduler: MLS sync, publishing, retries, notifications, CRM delivery. |
| `crosspost-db` | PostgreSQL | All application data. |

Both services build from the repository's `Dockerfile` and deploy automatically on pushes to `main`. Workers and pre-deploy commands need paid instance types; check Render's current pricing for the `starter` and `basic-256mb` plans used here.

## 1. Prerequisites

- A Render account with access to the GitHub repository.
- The domain you want to serve from, e.g. `listings.yourbrokerage.com`. You can start with the `*.onrender.com` URL and add the domain later.
- SMTP credentials for lead notification emails, e.g. from your email provider, Postmark, SendGrid or Amazon SES.
- **MLS RESO Web API credentials** from your MLS or its data vendor. Production refuses to start without them (see [Before you have MLS credentials](#before-you-have-mls-credentials)).

## 2. Create the Blueprint

1. In the Render dashboard choose **New → Blueprint**, select the repository, and pick the `main` branch.
2. Render reads `render.yaml` and asks for each value marked `sync: false`:

   | Variable | Value |
   |---|---|
   | `PUBLIC_BASE_URL` | `https://crosspost-web.onrender.com` for now, or your custom domain. Used in every tracked link. |
   | `BROKERAGE_NAME` | Your brokerage's name as it should appear in attributions. |
   | `BROKERAGE_OFFICE_IDS` | Your MLS office ID(s), comma separated. Only these offices' listings are marketed. |
   | `LEAD_NOTIFICATION_EMAILS` | Who receives new-lead emails, comma separated. |
   | `EMAIL_FROM`, `SMTP_HOST`, `SMTP_USERNAME`, `SMTP_PASSWORD` | Your SMTP settings. |
   | `RESO_BASE_URL`, `RESO_TOKEN_URL`, `RESO_CLIENT_ID`, `RESO_CLIENT_SECRET` | From your MLS data vendor. |

   You only enter these on the web service; the worker reads them from it.
3. Apply the Blueprint. Render generates `SECRET_KEY`, `TOKEN_ENCRYPTION_KEY` and `IP_HASH_SALT` once and shares them between both services through the `crosspost-secrets` environment group.

> **Do not regenerate `TOKEN_ENCRYPTION_KEY`** once social accounts are connected: their stored tokens can no longer be decrypted and every account must be reconnected.

## 3. Create the first admin user

Open the web service's **Shell** tab in Render and run:

```bash
python -m crosspost.cli create-user you@yourbrokerage.com --role admin
python -m crosspost.cli check-config
```

`create-user` prompts for a password (12+ characters, mixed case). Then sign in at `PUBLIC_BASE_URL/login`.

## 4. Add your domain (optional)

1. In the web service's **Settings → Custom Domains**, add your domain and create the DNS record Render shows. Render issues the TLS certificate.
2. Update `PUBLIC_BASE_URL` on the web service to the new `https://` address. The worker picks it up automatically on its next deploy; trigger one with **Manual Deploy** so new posts use the new address.

Links already published keep working only while the old hostname still reaches the service, so settle the domain before you start publishing.

## 5. Optional integrations

Add these to the **`crosspost-settings`** environment group (Dashboard → Env Groups) so both services receive them:

| Integration | Variables |
|---|---|
| Facebook / Instagram / Threads | `META_APP_ID`, `META_APP_SECRET` |
| LinkedIn | `LINKEDIN_CLIENT_ID`, `LINKEDIN_CLIENT_SECRET` |
| X | `X_CLIENT_ID`, `X_CLIENT_SECRET` |
| Pinterest | `PINTEREST_APP_ID`, `PINTEREST_APP_SECRET` |
| CRM | `CRM_PROVIDER` (`webhook` or `followupboss`) plus `CRM_WEBHOOK_URL` / `CRM_WEBHOOK_SECRET` or `FOLLOWUPBOSS_API_KEY` / `FOLLOWUPBOSS_SYSTEM_KEY` |
| Brokerage details | `BROKERAGE_PHONE`, `BROKERAGE_LICENSE`, `POST_ATTRIBUTION_SUFFIX`, `MLS_DISCLAIMER`, `DISPLAY_TIMEZONE`, `PRIVACY_CONTACT_EMAIL` |
| RESO tuning | `RESO_RESOURCE`, `RESO_EXPAND`, `RESO_EXTRA_FILTER`, `RESO_SCOPE` |
| Error monitoring | `SENTRY_DSN` (also add `sentry-sdk` to `requirements.txt`) |

Register each social platform's OAuth redirect URI as `PUBLIC_BASE_URL/admin/oauth/<platform>/callback` (for example `…/admin/oauth/facebook/callback`). Then connect accounts under **Social Accounts** and enable platforms under **Settings**.

## Before you have MLS credentials

`APP_ENV=production` deliberately refuses to start with the mock MLS feed, and the deploy will fail with a clear message in the logs. To try the deployed app first, temporarily set these in `crosspost-settings`:

```
APP_ENV=development
MLS_PROVIDER=mock
```

Only use this for a private trial. Development mode relaxes the production safety checks, and the mock listings are sample data that must not be published to real social accounts. Switch back to `production` and `reso` before going live.

## Operations

- **Deploys:** every push to `main` rebuilds both services. Migrations run before the new web version starts; if they fail, the old version keeps serving.
- **Logs:** JSON on stdout, visible in each service's **Logs** tab. Secrets and personal data are redacted.
- **Health:** `/healthz` checks the database connection.
- **Backups:** confirm the backup and point-in-time-recovery options for your Postgres plan in Render.
- **Scaling:** you can run more than one worker instance; jobs are claimed atomically, so they won't double-publish.

## Assumptions to check

`render.yaml` follows Render's documented Blueprint format, but it was written without access to Render's schema validator. If Render rejects a field when you apply the Blueprint, check:

- **Plan names:** `starter` for services and `basic-256mb` for Postgres. Render renames plans from time to time.
- **`preDeployCommand`:** requires a paid instance type.
- **`fromService` references:** used by the worker to read the web service's variables.
