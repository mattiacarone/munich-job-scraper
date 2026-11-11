"""
Unit tests for LivenessEngine and check_single_job_liveness.
Verifies authoritative ATS and HTTP status checks, redirect rules, content markers, and DB persistence.
"""

import sys
import unittest
from datetime import datetime, timezone
from pathlib import Path
from unittest.mock import MagicMock, patch
import tempfile
import shutil
import httpx

# Add project root to sys.path
sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from database import Database
from liveness import LivenessEngine, check_single_job_liveness
from models import JobPosting


class TestLivenessEngine(unittest.TestCase):
    def setUp(self):
        self.test_dir = tempfile.mkdtemp()
        self.db = Database(Path(self.test_dir) / "test_liveness.db")

    def tearDown(self):
        shutil.rmtree(self.test_dir, ignore_errors=True)

    def test_http_404_marked_closed(self):
        client = MagicMock(spec=httpx.Client)
        mock_resp = MagicMock()
        mock_resp.status_code = 404
        client.get.return_value = mock_resp

        job = {"id": "test:job:1", "source": "lever", "url": "https://jobs.lever.co/test/123", "apply_url": None}
        jid, is_live, reason = check_single_job_liveness(client, job)
        self.assertFalse(is_live)
        self.assertIn("404", reason)

    def test_http_200_active(self):
        client = MagicMock(spec=httpx.Client)
        mock_resp = MagicMock()
        mock_resp.status_code = 200
        mock_resp.url = httpx.URL("https://jobs.lever.co/test/123")
        mock_resp.text = "<html><body>Join our team as Solutions Engineer</body></html>"
        client.get.return_value = mock_resp

        job = {"id": "test:job:2", "source": "lever", "url": "https://jobs.lever.co/test/123", "apply_url": None}
        jid, is_live, reason = check_single_job_liveness(client, job)
        self.assertTrue(is_live)
        self.assertIn("Active", reason)

    def test_page_closed_text_marker_marked_closed(self):
        client = MagicMock(spec=httpx.Client)
        mock_resp = MagicMock()
        mock_resp.status_code = 200
        mock_resp.url = httpx.URL("https://boards.greenhouse.io/test/jobs/123")
        mock_resp.text = "<html><body>This position has been closed. Thank you for your interest.</body></html>"
        client.get.return_value = mock_resp

        job = {"id": "test:job:3", "source": "general", "url": "https://example.com/jobs/123", "apply_url": None}
        jid, is_live, reason = check_single_job_liveness(client, job)
        self.assertFalse(is_live)
        self.assertIn("closed notice", reason)

    def test_redirect_to_careers_root_marked_closed(self):
        client = MagicMock(spec=httpx.Client)
        mock_resp = MagicMock()
        mock_resp.status_code = 200
        mock_resp.url = httpx.URL("https://example.com/careers")
        mock_resp.text = "<html><body>Welcome to our careers page</body></html>"
        client.get.return_value = mock_resp

        job = {"id": "test:job:4", "source": "general", "url": "https://example.com/careers/jobs-closed", "apply_url": None}
        jid, is_live, reason = check_single_job_liveness(client, job)
        self.assertFalse(is_live)
        self.assertIn("Redirected", reason)

    def test_http_403_kept_active_inconclusive(self):
        client = MagicMock(spec=httpx.Client)
        mock_resp = MagicMock()
        mock_resp.status_code = 403
        client.get.return_value = mock_resp

        job = {"id": "test:job:5", "source": "workable", "url": "https://apply.workable.com/test/j/123", "apply_url": None}
        jid, is_live, reason = check_single_job_liveness(client, job)
        self.assertTrue(is_live)
        self.assertIn("Inconclusive", reason)

    def test_engine_updates_database(self):
        posting = JobPosting(
            id="test:slug:100",
            title="Integration Consultant",
            company="Test Corp",
            location="Munich, Germany",
            url="https://example.com/jobs/100",
            score=80.0,
            matched_skills=["REST API", "MuleSoft"],
            language_status="English Only",
        )
        self.db.save_jobs([posting])

        engine = LivenessEngine(self.db, concurrency=1)
        # Mock httpx.Client in liveness.py
        with patch("liveness.httpx.Client") as mock_client_cls:
            mock_client = MagicMock()
            mock_resp = MagicMock()
            mock_resp.status_code = 404
            mock_client.get.return_value = mock_resp
            mock_client_cls.return_value.__enter__.return_value = mock_client

            results = engine.check_candidates()
            self.assertEqual(results["total"], 1)
            self.assertEqual(results["closed"], 1)

        # Verify DB closed_at is set
        with self.db._get_connection() as conn:
            row = conn.execute("SELECT closed_at FROM jobs WHERE id = ?", ("test:slug:100",)).fetchone()
            self.assertIsNotNone(row["closed_at"])


if __name__ == "__main__":
    unittest.main()
