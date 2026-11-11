"""
Notification dispatcher supporting Telegram, Discord, Slack, and console feeds.
Sends only canonical rows (after dedupe), score >= 60, posted <= 3 days, not closed,
max 10 per cycle.
Message format: company_display, title, location, score, apply_url or url.
"""

import logging
import sys
from datetime import datetime, timezone
from typing import Optional
import httpx
from config import AppConfig
from models import JobPosting

logger = logging.getLogger(__name__)


class Notifier:
    def __init__(self, config: AppConfig):
        self.config = config

    def notify(self, jobs: list[JobPosting]) -> None:
        """Dispatches job alerts to all configured channels (max 10)."""
        if not jobs:
            return

        now = datetime.now(timezone.utc)
        qualified: list[JobPosting] = []

        for j in jobs:
            # 1. Canonical rows only (after dedupe)
            if j.gate_rejected is not None:
                continue
            # 2. Not closed
            if j.closed_at is not None:
                continue
            # 3. Score >= 60
            if j.score < 60.0:
                continue
            # 4. Posted <= 3 days (or created_at <= 3 days)
            ref_dt = j.posted_at or j.created_at
            if ref_dt:
                tz_ref = ref_dt if ref_dt.tzinfo else ref_dt.replace(tzinfo=timezone.utc)
                age_days = (now - tz_ref).total_seconds() / 86400.0
                if age_days > 3.0:
                    continue

            qualified.append(j)
            if len(qualified) >= 10:
                break

        if not qualified:
            logger.info("[Notifier] No jobs met alert criteria (canonical, score >= 60, posted <= 3 days, not closed).")
            return

        for job in qualified:
            self._log_console(job)
            if self.config.telegram_bot_token and self.config.telegram_chat_id:
                self._send_telegram_message(job)
            if self.config.discord_webhook_url:
                self._send_discord_webhook(job)
            if self.config.slack_webhook_url:
                self._send_slack_webhook(job)

    def _log_console(self, job: JobPosting) -> None:
        company = job.company_display or job.company
        link = job.apply_url or job.url
        banner = (
            f"\n🔥 [ALERT] {job.title} | {company}\n"
            f"   Location: {job.location}\n"
            f"   Score:    {job.score}\n"
            f"   URL:      {link}\n"
        )
        try:
            print(banner)
        except UnicodeEncodeError:
            print(banner.encode(sys.stdout.encoding, errors="replace").decode(sys.stdout.encoding))

    def _send_telegram_message(self, job: JobPosting) -> None:
        company = job.company_display or job.company
        link = job.apply_url or job.url
        text = (
            f"💼 *{job.title}* at *{company}*\n"
            f"📍 {job.location}\n"
            f"⭐ Score: {round(job.score, 1)}\n"
            f"🔗 {link}"
        )
        url = f"https://api.telegram.org/bot{self.config.telegram_bot_token}/sendMessage"
        payload = {
            "chat_id": self.config.telegram_chat_id,
            "text": text,
            "parse_mode": "Markdown",
            "disable_web_page_preview": False,
        }
        try:
            with httpx.Client(timeout=10.0) as client:
                res = client.post(url, json=payload)
                if res.status_code != 200:
                    logger.error(f"Telegram alert failed: {res.status_code} - {res.text}")
        except Exception as exc:
            logger.error(f"Failed to deliver Telegram notification: {exc}")

    def _send_discord_webhook(self, job: JobPosting) -> None:
        company = job.company_display or job.company
        link = job.apply_url or job.url
        payload = {
            "content": f"💼 **{job.title}** at **{company}**\n📍 {job.location} | ⭐ Score: {round(job.score, 1)}\n🔗 {link}"
        }
        try:
            with httpx.Client(timeout=10.0) as client:
                res = client.post(self.config.discord_webhook_url, json=payload)
                if res.status_code not in (200, 204):
                    logger.error(f"Discord webhook failed: {res.status_code} - {res.text}")
        except Exception as exc:
            logger.error(f"Failed to deliver Discord notification: {exc}")

    def _send_slack_webhook(self, job: JobPosting) -> None:
        company = job.company_display or job.company
        link = job.apply_url or job.url
        payload = {
            "text": f"*{job.title}* at *{company}*\nLocation: {job.location} | Score: {round(job.score, 1)}\nApply: <{link}>"
        }
        try:
            with httpx.Client(timeout=10.0) as client:
                res = client.post(self.config.slack_webhook_url, json=payload)
                if res.status_code != 200:
                    logger.error(f"Slack webhook failed: {res.status_code} - {res.text}")
        except Exception as exc:
            logger.error(f"Failed to deliver Slack notification: {exc}")
