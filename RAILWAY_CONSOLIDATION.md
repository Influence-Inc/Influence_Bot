# Railway Consolidation Runbook — Influence Platform

> This document is committed identically to all four service repos so it is
> discoverable from any of them. It describes how the four independently
> deployed Railway services are consolidated into **one Railway project** with
> **private networking**, with **zero downtime, no data loss, and full
> rollback**. The repo-side changes are already committed on the
> `claude/railway-repo-consolidation-ke2rlq` branch of each repo; the Railway
> dashboard steps below are **manual actions performed by an operator** with
> Railway account access.

Repositories in scope:

| Repo | Service | Stack | Datastore | Health |
| --- | --- | --- | --- | --- |
| `influence-stats` (ReelMetrics) | `influence-stats` | Node/Express (`server.js`) | JSON file on **volume** (`DATA_DIR`) | `GET /health` |
| `Influence_Bot` | `influence-bot` | Python/Flask + Slack Bolt | SQLite on **`/data` volume** | `GET /health` |
| `Outreach_Email_Automation` | `outreach` | Node/Express (`backend/` subdir) | **Postgres** | `GET /health` |
| `Creator-Database` | `creator-database` | NestJS/TS | **Postgres** + Prisma | `GET /health` |

---

## 1. Chosen architecture and why it is the safest

**One Railway project → four independent services**, not a monorepo/single
container.

```
                     Railway project: "influence-platform"
   ┌──────────────────────────────────────────────────────────────────┐
   │                                                                    │
   │   [influence-stats]      [influence-bot]      [outreach]           │
   │        │  ▲                   │  ▲                │ ▲               │
   │        │  └──── /webhook ─────┘  │                │ │               │
   │        └── /api/bot/campaigns ───┘                │ │               │
   │        ▲                                          │ │               │
   │        └────── /api/bot (contracts) ──────────────┘ │               │
   │                            /api/bot/campaigns ──────┘               │
   │                                                                    │
   │   [outreach] ── x-api-key ──▶ [creator-database]                    │
   │                                                                    │
   │   Postgres (outreach)      Postgres (creator-database)              │
   │   Volume (stats DATA_DIR)  Volume (bot /data)                       │
   │                                                                    │
   │   ── all internal hops over *.railway.internal (private network) ──│
   └──────────────────────────────────────────────────────────────────┘
   Public custom domains stay attached for EXTERNAL callers only:
     campaign.influence.technology  → influence-stats
     campaigns.influence.technology → outreach   (see §3 domain note)
```

Why this is safest and correct for Railway:

- **Merging the four apps into one service/container would be wrong.** They are
  three different runtimes (Node, Python, NestJS/Docker) with hard constraints:
  `influence-bot` must run **exactly one** web worker (its in-process
  APScheduler would otherwise fire every job N times), and both `influence-stats`
  and `influence-bot` depend on their **own persistent volumes**, which cannot
  be shared between services. One container would couple release cycles, force a
  single runtime, and break these invariants.
- A **Railway project is the natural consolidation boundary**: services in the
  same project + environment share a **private network** with automatic
  `*.railway.internal` DNS and can share secrets via **reference variables** —
  giving "one unified deployment" while every service keeps its own build,
  scaling, scheduler, volume, and domain.
- **Every service-to-service call already reads a URL from an environment
  variable — there are zero hardcoded cross-service URLs in any repo.** So the
  switch to private networking is **pure configuration**: repoint the existing
  env vars at `*.railway.internal`. No application code changes are required for
  networking, which is why this migration carries very little regression risk.
- Cutover is **blue-green**: the new project is stood up beside the existing
  live deployment and verified before any domain moves. Rollback at every step
  is "point the domain / env var back", and the old project stays intact until a
  soak period passes.

---

## 2. Repo-side changes already made (on `claude/railway-repo-consolidation-ke2rlq`)

All changes are **additive and backward-compatible** — they are no-ops under the
current single-project deploys and change no runtime behavior until the operator
performs the Railway steps in §3.

- **`influence-stats`**
  - Added an explicit `GET /health` route in `server.js` (returns
    `{status:"healthy",service:"influence-stats"}`), registered before the SPA
    catch-all. Previously the only health signal was the `/` catch-all returning
    HTML.
  - Added `railway.json` (NIXPACKS builder, `startCommand: "node server.js"`
    identical to the `Procfile`, `healthcheckPath: "/health"`, restart on
    failure). Makes the deploy deterministic and gives Railway a real health
    gate.
- **`influence-bot`**
  - Added `railway.json` whose `startCommand` is **byte-for-byte identical to
    the existing `Procfile`** (`gunicorn --workers 1 --threads 64 --worker-class
    gthread --timeout 60 --bind 0.0.0.0:$PORT app:flask_app`). This pins the
    critical **single-worker** invariant in version-controlled config,
    `healthcheckPath: "/health"`, restart on failure.
- **`Outreach_Email_Automation`**
  - Added `backend/railway.json` (NIXPACKS, `startCommand: "npm start"` which
    already runs `migrate.js` then `server.js`, `healthcheckPath: "/health"`,
    restart on failure). The Railway service's **root directory must be
    `backend`** (see §3) so this file is picked up.
- **`Creator-Database`**
  - **No code change** — it already ships a complete `Dockerfile` +
    `railway.json` (`/health`, restart on failure, migrations applied on boot
    after the port binds).

The `Procfile`s in `influence-stats` and `influence-bot` are intentionally left
in place; `railway.json`'s `startCommand` takes precedence and matches them
exactly, so behavior is unchanged whether Railway reads the Procfile or the JSON.

---

## 3. Manual Railway steps (operator with account access)

> Perform these in order. Nothing here is destructive to the live deployment
> until the domain move in step F. Keep the old project running untouched
> throughout.

### A. Create the project and databases
1. Create a new Railway project, e.g. **`influence-platform`**.
2. Decide on Postgres. Lowest-risk option: **keep the two existing Postgres
   instances** (Outreach's and Creator-DB's) and have the new services connect
   to them by reusing their `DATABASE_URL` values — no data movement at all. If
   you prefer databases living inside the new project, add Postgres plugin(s)
   and migrate with `pg_dump | pg_restore` during step E (verify row counts
   before cutover).
3. Private networking is enabled per environment by default — confirm it is on
   for this project's environment.

### B. Add the four services (pointed at the consolidation branch first)
Create four services in the project, each connected to its GitHub repo and the
`claude/railway-repo-consolidation-ke2rlq` branch (switch each back to
`master`/`main` after the PRs merge — see §4). Suggested service names (they
determine the internal DNS name): `influence-stats`, `influence-bot`,
`outreach`, `creator-database`.

- For **`outreach`**, set **Root Directory = `backend`** (Settings → Source), so
  `backend/railway.json` and the app are found.
- For **`influence-bot`**, the added `railway.json` already enforces
  `--workers 1`; confirm no `Start Command` override re-introduces more workers.

### C. Recreate environment variables (export, don't retype)
For each service, copy every variable from the corresponding **existing live
service** (Railway → old service → Variables → export / "Raw Editor"), so
secrets are transferred verbatim. Do **not** hand-transcribe secret values.
Full inventory:

- **influence-stats:** `PORT` (Railway-injected), `APIFY_TOKEN`, `POSTHOG_KEY`,
  `ADMIN_PASS`, `BOT_TOKEN`, `SLACK_WEBHOOK_URL`, `OUTREACH_BASE_URL`,
  `OUTREACH_BOT_TOKEN`, `DATA_DIR`.
- **influence-bot:** `BOT_TOKEN`, `REELSTATS_API_URL`, `SLACK_BOT_TOKEN`,
  `SLACK_SIGNING_SECRET`, `SLACK_CHANNEL_ID`, `SLACK_CLIENT_ID`,
  `SLACK_CLIENT_SECRET`, `SLACK_OAUTH_REDIRECT_URI`, `SLACK_OAUTH_SCOPES`,
  `SLACK_OAUTH_STATE_SECRET`, `SLACK_CHANNEL_REVIEWS`, `SLACK_CHANNEL_UPLOADS`,
  `SLACK_CHANNEL_PAYMENTS`, `SLACK_CHANNEL_MILESTONES`, `SLACK_CHANNEL_DEADLINES`,
  `RESEND_API_KEY`, `EMAIL_FROM_ADDRESS`, `EMAIL_FROM_NAME`, `EMAIL_REPLY_TO`,
  `DATABASE_URL`, `POLL_INTERVAL_SECONDS`, `TEST_CAMPAIGN_NAME` (only if
  intentionally set), `PUBLIC_BASE_URL`, `CHAT_SECRET_KEY`, `CHAT_ADMIN_TOKEN`,
  `CHAT_UPLOADS_DIR`, `CHAT_MAX_ATTACHMENT_BYTES`, `CHAT_MAGIC_LINK_TTL`,
  `CHAT_SESSION_TTL`, `CHAT_BRAND_LINK_TTL`.
  - Note: `RESEND_API_KEY` is the real email mechanism; the `SMTP_*` vars in the
    old `.env.example` are dead code — do **not** carry them.
- **outreach:** `PORT` (injected), `PUBLIC_BASE_URL`, `DATABASE_URL`,
  `SCHEDULER_INTERVAL_MINUTES`, `EMAIL_VERIFY`, `SEND_PACING_MS`, `SENDER_NAME`,
  `CAMPAIGNS_API_BASE`, `CAMPAIGNS_API_TOKEN`, `IG_SESSION_COOKIE`,
  `INSTANTLY_API_KEY`, `INSTANTLY_CAMPAIGN_ID`, `INSTANTLY_WEBHOOK_SECRET`,
  `INSTANTLY_TIMEOUT_MS`, `INSTANTLY_EACCOUNT`, `ANTHROPIC_API_KEY`,
  `CLAUDE_MODEL`, `TARGET_CPM`, `RISK_BUFFER`, `BONUS_PERCENTAGE`, `NUM_VIDEOS`,
  `REQUIRE_OFFER_APPROVAL`, `NEGOTIATION_FOLLOWUP_DAYS`,
  `NEGOTIATION_MAX_FOLLOWUPS`, `BRAND_NAME`, `MANAGER_NAME`, `CAMPAIGN_DEADLINE`,
  `CONTENT_CADENCE`, `LEARN_FROM_DELEGATE`, `LEARN_HARVEST_HOURS`,
  `LEARN_HARVEST_MAX_EMAILS`, `SENDER_EMAIL`, `CREATOR_DB_URL`,
  `CREATOR_DB_API_KEY`, `CREATOR_DB_TIMEOUT_MS`, `OUTREACH_BOT_TOKEN`, `DRY_RUN`.
- **creator-database:** `PORT` (injected), `NODE_ENV`, `LOG_LEVEL`,
  `DATABASE_URL`, `INTERNAL_API_KEY`, `INSTANTLY_API_KEY`, `INSTANTLY_API_BASE`,
  `INSTANTLY_TIMEOUT_MS`, `INSTANTLY_CAMPAIGN_IDS`, `INSTANTLY_EACCOUNT`,
  `CLAUDE_API_KEY`, `CLAUDE_MODEL`, `CLAUDE_MAX_TOKENS`, `CLAUDE_MAX_RETRIES`,
  `ENABLE_SCHEDULER`, `CRON_OUTREACH_SYNC`, `CRON_EMAIL_SYNC`,
  `CRON_CLAUDE_EXTRACTION`, `UPCOMING_DEADLINE_DAYS`,
  `RUN_MIGRATIONS_ON_BOOT` (leave unset/true).

**Shared secrets → use Railway reference variables** so each is defined once and
referenced by the other service (prevents drift):

| Secret | Defined on | Referenced by | Must match |
| --- | --- | --- | --- |
| `BOT_TOKEN` | influence-stats | influence-bot | stats `BOT_TOKEN` ↔ bot `BOT_TOKEN` (sent as `x-bot-token`) |
| `OUTREACH_BOT_TOKEN` | outreach | influence-stats | outreach `OUTREACH_BOT_TOKEN` ↔ stats `OUTREACH_BOT_TOKEN` |
| `CREATOR_DB_API_KEY` / `INTERNAL_API_KEY` | creator-database (`INTERNAL_API_KEY`) | outreach (`CREATOR_DB_API_KEY`, sent as `x-api-key`) | the two values must be equal |
| `CAMPAIGNS_API_TOKEN` | influence-stats (`BOT_TOKEN`) | outreach (`CAMPAIGNS_API_TOKEN`) | must equal stats `BOT_TOKEN` — outreach polls stats' `/api/bot/campaigns` |

### D. Repoint internal calls to private DNS (config-only)
Set these variables so all four internal hops travel the private network. The
value is `http://<service-name>.railway.internal:<PORT>` — plain **http on the
app's own port** (not `:443`). Keep the current public-URL value written down;
it is the one-line rollback for each.

| Service | Variable | New internal value | Was (public) |
| --- | --- | --- | --- |
| influence-bot | `REELSTATS_API_URL` | `http://influence-stats.railway.internal:${PORT_of_stats}` | `https://campaign.influence.technology` |
| influence-stats | `SLACK_WEBHOOK_URL` | `http://influence-bot.railway.internal:${PORT_of_bot}/webhook` | bot public URL + `/webhook` |
| influence-stats | `OUTREACH_BASE_URL` | `http://outreach.railway.internal:${PORT_of_outreach}` | outreach public URL |
| outreach | `CAMPAIGNS_API_BASE` | `http://influence-stats.railway.internal:${PORT_of_stats}` | `https://campaigns.influence.technology` |
| outreach | `CREATOR_DB_URL` | `http://creator-database.railway.internal:${PORT_of_creatordb}` | creator-db public URL |

Notes:
- Use each target service's actual listening `PORT` (Railway assigns one per
  service; you can reference it, e.g. `${{influence-stats.PORT}}`).
- `stats → Outreach` contract **pages** are fetched server-side by stats and
  re-served under the public domain, so that hop is safe to move internal too.
- **External** callers (Slack, Instantly, creators' browsers) must keep using the
  **public custom domains** — do not move those to internal.

**Domain note (pre-existing inconsistency — verify, do not silently rename):**
`influence-stats`/`influence-bot` use **`campaign.influence.technology`**
(singular) while `Outreach`'s `CAMPAIGNS_API_BASE` points at
**`campaigns.influence.technology`** (plural). Before cutover, confirm which
hostname actually resolves in production today and keep the working one. Once
`CAMPAIGNS_API_BASE` is moved to the internal DNS name in the table above, this
particular discrepancy stops mattering for the stats↔outreach hop, but the
public domain that creators load for contract pages must remain whatever is live
today.

### E. Attach volumes and migrate data (no loss)
- **influence-stats:** attach a volume mounted at the `DATA_DIR` path. Migrate
  its JSON DB with the built-in, terminal-free tools: on the OLD service call
  `GET /api/admin/db-export` (admin auth), then on the NEW service
  `POST /api/admin/db-import` with that payload (it backs up to
  `db.backup.<ts>.json` first). Verify campaign counts match.
- **influence-bot:** attach a volume at `/data` (and set `CHAT_UPLOADS_DIR` to a
  path on that volume so chat attachments survive). During a short maintenance
  window, copy the SQLite file and attachments from the old volume to the new
  one (Railway volume snapshot/restore, or stream the file out and back). The
  bot's DB holds notification-dedup and chat state — the **source of truth for
  campaigns is stats' API**, so a brief pause here does not lose business data,
  but copying preserves dedup history so no duplicate Slack alerts fire.
- **Postgres services (outreach, creator-database):** simplest zero-loss path is
  to **reuse the existing databases** (same `DATABASE_URL`). If moving into
  project-local Postgres, `pg_dump` the old DB and `pg_restore` into the new one,
  then verify row counts before cutover. Both apps run migrations on boot
  idempotently.

### F. Blue-green cutover
1. Deploy all four new services. Confirm each `GET /health` is 200 and check the
   verification list in §7 **while the old project still serves live traffic**.
2. Move the **custom domains** to the new services: in Railway attach
   `campaign.influence.technology` (→ `influence-stats`) and
   `campaigns.influence.technology` (→ `outreach`, if that is the live contract
   host) to the new services, and update the DNS CNAME targets Railway shows.
   Because the hostnames are unchanged, **Slack and Instantly webhook
   registrations do not need editing** — they keep hitting the same public URLs,
   now served by the new services.
3. If any service is instead exposed via a Railway-generated
   `*.up.railway.app` URL that will change, update those external registrations
   (Slack app Event/Command/Interactivity/Redirect URLs; Instantly webhook URL;
   `SLACK_OAUTH_REDIRECT_URI` / `PUBLIC_BASE_URL`) — see §5.
4. Watch logs for the internal polls/webhooks succeeding (see §7).

### G. Decommission
Leave the old project running (idle) for a soak period (recommend ≥ 48h across at
least one daily-cron cycle). Only then delete the old services/volumes.

---

## 4. GitHub / CI-CD changes

- **No GitHub Actions exist** in any repo; Railway deploys directly from the
  connected branch, so there are **no pipeline files to add or change**.
- The only CI/CD-relevant change is **which branch each Railway service tracks**:
  point them at `claude/railway-repo-consolidation-ke2rlq` for the migration,
  then back to each repo's default branch (`master` for `influence-stats`,
  `main` for the others) once the consolidation PRs merge.
- Open PRs from `claude/railway-repo-consolidation-ke2rlq` → default branch in
  each repo only when you want these repo changes merged (the changes are safe to
  merge before or after the Railway steps, since they are no-ops until the
  Railway config is applied).

---

## 5. Manual actions required from you (summary checklist)

- [ ] Create the `influence-platform` Railway project; confirm private
      networking is on.
- [ ] Decide Postgres strategy (reuse existing DBs = zero data movement,
      recommended).
- [ ] Add the four services from the consolidation branch; set **outreach root
      directory = `backend`**; confirm bot stays single-worker.
- [ ] Export every env var from each old service into the new one (verbatim
      secrets); wire the four shared secrets as reference variables (§3C table).
- [ ] Repoint the five internal URL vars to `*.railway.internal` (§3D table);
      record the old public values for rollback.
- [ ] Attach + migrate volumes for stats (`DATA_DIR`, via db-export/import) and
      bot (`/data` + `CHAT_UPLOADS_DIR`, via volume copy).
- [ ] Verify §7 on the new services while the old project still serves traffic.
- [ ] Move the custom domains + update DNS CNAMEs (last step).
- [ ] Only if a public hostname changes: update Slack app URLs, Instantly webhook
      URL, `SLACK_OAUTH_REDIRECT_URI`, `PUBLIC_BASE_URL`.
- [ ] Soak ≥ 48h, then decommission the old project.

External integrations to be aware of (kept working, no re-registration needed if
domains are preserved): **Slack** app (events/commands/interactivity/OAuth) →
bot; **Instantly** webhook (`/webhook/instantly`, HMAC) → outreach; **Resend**
(verified sender domains `useinfluence.xyz` / `influence.technology`) → bot;
**Anthropic/Claude Console API** → outreach + creator-database; **Instantly API**
→ outreach + creator-database; **Apify** → stats; **PostHog** → stats frontend.

---

## 6. Rollback procedures

The migration is blue-green, so the old project is a complete, untouched fallback
until step G.

- **Bad internal URL / networking issue:** set the affected variable in the table
  in §3D back to its recorded public value; the service resumes calling over the
  public internet immediately. (No redeploy of code needed — variable change
  redeploys the service.)
- **Bad service deploy:** Railway → service → Deployments → **Redeploy** the last
  good deployment, or switch the tracked branch back.
- **Domain / cutover problem (step F):** re-attach the custom domain to the OLD
  service and revert the DNS CNAME. Because the old project was never stopped,
  traffic is restored with only DNS-propagation delay. This is the master
  rollback for the whole migration.
- **Data concern on stats/bot volumes:** the old volumes are never modified
  (migration only reads from them), so the old services keep serving correct
  data; roll back the domain to them.
- **Single-worker regression (duplicate Slack alerts):** confirm the bot service
  has exactly one web worker; `railway.json` enforces `--workers 1` — remove any
  Railway "Start Command" override that changed it, and redeploy.

---

## 7. Post-migration verification (must all pass)

- **Health:** `GET /health` returns 200 on all four services (the
  `influence-stats` route is new in this branch).
- **bot → stats (internal):** bot logs show successful `/api/bot/campaigns`
  polls (every `POLL_INTERVAL_SECONDS`) with `200`.
- **stats → bot (internal):** trigger a stats event (e.g. a creator submits
  links) and confirm bot `POST /webhook` handles it and posts to Slack.
- **outreach → stats (internal):** outreach boot log shows
  `Synced N campaigns from upstream` (its `CAMPAIGNS_API_BASE` call).
- **outreach → creator-database (internal):** exercise a signed-contract path (or
  `POST /sync/outreach` on creator-database) and confirm `x-api-key` auth
  succeeds over `*.railway.internal`.
- **External inbound:** run a Slack slash command (`/influence-status`); send an
  Instantly test webhook to `/webhook/instantly` and confirm HMAC verification +
  status advance; load a `/contract/:token` page over the public domain.
- **External outbound:** confirm a Resend email sends (bot), an Apify refresh
  runs (stats), and Claude extraction runs (creator-database Job 3 /
  outreach negotiation).
- **Schedulers:** bot APScheduler (`poll_and_check`, `daily_payment_summary`,
  `auto_approval_sweep`), outreach 5-min poller, and creator-database crons each
  log a run in the new project.
- **Data parity:** stats campaign count and creator-database creator count match
  the pre-migration numbers.

When every item above is green and the soak period has passed, the four
repositories are communicating over private networking and the unified project is
fully operational; the old project can be decommissioned.
