# Setting up a persistent database on Railway (Postgres)

By default the bot uses SQLite, stored as a file inside the container. On
Railway that file lives on the **ephemeral filesystem**, so it is wiped on
every redeploy. That loses brand installs, payment history, email dedup, chat
spaces, and notification state.

Switching to Railway Postgres makes all of that persist across redeploys. The
code already supports it (`psycopg2-binary` is in `requirements.txt`, and the
schema/migrations work on Postgres) — you only need to provision the database
and point the bot at it.

## Steps

1. **Add a Postgres database**
   - Open your project in the Railway dashboard.
   - Click **New** → **Database** → **Add PostgreSQL**.
   - Railway creates a Postgres service with its own `DATABASE_URL`.

2. **Point the bot at Postgres**
   - Open your **bot service** (the one running this repo) → **Variables**.
   - Add a variable named `DATABASE_URL` with the value:
     ```
     ${{Postgres.DATABASE_URL}}
     ```
     (If your Postgres service has a different name, use that name instead of
     `Postgres`. Railway autocompletes the reference as you type `${{`.)

3. **Redeploy**
   - Save the variable. Railway redeploys the bot automatically.
   - On boot the bot connects to Postgres and creates all tables
     (`init_db()` runs at startup).

4. **Verify**
   - Check the deploy logs — you should see the bot start with no database
     errors, followed by:
     `Notification baseline recorded silently (...) — suppressing pre-existing
     notifications for this deploy.`
   - This baseline runs **once** on Postgres (the watermark now persists), so
     the very first deploy after switching won't spam the workspace, and later
     redeploys stay quiet.

## Notes

- **No code changes needed.** The bot reads `DATABASE_URL` and connects. It
  even normalizes the legacy `postgres://` scheme to `postgresql://`
  automatically, and enables `pool_pre_ping` so idle Postgres connections
  don't cause errors.
- **Existing SQLite data is not migrated.** The current SQLite data is
  ephemeral anyway, so there's nothing worth moving — brands simply re-install
  once against the new persistent database, and it sticks from then on.
- **Local development** still uses SQLite by default (no `DATABASE_URL` set),
  so nothing changes on your machine.
