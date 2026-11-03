"""Vercel ASGI entrypoint for the Telegram test bot."""
from __future__ import annotations

import logging
import os

from fastapi import FastAPI, Header, HTTPException, Request
from types import SimpleNamespace
from telegram import Update
from telegram.ext import Application

from core.pgdb import claim_update, load_user_state, save_user_state

logging.basicConfig(level=logging.INFO)
logger = logging.getLogger("telegram-webhook")
app = FastAPI(title="Kermanshah Weather Telegram Webhook")


def _build_application() -> Application:
    from bots import telegram_bot as bot_module

    token = os.getenv("TELEGRAM_BOT_TOKEN")
    if not token:
        raise RuntimeError("TELEGRAM_BOT_TOKEN is not configured")
    owner_id = os.getenv("BOT_OWNER_ID")
    bot_module.OWNER_ID = int(owner_id) if owner_id else None
    application = Application.builder().token(token).build()
    bot_module.configure_application(application)
    return application


@app.get("/")
async def health():
    return {"ok": True, "service": "kermanshah-weather-telegram-webhook"}


@app.post("/webhook")
async def telegram_webhook(
    request: Request,
    x_telegram_bot_api_secret_token: str | None = Header(default=None),
):
    expected = os.getenv("TELEGRAM_WEBHOOK_SECRET")
    if not expected:
        logger.error("TELEGRAM_WEBHOOK_SECRET is missing")
        raise HTTPException(status_code=503, detail="Webhook is not configured")
    if not x_telegram_bot_api_secret_token or x_telegram_bot_api_secret_token != expected:
        raise HTTPException(status_code=403, detail="Forbidden")

    payload = await request.json()
    update_id = payload.get("update_id")
    if not isinstance(update_id, int):
        raise HTTPException(status_code=400, detail="Invalid Telegram update")
    if not claim_update(update_id):
        return {"ok": True, "duplicate": True}

    application = _build_application()
    update = Update.de_json(payload, application.bot)
    if update is None:
        raise HTTPException(status_code=400, detail="Could not decode update")
    user = update.effective_user
    if user is not None:
        application.user_data[user.id].update(load_user_state(user.id))

    try:
        await application.initialize()
        await application.start()
        await application.process_update(update)
        if user is not None:
            save_user_state(user.id, dict(application.user_data[user.id]))
    except Exception:
        logger.exception("Telegram update processing failed: %s", update_id)
        # Telegram can retry failed updates; remove the claim so it can be retried.
        from core.pgdb import connect
        with connect() as conn:
            conn.execute("DELETE FROM processed_updates WHERE update_id = ?", (update_id,))
        raise HTTPException(status_code=500, detail="Update processing failed")
    finally:
        try:
            if application.running:
                await application.stop()
            await application.shutdown()
        except Exception:
            logger.exception("Application shutdown failed")
    return {"ok": True}


async def _run_scheduled_job(job):
    application = _build_application()
    await application.initialize()
    try:
        await job(SimpleNamespace(bot=application.bot))
    finally:
        await application.shutdown()


def _authorize_cron(authorization: str | None):
    secret = os.getenv("CRON_SECRET")
    if not secret or authorization != f"Bearer {secret}":
        raise HTTPException(status_code=401, detail="Unauthorized")


@app.get("/cron/refresh")
async def cron_refresh(authorization: str | None = Header(default=None)):
    _authorize_cron(authorization)
    from bots.telegram_bot import process_event_refreshes
    await _run_scheduled_job(process_event_refreshes)
    return {"ok": True, "job": "event-refresh"}


@app.get("/cron/hourly")
async def cron_hourly(authorization: str | None = Header(default=None)):
    _authorize_cron(authorization)
    from bots.telegram_bot import hourly_report_job
    await _run_scheduled_job(hourly_report_job)
    return {"ok": True, "job": "hourly-report"}
