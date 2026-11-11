"""
Unit tests for web_app.py dashboard endpoints and database dashboard helpers.
"""

import json
import sys
import unittest
from pathlib import Path
import tempfile
import shutil
import urllib.request
import urllib.parse
from http.server import ThreadingHTTPServer
import threading
import time

# Add project root to sys.path
sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from database import Database
from models import JobPosting
from web_app import DashboardRequestHandler


class TestWebApp(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.test_dir = tempfile.mkdtemp()
        cls.db = Database(Path(cls.test_dir) / "test_web.db")

        # Seed sample postings
        j1 = JobPosting(
            id="greenhouse:celonis:101",
            title="Senior Solutions Engineer",
            company="Celonis",
            company_display="Celonis SE",
            location="Munich, Germany",
            url="https://boards.greenhouse.io/celonis/jobs/101",
            apply_url="https://boards.greenhouse.io/celonis/jobs/101#apply",
            score=88.0,
            matched_skills=["REST API", "SQL", "Pre-sales"],
            language_status="English Only",
            source="greenhouse",
            user_status="new",
        )
        j2 = JobPosting(
            id="lever:personio:202",
            title="Integration Consultant",
            company="Personio",
            company_display="Personio GmbH",
            location="Munich",
            url="https://jobs.lever.co/personio/202",
            score=72.0,
            matched_skills=["Workato", "iPaaS"],
            language_status="German is a Plus",
            source="lever",
            user_status="saved",
        )
        j3 = JobPosting(
            id="general:closed:303",
            title="Old Solutions Architect",
            company="Legacy AG",
            location="Munich",
            url="https://legacy.example/job/303",
            score=65.0,
            matched_skills=["Python"],
            language_status="English Only",
            source="general",
            user_status="archived",
            closed_at="2026-10-10T12:00:00+00:00",
        )
        cls.db.save_jobs([j1, j2, j3])

        # Start web server in background thread
        DashboardRequestHandler.db = cls.db
        cls.port = 8765
        cls.server = ThreadingHTTPServer(("127.0.0.1", cls.port), DashboardRequestHandler)
        cls.thread = threading.Thread(target=cls.server.serve_forever, daemon=True)
        cls.thread.start()
        time.sleep(0.5)

    @classmethod
    def tearDownClass(cls):
        cls.server.shutdown()
        cls.server.server_close()
        shutil.rmtree(cls.test_dir, ignore_errors=True)

    def _url(self, path: str) -> str:
        return f"http://127.0.0.1:{self.port}{path}"

    def test_get_root_serves_html(self):
        req = urllib.request.Request(self._url("/"))
        with urllib.request.urlopen(req) as resp:
            self.assertEqual(resp.status, 200)
            self.assertIn("text/html", resp.headers.get("Content-Type", ""))
            body = resp.read().decode("utf-8")
            self.assertIn("Munich Job Matcher", body)
            self.assertIn("Mattia Carone", body)

    def test_get_stats(self):
        req = urllib.request.Request(self._url("/api/stats"))
        with urllib.request.urlopen(req) as resp:
            self.assertEqual(resp.status, 200)
            data = json.loads(resp.read().decode("utf-8"))
            self.assertEqual(data["total_db"], 3)
            # j1 and j2 are open candidates (closed_at is null, score > 0, gate_rejected is null)
            self.assertEqual(data["total_candidates"], 2)
            self.assertEqual(data["english_only"], 1)
            self.assertEqual(data["german_plus"], 1)
            self.assertEqual(data["saved"], 1)

    def test_get_jobs_filter_language(self):
        # English Only filter
        url = self._url("/api/jobs?lang=English+Only")
        with urllib.request.urlopen(url) as resp:
            data = json.loads(resp.read().decode("utf-8"))
            self.assertEqual(data["total"], 1)
            self.assertEqual(data["jobs"][0]["id"], "greenhouse:celonis:101")

        # German is a Plus filter
        url = self._url("/api/jobs?lang=German+is+a+Plus")
        with urllib.request.urlopen(url) as resp:
            data = json.loads(resp.read().decode("utf-8"))
            self.assertEqual(data["total"], 1)
            self.assertEqual(data["jobs"][0]["id"], "lever:personio:202")

    def test_post_job_user_status(self):
        job_id = "greenhouse:celonis:101"
        payload = json.dumps({"status": "applied", "notes": "Applied via referral link"}).encode("utf-8")
        req = urllib.request.Request(
            self._url(f"/api/jobs/{urllib.parse.quote(job_id)}/status"),
            data=payload,
            headers={"Content-Type": "application/json"},
            method="POST",
        )
        with urllib.request.urlopen(req) as resp:
            self.assertEqual(resp.status, 200)
            res = json.loads(resp.read().decode("utf-8"))
            self.assertTrue(res["ok"])
            self.assertEqual(res["status"], "applied")

        # Verify DB reflects the change
        with self.db._get_connection() as conn:
            row = conn.execute("SELECT user_status, applied_at, notes FROM jobs WHERE id = ?", (job_id,)).fetchone()
            self.assertEqual(row["user_status"], "applied")
            self.assertIsNotNone(row["applied_at"])
            self.assertEqual(row["notes"], "Applied via referral link")


if __name__ == "__main__":
    unittest.main()
