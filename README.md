# INFLUENCE Bot

An automated Slack bot for **INFLUENCE** — an influencer marketing business that connects brands with Instagram creators for social media marketing campaigns.

## What It Does

INFLUENCE Bot automates the entire creator-brand content workflow:

1. **Video Review & Approval** — Creators submit draft videos via Tally. The bot sends them to brand POCs on Slack with Approve / Request Changes buttons. Decisions trigger automatic emails to creators.

2. **The post-deadline chase** — When a creator misses their posting deadline, the bot works a four-rung ladder from `jennifer@useinfluence.xyz`: an overdue notice at D+1, a nudge naming the shortfall at D+3, a final notice at D+7, and at D+10 it stops emailing and hands the creator to the team in Slack. Every rung carries a link into the creator's campaign chat, and every Slack alert carries Snooze / Stop buttons so a person can halt the ladder.

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
│   ├── actions.py                  # Interactive actions (approve / request-changes / ignore / mark-as-paid)
│   └── chat_routes.py              # Creator <-> brand chat-space HTTP routes
├── services/
│   ├── reelstats_api.py            # Polls GET /api/bot/campaigns on the consolidated container
│   ├── webhook_handler.py          # Handles ReelStats webhook events (review/video-links submitted)
│   ├── scheduler_service.py        # Poll loop + milestone/deliverable/deadline/upload checks
│   ├── review_approval.py          # Shared approve / 24h auto-approval flow
│   ├── review_coverage.py          # Do a creator's in-review videos cover what they still owe?
│   ├── review_ignore.py            # "Ignore" — take a mistaken submission out of play
│   ├── review_messages.py          # Rebuilds the admin/brand Slack messages a review lives in
│   ├── email_service.py            # Resend HTTPS email sending (jennifer@useinfluence.xyz)
│   ├── brand_routing.py            # Maps Slack workspaces <-> brands for per-brand notifications
│   ├── slack_oauth.py              # Per-brand install links + OAuth callback
│   ├── chat_service.py             # Creator <-> brand chat spaces
│   ├── creator_updates.py          # Campaign updates -> creators' WhatsApp (via the outreach service)
│   └── ai_drafts.py                # Claude-drafted reply options for the admin composer
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
        │    (same card + an Ignore button, INFLUENCE workspace only)
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
| **Outreach service** | Delivers campaign updates to creators on WhatsApp (`POST /api/bot/creator-updates`) | `Influence-Inc/Outreach_Email_Automation` |

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

Optional:
- `ANTHROPIC_API_KEY` — enables the ✨ AI-draft button in the admin chat composer (see below); unset leaves the feature off
- `ALERTS_SLACK_WEBHOOK_URL` — optional: a Slack incoming webhook for the alerts channel. When a Claude call fails because the Anthropic Console account is out of credits (or hit its usage limit) or the key is rejected, the bot posts there once, then every 3 hours while it lasts (`utils/claude_alerts.py`). The creator-list worker and the outreach backend use the same variable and channel
- `OUTREACH_API_BASE` + `OUTREACH_BOT_TOKEN` — the outreach service's base URL and its `OUTREACH_BOT_TOKEN`. Set both to send creators their campaign updates on WhatsApp (see **Campaign updates to creators on WhatsApp** below); with either unset the bot's Slack and email notifications behave exactly as before and no WhatsApp copy is sent

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
   | `CHASE_TIMEZONE` | `America/Los_Angeles` *(optional)* — the zone every "today" in the deadline ladder is read in. Deadlines are bare calendar dates worked to by US creators; on a UTC host the two disagree for the last seven hours of every day, which marks a Californian creator overdue while it is still their deadline afternoon |
   | `CHASE_SEND_HOUR_START` / `CHASE_SEND_HOUR_END` | `9` / `17` *(optional)* — chase rungs only send inside these hours, local to `CHASE_TIMEZONE`. The poll ticks every minute, so without a window a final notice arrives at 12:01am |
   | `CHASE_RUNG_DAYS` | `1,3,7,10` *(optional)* — days past the deadline each of the four rungs is due on. A malformed value logs an error and falls back to the default rather than taking the chase down |
   | `CHASE_ACTIVITY_SNOOZE_DAYS` | `3` *(optional)* — a creator who shared a draft or logged a post this recently is already moving, so the ladder holds |
   | `CHASE_INBOUND_ENABLED` | unset *(optional)* — set to `true` to switch on reading creator replies. Leave off until the reply domain is actually routing: turning it on moves `Reply-To` off the human inbox |
   | `CHASE_REPLY_DOMAIN` | `reply.useinfluence.xyz` *(optional)* — the receiving domain per-chase reply addresses live on |
   | `RESEND_WEBHOOK_SECRET` | `whsec_…` — signing secret for the inbound webhook. **Required** for `/webhook/resend`, which refuses everything without it |
   | `RESEND_RECEIVING_API_URL` | `https://api.resend.com/emails/receiving` *(optional)* — where an inbound email's body is fetched from |
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

> **Channel required — DMs are rejected.** If the brand picks a Direct
> Message on the consent screen instead of a channel, the callback rejects
> the install (nothing is persisted) and shows a "Please pick a channel"
> page asking them to re-install and choose a dedicated channel. This keeps
> notifications visible to the whole brand team rather than a single person.
> A 1:1 DM is detected by its `D…` conversation id; private channels (`G…`)
> are allowed.

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
- **Escalating deadline reminders** — 3 days before -> 1 day before, via Slack + email
- **The post-deadline chase ladder** — four rungs at D+1, D+3, D+7 and D+10 (`CHASE_RUNG_DAYS`), then a hard stop. The first keeps the old `overdue` name so anyone mid-chase isn't re-emailed when it ships. Rules that decide whether a rung actually goes out live in `services/chase_ladder.py`:
  - **Highest due rung only** — a creator already 30 days late the day this deploys gets one final notice, not four emails in a minute
  - **The clock holds while the brand has the ball** — days where the creator's drafts sit unanswered aren't counted, so brand silence can't march a creator to a final notice for work that isn't theirs
  - **Never chased for views** — a creator whose videos are all live and who is only short on `minViews` gets no email; views accrue on their own
  - **Recent activity pauses it** — a draft or a live post in the last `CHASE_ACTIVITY_SNOOZE_DAYS` days
  - **US calendar** — "today" is read in `CHASE_TIMEZONE` (default `America/Los_Angeles`), rungs only send between `CHASE_SEND_HOUR_START` and `CHASE_SEND_HOUR_END`, and a rung landing on a weekend slides to the next working day
- **The chase brake** — Snooze 3d / Snooze 7d / Stop chasing buttons on every post-deadline Slack alert. Resend only sends, so a creator replying by email lands in a human inbox the bot can't read; whoever reads that reply is already in `#creator-deadlines` and can halt the ladder in one click
- **Creator replies are read** — each chase email carries a per-chase `Reply-To` (`chase+<token>@CHASE_REPLY_DOMAIN`), so a creator hitting reply lands at `POST /webhook/resend` instead of a human inbox nobody automates. The reply is attributed by that address (not the `From:`, which changes and is shared), its body fetched from Resend's receiving API, and read by Claude. When the creator names a date it is posted to `#creator-deadlines` as a **proposal with a Confirm button** — a deadline is a contract term, and a model reading an email is not authority to rewrite one. Any reply pauses the ladder 3 days so the next rung doesn't go out from under whoever is reading it. Off until `CHASE_INBOUND_ENABLED` is set; the route **fails closed** without `RESEND_WEBHOOK_SECRET`
- **Deadline changes are announced** — the campaigns dashboard fires `deadline_changed` whenever a stored deadline moves, whoever moved it (an admin, a signed contract, or a revision the bot wrote back). The bot posts it to `#creator-deadlines` and **resets the ladder** for the new date — rung dedup rows don't record which deadline they were sent for, so without the reset a creator who agrees a new date would sail past it in silence
- **Reminder emails skip creators waiting on review** — the nag email is held when the videos a creator has already shared for review cover the deliverables they still owe (counted, so 1 video shared against 2 still owed still emails; a draft sent back with "Request Changes" or marked as ignored doesn't count, and an unmet view target needs at least one video still in the pipeline). The Slack alert still posts, annotated with why no email went out
- **Real-time webhook alerts** — Review submissions, video-link submissions, approvals (poll is the safety-net fallback)
- **24h review auto-approval** — Sweeps every 30 min to auto-approve reviews left un-actioned for 24h. A chat message from someone other than the creator (brand or INFLUENCE) means the review is being worked and stops the clock; the creator's own messages don't. Each draft gets its own clock, so feedback on an earlier draft doesn't keep a later one from auto-approving
- **Ignore a submission that was never meant for review** — An **Ignore** button on the `#content-reviews` copy of every review (INFLUENCE workspace only — brands never see it) for creators who paste the wrong link, submit a duplicate, or test the form. An ignored submission is out of play: it's skipped by the 24h auto-approval sweep, so no approval email reaches the creator, and it stops counting as a video in review, so their deadline reminder emails keep going out. The brand's copy loses its Approve / Request Changes buttons at the same time, with a neutral "no review needed" note. **Undo ignore** puts it back, decision and all — the 24h clock is not restarted, since it measures how long the brand has been silent. Review messages posted before the button shipped pick it up automatically: a one-shot backfill re-renders every still-pending review message shortly after boot
- **One chat space per campaign** — A creator's chat opens on their first submission and is reused for every draft on that campaign, so earlier feedback stays on screen. Approving a draft posts a notice in the chat instead of closing it; the space is archived when the campaign ends
- **Draft cards in the chat** — Each new video submitted for review lands in the chat as a card (draft number, source, tap to watch) on the creator's side of the conversation
- **Draft link previews** — The card shows a real thumbnail of the video. The server resolves the link (Google Drive, YouTube, Vimeo and Loom via their thumbnail endpoints; anything else via the page's `og:image`), fetches the image and serves it from our own origin, so the preview works regardless of hotlink rules and the creator's URL never leaves the server. Previews are cached for 6h; a link with no usable preview (an unshared Drive file, say) quietly keeps the placeholder artwork
- **AI-drafted admin replies** — The ✨ button in the admin composer opens a draft sheet: say what you want to get across in shorthand ("hook at 0:03, and we can wait for her AI credits, no rush") and Claude writes it as a full message, or leave the field empty and get replies read off the conversation. Drafts come back as iMessage-style bubbles above the composer — tap one to drop it in the composer, or ✎ to edit it in place first. They're written in the voice the team already uses with creators: greet by first name, lead with what's working, soften every ask and say why, keep the emoji, and break a long note into paragraphs and a numbered list. Nothing is ever posted automatically, and creators and brands never see the button. Set `ANTHROPIC_API_KEY` to switch it on (optional: `CLAUDE_MODEL`, `CLAUDE_EFFORT`, `CLAUDE_MAX_TOKENS`, `AI_DRAFT_CONTEXT_MESSAGES`); with the key unset the button isn't rendered and the chat is otherwise unchanged
- **Campaign updates to creators on WhatsApp** — Everything this bot already tells Slack and email about a creator's content is also sent to the creator, on the channel they actually read. This bot has the events; the outreach service (`Influence-Inc/Outreach_Email_Automation`) has the phone numbers, the WhatsApp Business credentials and the approved templates, so `services/creator_updates.py` reports each event there — a draft submitted, the brand's approval (with the submit-posts link), feedback from the review chat relayed verbatim, a live post link, and deliverables complete — and every decision needing data this bot doesn't have (is the creator subscribed? is their 24h WhatsApp window open? send now or queue it?) is made on that side. A creator joins the lane when they sign their first contract and stays on it afterwards, so the next campaign's outreach reaches them in the same thread instead of a cold email. Each event carries a dedup key (the review id, the video id) so a webhook redelivery and the polling safety net can't message the creator twice. Sends are fire-and-forget on a background thread and never raise — an unreachable outreach service costs the WhatsApp copy, never the Slack post. Set `OUTREACH_API_BASE` + `OUTREACH_BOT_TOKEN` to switch it on
- **Team & brand stay notified of chat activity** — Every creator/brand chat message pings `#content-reviews`, threaded under the review post so the conversation stays grouped. Because Slack doesn't notify anyone of a thread reply they aren't following, inbound (creator/brand) messages are also broadcast to the channel so the team actually sees them; the team's own admin-sent replies stay quiet threaded posts. Set `SLACK_REVIEWS_NOTIFY` (a user-group, user, or `<!here>`/`<!channel>` mention) to add a hard ping on inbound messages even when the channel is muted. The same broadcast applies to the brand's own workspace ping (creator/admin messages), so brands aren't left watching a silent thread either
