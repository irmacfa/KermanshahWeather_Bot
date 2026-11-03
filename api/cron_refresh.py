"""Shared helpers for authenticated Vercel cron functions."""
import asyncio
import json
import os
from http.server import BaseHTTPRequestHandler


def run_job(job):
    from bots import telegram_bot as bot_module
    token = os.getenv("TELEGRAM_BOT_TOKEN")
    if not token:
        raise RuntimeError("TELEGRAM_BOT_TOKEN is not configured")
    owner_id = os.getenv("BOT_OWNER_ID")
    bot_module.OWNER_ID = int(owner_id) if owner_id else None
    from telegram.ext import Application
    from types import SimpleNamespace
    application = Application.builder().token(token).build()
    async def execute():
        await application.initialize()
        try:
            await job(SimpleNamespace(bot=application.bot))
        finally:
            await application.shutdown()
    asyncio.run(execute())


class CronHandler(BaseHTTPRequestHandler):
    job = None

    def do_GET(self):
        secret = os.getenv("CRON_SECRET")
        if not secret or self.headers.get("Authorization") != f"Bearer {secret}":
            self._respond(401, {"ok": False, "error": "Unauthorized"})
            return
        try:
            run_job(self.job)
            self._respond(200, {"ok": True})
        except Exception:
            import logging
            logging.getLogger("cron").exception("Scheduled job failed")
            self._respond(500, {"ok": False, "error": "Job failed"})

    def _respond(self, status, body):
        data = json.dumps(body).encode("utf-8")
        self.send_response(status)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(data)))
        self.end_headers()
        self.wfile.write(data)

from bots.telegram_bot import process_event_refreshes

class handler(CronHandler):
    job = staticmethod(process_event_refreshes)
