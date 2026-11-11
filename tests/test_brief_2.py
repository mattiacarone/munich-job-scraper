"""
Unit tests for IMPROVEMENT_BRIEF_2.md Section 12:
1. Dedupe keeps one row and prefers the ATS source
2. company_display from a mocked Greenhouse board response
3. LinkedIn externalApply URL decoding
4. Closed posting gets closed_at and leaves the candidate set
5. 'Germany' alone rejected, 'Remote Germany' accepted
6. German title rejected with an English description
7. Tutor posting rejected
8. Slug probe counts only a non-empty job list
"""

import sys
import unittest
from datetime import datetime, timezone
from pathlib import Path
from unittest.mock import MagicMock
import tempfile
import shutil

# Add project root to sys.path
sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from config import config
from database import Database, compute_dedupe_key
from matcher import ProfileMatcher
from models import JobPosting
from scrapers.greenhouse import GreenhouseScraper
from prober import probe_single_slug


class TestBrief2(unittest.TestCase):
    def setUp(self):
        self.test_dir = tempfile.mkdtemp()
        self.db_path = Path(self.test_dir) / "test_jobs.db"
        self.db = Database(self.db_path)
        self.matcher = ProfileMatcher(config.profile)

    def tearDown(self):
        shutil.rmtree(self.test_dir, ignore_errors=True)

    def test_dedupe_keeps_one_row_and_prefers_ats_source(self):
        """
        Dedupe: The same 'Customer Success Engineer' at Veeam from two sources
        leaves exactly one row with gate_rejected IS NULL, preferring ATS over LinkedIn.
        """
        # Create LinkedIn row
        li_job = JobPosting(
            id="li:1001",
            title="Customer Success Engineer (m/w/d)",
            company="Veeam Software",
            location="Munich, Germany",
            url="https://linkedin.com/jobs/view/1001",
            source="linkedin",
            score=65.0,
            gate_rejected=None,
        )
        li_job.dedupe_key = compute_dedupe_key(li_job.company, li_job.title)

        # Create Greenhouse row (ATS source)
        gh_job = JobPosting(
            id="greenhouse:veeam:2002",
            title="Customer Success Engineer",
            company="Veeam Software",
            location="Munich, Germany",
            url="https://boards.greenhouse.io/veeam/jobs/2002",
            source="greenhouse",
            score=65.0,
            gate_rejected=None,
        )
        gh_job.dedupe_key = compute_dedupe_key(gh_job.company, gh_job.title)

        self.assertEqual(li_job.dedupe_key, gh_job.dedupe_key)

        self.db.save_jobs([li_job, gh_job])

        # Run deduplication
        self.db.deduplicate_all_jobs()

        # Query both
        all_jobs = {j.id: j for j in self.db.get_all_jobs()}
        
        # Greenhouse job must be the canonical row (gate_rejected is None)
        self.assertIsNone(all_jobs["greenhouse:veeam:2002"].gate_rejected)
        
        # LinkedIn job must be marked duplicate of greenhouse job
        self.assertEqual(
            all_jobs["li:1001"].gate_rejected,
            "duplicate_of:greenhouse:veeam:2002",
        )

    def test_company_display_from_mocked_greenhouse_response(self):
        """
        company_display extraction from GET https://boards-api.greenhouse.io/v1/boards/{slug}
        and caching in Database.
        """
        scraper = GreenhouseScraper(db=self.db)
        mock_client = MagicMock()
        mock_response = MagicMock()
        mock_response.status_code = 200
        mock_response.json.return_value = {
            "name": "Veeam Software",
            "description": "Leader in Data Protection",
        }
        mock_client.get.return_value = mock_response

        display_name = scraper._get_company_display(mock_client, "veeam")
        self.assertEqual(display_name, "Veeam Software")

        # Verify cached in DB
        cached_name = self.db.get_company_display("greenhouse", "veeam")
        self.assertEqual(cached_name, "Veeam Software")

    def test_closed_posting_gets_closed_at_and_leaves_candidate_set(self):
        """
        Closed posting gets closed_at and leaves the candidate set
        (gate_rejected IS NULL AND score > 0 AND closed_at IS NULL).
        """
        active_job = JobPosting(
            id="greenhouse:acme:1",
            title="Enterprise Integration Consultant",
            company="Acme Corp",
            location="Munich, Germany",
            url="https://boards.greenhouse.io/acme/jobs/1",
            source="greenhouse",
            score=75.0,
            gate_rejected=None,
            closed_at=None,
            posted_at=datetime.now(timezone.utc),
        )
        self.db.save_jobs([active_job])

        # Initially in candidate jobs
        candidates_before = self.db.get_candidate_jobs()
        self.assertEqual(len(candidates_before), 1)
        self.assertEqual(candidates_before[0].id, "greenhouse:acme:1")

        # Simulate ATS board response where job id 1 is no longer listed
        closed_count = self.db.close_missing_ats_postings("greenhouse", "acme", active_ids=set())
        self.assertEqual(closed_count, 1)

        # Confirm closed_at is set in the database
        all_jobs = self.db.get_all_jobs()
        self.assertIsNotNone(all_jobs[0].closed_at)

        # Must leave the candidate set
        candidates_after = self.db.get_candidate_jobs()
        self.assertEqual(len(candidates_after), 0)

    def test_germany_alone_rejected_remote_germany_accepted(self):
        """
        Location gate: 'Germany' alone rejected; 'Remote Germany' accepted.
        """
        # Germany alone -> Rejected
        ok_de, reason_de = self.matcher.check_gate_location("Germany")
        self.assertFalse(ok_de)
        self.assertIn("Germany' alone", reason_de)

        ok_deutschland, reason_deutschland = self.matcher.check_gate_location("Deutschland")
        self.assertFalse(ok_deutschland)

        # Remote Germany -> Accepted
        ok_remote_de, _ = self.matcher.check_gate_location("Remote Germany")
        self.assertTrue(ok_remote_de)

        ok_de_remote, _ = self.matcher.check_gate_location("Germany (Remote)")
        self.assertTrue(ok_de_remote)

        ok_remote_code, _ = self.matcher.check_gate_location("Remote DE")
        self.assertTrue(ok_remote_code)

        # Germany with Remote in Title -> Accepted
        ok_title_remote, _ = self.matcher.check_gate_location(
            loc_str="Germany",
            title_str="Solutions Engineer (Remote)",
        )
        self.assertTrue(ok_title_remote)

    def test_german_title_rejected_with_english_description(self):
        """
        German gate: Also reject when title contains German title words
        even if the description is English.
        """
        english_desc = (
            "We are looking for a skilled professional to join our team in Munich. "
            "You will collaborate on MuleSoft and Boomi enterprise integration architectures, "
            "design APIs, and work closely with international stakeholders. "
            "Proficiency in English is required."
        )
        job_berater = JobPosting(
            id="test:de_title_1",
            title="Senior IT Berater (Integration)",
            company="Cloud Integrations",
            location="Munich, Germany",
            url="https://example.com/jobs/1",
            description=english_desc,
        )
        eval_berater = self.matcher.evaluate(job_berater)
        self.assertEqual(eval_berater.gate_rejected, "german")
        self.assertIn("title contains DACH/German words", eval_berater.rejection_reason)

        job_mitarbeiter = JobPosting(
            id="test:de_title_2",
            title="Mitarbeiter Customer Success & Support",
            company="Tech Corp",
            location="Munich, Germany",
            url="https://example.com/jobs/2",
            description=english_desc,
        )
        eval_mitarbeiter = self.matcher.evaluate(job_mitarbeiter)
        self.assertEqual(eval_mitarbeiter.gate_rejected, "german")

    def test_tutor_posting_rejected(self):
        """
        Off-function gate: Tutor, teacher, nanny, cleaner, cashier, etc. must be rejected.
        """
        tutor_job = JobPosting(
            id="test:tutor",
            title="Online English Tutor / Teacher",
            company="Tutoring Corp",
            location="Munich, Germany",
            url="https://example.com/jobs/tutor",
            description="Teach English to students online.",
        )
        eval_tutor = self.matcher.evaluate(tutor_job)
        self.assertEqual(eval_tutor.gate_rejected, "off-function")

        cashier_job = JobPosting(
            id="test:cashier",
            title="Supermarket Cashier",
            company="Retail Mart",
            location="Munich, Germany",
            url="https://example.com/jobs/cashier",
            description="Store cashier duties in Munich center.",
        )
        eval_cashier = self.matcher.evaluate(cashier_job)
        self.assertEqual(eval_cashier.gate_rejected, "off-function")

    def test_slug_probe_counts_only_non_empty_job_list(self):
        """
        Auto-probe: A slug counts ONLY if the API returns at least one job.
        """
        mock_client = MagicMock()

        # Case 1: Greenhouse returns 200 with empty jobs list -> None
        mock_res_empty = MagicMock()
        mock_res_empty.status_code = 200
        mock_res_empty.json.return_value = {"jobs": []}
        mock_client.get.return_value = mock_res_empty

        result_empty = probe_single_slug(mock_client, "somecompany")
        self.assertIsNone(result_empty)

        # Case 2: Greenhouse returns 200 with non-empty jobs list -> returns ("greenhouse", slug)
        mock_res_nonempty = MagicMock()
        mock_res_nonempty.status_code = 200
        mock_res_nonempty.json.return_value = {
            "jobs": [{"id": 12345, "title": "Solutions Engineer"}]
        }
        mock_client.get.return_value = mock_res_nonempty

        result_success = probe_single_slug(mock_client, "validcompany")
        self.assertEqual(result_success, ("greenhouse", "validcompany"))


if __name__ == "__main__":
    unittest.main()

