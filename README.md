# INFLUENCE Bot

An automated Slack bot for **INFLUENCE** — an influencer marketing business that connects brands with Instagram creators for social media marketing campaigns.

## What It Does

INFLUENCE Bot automates the entire creator-brand content workflow:

1. **Video Review & Approval** — Creators submit draft videos via Tally. The bot sends them to brand POCs on Slack with Approve / Request Changes buttons. Decisions trigger automatic emails to creators.

2. **Automated Follow-Up Emails** — When a creator misses their posting deadline, the bot sends escalating follow-up emails (friendly reminder -> second nudge -> urgent notice) from `jennifer@useinfluence.xyz`.

3. **Team Notifications & Alerts** — Real-time Slack alerts for new campaigns, video submissions, approvals, overdue deadlines, and daily campaign summaries every morning at 9 AM.

4. **View Milestone Alerts** — The bot polls the ReelStats campaign API (and consumes its webhooks) for creator view counts and posts a Slack alert each time a video crosses a milestone (250K, 500K, 1M, …).

5. **Campaign Tracking** — Full lifecycle tracking: pending -> video submitted -> under review -> approved/changes requested -> posted.

## Architecture

```
INFLUENCE Bot
├── app.py                          # Main entry point (Flask + Slack Bolt); /webhook, /health, /slack/*
├── config.py                       # Environment variable configuration
├── bot/
│   ├── handlers.py                 # Slack event handlers (app_mention, message, team_join)
│   ├── commands.py                 # Slash commands (/influence-status, /influence-check, …)
│   ├── actions.py                  # Interactive actions (approve / request-changes / mark-as-paid)
│   └── chat_routes.py              # Creator <-> brand chat-space HTTP routes
├── services/
│   ├── reelstats_api.py            # Polls GET /api/bot/campaigns on the consolidated container
│   ├── webhook_handler.py          # Handles ReelStats webhook events (review/video-links submitted)
│   ├── scheduler_service.py        # Poll loop + milestone/deliverable/deadline/upload checks
│   ├── review_approval.py          # Shared approve / 24h auto-approval flow
│   ├── email_service.py            # Resend HTTPS email sending (jennifer@useinfluence.xyz)
│   ├── brand_routing.py            # Maps Slack workspaces <-> brands for per-brand notifications
│   ├── slack_oauth.py              # Per-brand install links + OAuth callback
│   └── chat_service.py             # Creator <-> brand chat spaces
├── models/
│   └── models.py                   # SQLAlchemy models (installs, reviews, dedup + chat tables)
├── templates/
│   ├── email_templates.py          # Email templates
│   └── slack_blocks.py             # Slack Block Kit message templates
└── utils/
    └── helpers.py                  # Utility functions
```

## Workflow

```
Creator submits video via Tally
        │
        ▼
  Tally Webhook ──► INFLUENCE Bot
        │
        ├──► Sends video to Brand's Slack channel
        │    (with Approve / Request Changes buttons)
        │
        ├──► Notifies INFLUENCE team on Slack
        │
        └──► Emails Brand POC about the submission
                    │
        ┌───────────┴───────────┐
        ▼                       ▼
   Brand Approves         Brand Requests Changes
        │                       │
        ▼                       ▼
  Email creator            Email creator
  "You're approved!"      with feedback
        │                       │
        ▼                       ▼
  Notify team              Notify team
  on Slack                 on Slack
```

## Integrations

| Service | Purpose | Link |
|---------|---------|------|
| **Slack** | Team notifications, brand approvals | Workspace `T09DSH6AEQH` |
| **ReelStats API** | Campaign + creator data (polls `GET /api/bot/campaigns`; receives webhooks) | see `BOT_API.md` |
| **Email (Resend)** | Follow-ups and approval notifications | `jennifer@useinfluence.xyz` |
| **Campaign Website** | Campaign management + creator submissions | https://campaign.influence.technology |

## Setup

### 1. Clone and Install

```bash
git clone <repo-url>
cd Influence_Bot
python3 -m venv venv
source venv/bin/activate
pip install -r requirements.txt
```

### 2. Configure Environment

```bash
cp .env.example .env
# Edit .env with your actual credentials
```

Required environment variables:
- `BOT_TOKEN` — sent as `x-bot-token` when polling the ReelStats `/api/bot/*` API (must match the server's `BOT_TOKEN`)
- `REELSTATS_API_URL` — base URL of the consolidated container (e.g. `https://campaign.influence.technology`)
- `SLACK_BOT_TOKEN` — Slack Bot User OAuth Token (`xoxb-...`)
- `SLACK_SIGNING_SECRET` — From Slack App settings
- `SLACK_CHANNEL_ID` — Fallback channel for notifications (per-type channels optional; see `config.py`)
- `RESEND_API_KEY` — Resend HTTPS API key for `jennifer@useinfluence.xyz` (Railway blocks outbound SMTP)

### 3. Create Slack App

At https://api.slack.com/apps, create a new app with:

**Bot Token Scopes:**
- `channels:history`, `channels:read`
- `chat:write`
- `commands`
- `im:write`
- `users:read`

**Event Subscriptions** (Request URL: `https://your-domain/slack/events`):
- `message.channels`
- `app_mention`
- `team_join`

**Slash Commands** (all point to `https://your-domain/slack/commands`):
- `/influence-status`
- `/influence-check`
- `/influence-install`
- `/influence-help`

**Interactivity** (Request URL: `https://your-domain/slack/actions`)

### 4. Configure Tally Webhook

In Tally Dashboard -> Your Form -> Integrations -> Webhooks:
- Webhook URL: `https://your-domain/webhooks/tally`

### 5. Deploy to Railway

The bot is designed to run on [Railway](https://railway.app) — `git push`
to the deploy branch and Railway rebuilds and redeploys automatically.
There is no local run path; gunicorn (pinned to one worker so the
in-process APScheduler doesn't fire jobs multiple times) is the only
supported server.

**One-time setup:**

1. **Create project.** Railway dashboard → *New Project* → *Deploy from
   GitHub repo* → pick `Influence-Inc/Influence_Bot` → select the deploy
   branch.

2. **Add a Volume for SQLite.** Service → *Settings* → *Volumes* → *New
   Volume*, mount path `/data`, size 1 GB. Without this, the database
   is wiped on every redeploy.

3. **Set environment variables** in the service's *Variables* tab
   (see `.env.example` for the full list):

   | Variable | Value |
   |---|---|
   | `BOT_TOKEN` | ReelStats polling token |
   | `REELSTATS_API_URL` | `https://campaign.influence.technology` |
   | `SLACK_BOT_TOKEN` | `xoxb-…` |
   | `SLACK_SIGNING_SECRET` | from Slack app |
   | `SLACK_CHANNEL_ID` | e.g. `C0XXXXXXXXX` |
   | `RESEND_API_KEY` | Resend HTTPS API key (domains verified on the Resend account) |
   | `EMAIL_FROM_ADDRESS` / `EMAIL_FROM_NAME` | e.g. `jennifer@useinfluence.xyz` / `Jennifer - INFLUENCE` *(optional; sensible defaults)* |
   | `DATABASE_URL` | `sqlite:////data/influence_bot.db` *(four slashes)* |
   | `POLL_INTERVAL_SECONDS` | `60` *(optional)* |
   | `TEST_CAMPAIGN_NAME` | `Dummy testing` *(optional, while testing)* |

   Railway also injects `PORT` automatically — don't set it yourself.

4. **Grab the public URL.** Service → *Settings* → *Networking* →
   *Generate Domain*. You'll get `https://<service>.up.railway.app`.

5. **Update Slack app URLs** at https://api.slack.com/apps:
   - *Event Subscriptions* → `https://<url>/slack/events`
   - *Slash Commands* (each one) → `https://<url>/slack/commands`
   - *Interactivity & Shortcuts* → `https://<url>/slack/actions`

6. **Update ReelStats webhook target.** On the ReelStats server, set
   `SLACK_WEBHOOK_URL=https://<url>/webhook` (see `BOT_API.md`).

7. **Verify.**
   - `curl https://<url>/health` → `200` JSON.
   - Run `/influence-check` in Slack → no timeout.
   - Check Railway logs for a single
     `Scheduler started: polling every 60s, daily summary at 9 AM` line.

From then on, every `git push` to the deploy branch triggers a new
Railway build + rollout automatically.

## Generating Install Links for Brands

Each brand installs INFLUENCE Bot into their own Slack workspace via a signed
OAuth link. The `incoming-webhook` scope causes Slack to prompt the installing
user to pick a channel during consent — that channel is stored alongside the
workspace token and is where the bot posts for that brand.

### 1. One-time setup on the Slack app

At https://api.slack.com/apps -> your app:

- **OAuth & Permissions** -> **Redirect URLs**: add
  `https://your-domain/slack/oauth_redirect`
- **Manage Distribution**: complete the checklist and activate public
  distribution (required for installing into other workspaces)
- **Scopes** -> Bot Token Scopes: `chat:write`, `channels:read`, `commands`,
  `incoming-webhook`, `users:read`

Then set these env vars on the bot host:

```
SLACK_CLIENT_ID=...           # from "Basic Information" -> "App Credentials"
SLACK_CLIENT_SECRET=...
SLACK_OAUTH_REDIRECT_URI=https://your-domain/slack/oauth_redirect
# Optional — defaults to the scopes listed above
SLACK_OAUTH_SCOPES=chat:write,channels:read,commands,incoming-webhook,users:read
```

### 2. Generate a per-brand link

Either use the CLI…

```bash
# Direct Slack URL (signed state embeds the brand; link expires after 10 min)
python generate_install_link.py acme

# Stable shareable URL routed through this app (no expiry — the signed state
# is minted at request time)
python generate_install_link.py acme --public-url https://your-domain
# -> https://your-domain/slack/install/acme
```

…or just share the app route directly:

```
https://your-domain/slack/install/<brand-slug>
```

Hitting that route 302s the brand to Slack's consent screen.

### 3. Flow the brand sees

1. Brand opens `https://your-domain/slack/install/acme`
2. Slack shows the app's consent screen; brand picks a channel + clicks Allow
3. Slack redirects back to `/slack/oauth_redirect` with `?code=...&state=...`
4. The bot exchanges the code for a bot token and saves a row in
   `slack_installations` containing `team_id`, `bot_token`, `channel_id`,
   `channel_name`, and `webhook_url`. From then on the bot uses that token +
   channel when posting on that brand's behalf.

### Endpoints added

| Route | Purpose |
|-------|---------|
| `GET /slack/install` | Generic install URL (no brand attribution) |
| `GET /slack/install/<brand>` | Per-brand install URL |
| `GET /slack/oauth_redirect` | OAuth callback — exchanges `code` for a token |

## Slack Commands

| Command | Description |
|---------|-------------|
| `/influence-status` | View active campaign statuses (brand workspaces see only their own brand) |
| `/influence-check` | Manually run all notification checks — milestones, deliverables, deadlines, uploads (admin only) |
| `/influence-install <brand>` | Generate a per-brand Slack install link (admin only) |
| `/influence-help` | Show all available commands |

## Automated Features

- **Poll-loop checks** — Every `POLL_INTERVAL_SECONDS` (default 60s) the bot re-fetches `GET /api/bot/campaigns` and runs milestone, deliverables-complete, deadline, and upload-follow-up checks (idempotent via per-alert dedup tables)
- **Daily summary at 9 AM** — Posts a payment-readiness overview to the payments channel
- **Escalating deadline reminders** — 3 days before -> 1 day before -> overdue, via Slack + email
- **Real-time webhook alerts** — Review submissions, video-link submissions, approvals (poll is the safety-net fallback)
- **24h review auto-approval** — Sweeps every 30 min to auto-approve reviews left un-actioned for 24h
