"""Telegram webhook endpoint for Vercel's Python Functions runtime."""
import asyncio
import json
import logging
import os
from http.server import BaseHTTPRequestHandler

from telegram import Update
from telegram.ext import Application

from core.pgdb import claim_update, complete_update, release_update, load_user_state, save_user_state
from types import SimpleNamespace

logger = logging.getLogger("telegram-webhook")


def _application() -> Application:
    from bots import telegram_bot as bot_module
    token = os.getenv("TELEGRAM_BOT_TOKEN")
    if not token:
        raise RuntimeError("TELEGRAM_BOT_TOKEN is not configured")
    owner_id = os.getenv("BOT_OWNER_ID")
    bot_module.OWNER_ID = int(owner_id) if owner_id else None
    application = Application.builder().token(token).build()
    bot_module.configure_application(application)
    return application


async def _process(payload: dict) -> None:
    application = _application()
    update = Update.de_json(payload, application.bot)
    if update is None:
        raise ValueError("Could not decode Telegram update")
    user = update.effective_user
    if user is not None:
        application.user_data[user.id].update(load_user_state(user.id))
    try:
        await application.initialize()
        await application.start()
        await application.process_update(update)
        # Refresh queued event posts immediately after the update that changed them.
        from bots.telegram_bot import process_event_refreshes
        await process_event_refreshes(SimpleNamespace(bot=application.bot))
        if user is not None:
            save_user_state(user.id, dict(application.user_data[user.id]))
    finally:
        try:
            if application.running:
                await application.stop()
        finally:
            await application.shutdown()


class handler(BaseHTTPRequestHandler):
    def _respond(self, status: int, body: dict):
        data = json.dumps(body).encode("utf-8")
        self.send_response(status)
        self.send_header("Content-Type", "application/json; charset=utf-8")
        self.send_header("Content-Length", str(len(data)))
        self.end_headers()
        self.wfile.write(data)

    def do_GET(self):
        self._respond(200, {"ok": True, "service": "telegram-webhook"})

    def do_POST(self):
        expected = os.getenv("TELEGRAM_WEBHOOK_SECRET")
        supplied = self.headers.get("X-Telegram-Bot-Api-Secret-Token")
        if not expected:
            self._respond(503, {"ok": False, "error": "Webhook secret is not configured"})
            return
        if supplied != expected:
            self._respond(403, {"ok": False, "error": "Forbidden"})
            return
        try:
            length = int(self.headers.get("Content-Length", "0"))
            if length <= 0 or length > 1_000_000:
                self._respond(400, {"ok": False, "error": "Invalid request size"})
                return
            payload = json.loads(self.rfile.read(length))
            update_id = payload.get("update_id")
            if not isinstance(update_id, int):
                self._respond(400, {"ok": False, "error": "Invalid update_id"})
                return
            if not claim_update(update_id):
                self._respond(200, {"ok": True, "duplicate": True})
                return
            try:
                asyncio.run(_process(payload))
                complete_update(update_id)
            except Exception:
                logger.exception("Telegram update %s failed", update_id)
                release_update(update_id)
                self._respond(500, {"ok": False, "error": "Update processing failed"})
                return
            self._respond(200, {"ok": True})
        except (ValueError, json.JSONDecodeError):
            self._respond(400, {"ok": False, "error": "Invalid JSON payload"})
        except Exception:
            logger.exception("Webhook request failed")
            self._respond(500, {"ok": False, "error": "Internal server error"})
