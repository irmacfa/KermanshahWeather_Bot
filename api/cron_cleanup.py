"""Secured cron endpoint for delayed Telegram message cleanup."""
import asyncio
import json
import logging
import os
from http.server import BaseHTTPRequestHandler
from types import SimpleNamespace

from telegram.ext import Application

from bots.telegram_bot import process_scheduled_deletions


class handler(BaseHTTPRequestHandler):
    def _respond(self, status, body):
        data = json.dumps(body).encode("utf-8")
        self.send_response(status)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(data)))
        self.end_headers()
        self.wfile.write(data)

    def do_GET(self):
        secret = os.getenv("CRON_SECRET")
        if not secret or self.headers.get("Authorization") != f"Bearer {secret}":
            self._respond(401, {"ok": False, "error": "Unauthorized"})
            return
        token = os.getenv("TELEGRAM_BOT_TOKEN")
        if not token:
            self._respond(503, {"ok": False, "error": "Bot token is missing"})
            return
        application = Application.builder().token(token).build()

        async def execute():
            await application.initialize()
            try:
                await process_scheduled_deletions(SimpleNamespace(bot=application.bot))
            finally:
                await application.shutdown()

        try:
            asyncio.run(execute())
            self._respond(200, {"ok": True})
        except Exception:
            logging.getLogger("cron").exception("Scheduled deletion cleanup failed")
            self._respond(500, {"ok": False, "error": "Cleanup failed"})
