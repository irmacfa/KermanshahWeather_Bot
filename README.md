# Kermanshah Weather Bot — Vercel test deployment

Telegram weather and flying-site forecast bot for Kermanshah province.

This branch is the isolated Vercel migration test. It is Telegram-only; the existing
production server and `main` branch remain unchanged until the migration is accepted.

## Architecture

- `api/webhook.py`: Telegram webhook endpoint (`/api/webhook`).
- `api/cron_refresh.py`: secured event-message refresh endpoint (`/api/cron_refresh`).
- `api/cron_hourly.py`: secured hourly forecast-report endpoint (`/api/cron_hourly`).
- `core/pgdb.py`: Neon PostgreSQL connection and schema initialization.
- `core/tracking.py`: PostgreSQL when `DATABASE_URL` is present; SQLite remains available for local tests.
- `core/user_state.py`: persisted per-user Telegram conversation state.
- `bots/telegram_bot.py`: shared Telegram handlers. Polling entrypoint is for local development only.

Vercel Functions are stateless. The webhook stores user conversation state in Neon and
deduplicates Telegram update IDs. Set the Telegram webhook with `max_connections=1`
to avoid overlapping updates for the same user's conversation state.

## Required Vercel environment variables

Set these on **Preview only** while testing:

- `DATABASE_URL`: Neon pooled PostgreSQL connection string.
- `TELEGRAM_BOT_TOKEN`: token for a separate test bot, never the production bot token.
- `TELEGRAM_WEBHOOK_SECRET`: random secret passed to Telegram's `setWebhook`.
- `BOT_OWNER_ID`: numeric Telegram user ID of the owner.
- `WINDY_API_KEY`: Windy API key.
- `CRON_SECRET`: random secret protecting scheduled-job endpoints.

Do not commit real credentials or put them in GitHub.

## Configure Telegram webhook

After the Preview deployment is ready, set the webhook against that deployment URL:

```bash
curl -X POST "https://api.telegram.org/bot<TEST_BOT_TOKEN>/setWebhook" \
  -d "url=https://<VERCEL_PREVIEW_HOST>/api/webhook" \
  -d "secret_token=<TELEGRAM_WEBHOOK_SECRET>" \
  -d "max_connections=1"
```

Use the test bot token and the exact Preview deployment host. Do not use the production
bot token while the production server is polling.

Check the webhook:

```bash
curl "https://api.telegram.org/bot<TEST_BOT_TOKEN>/getWebhookInfo"
```

## Scheduled jobs

The secured endpoints are:

- `GET /api/cron_refresh`
- `GET /api/cron_hourly`

They require `Authorization: Bearer <CRON_SECRET>`. Scheduling is intentionally not
enabled in `vercel.json` until the Vercel plan is confirmed: Hobby permits only one
execution per day, while Pro supports per-minute schedules. The original bot refreshes
event posts every five seconds and sends reports hourly, so the scheduler must match
the actual Vercel plan or use an external scheduler.

## Local tests

```bash
python -m unittest discover -s tests -v
python -m py_compile api/*.py bots/telegram_bot.py core/*.py
```

Without `DATABASE_URL`, tracking uses the existing SQLite backend for regression tests.
For a real Vercel preview test, use a fresh Neon database and a separate Telegram bot.
