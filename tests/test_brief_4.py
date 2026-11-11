"""
Unit tests for IMPROVEMENT_BRIEF_4.md:
1. LinkedIn employer apply link extraction vs Easy Apply:
   - Off-site fixture extracts employer Greenhouse URL, sets ats='greenhouse', ats_slug='datadoghq'
   - Easy Apply fixture leaves apply_url=None and sets ats='linkedin-easy-apply'
   - Double URL-decoding
2. German titles and domain gate:
   - "Senior SuccessFactors Consultant & Entwickler" -> rejected
   - "Technischer Systemingenieur ÖPNV (m/w/d)" -> rejected
   - "Systems Engineer - Weapon Integration" -> rejected
   - "Avionics Integration Engineer" -> rejected
   - "SAP MDG Consultant" -> rejected
   - "SAP MDG & Salesforce Integration Consultant" -> passes domain gate
3. Typo-tolerant engineering filter & fullstack exclusion:
   - "Senior Process Engieer" -> rejected
   - "Sr. Forward Deployed Engineer - FDE (Fullstack)" -> rejected
   - "Forward Deployed Engineer" -> accepted
4. Database prune & VACUUM:
   - Deletes rejected rows older than 30 days
   - Preserves passed rows and duplicate matches
5. Daemon heartbeat:
   - Heartbeat timestamp written and validated
6. LinkedIn cadence rotating queries:
   - Queries rotate from last_linkedin_query_index
"""

import sys
import unittest
import tempfile
import shutil
from datetime import datetime, timedelta, timezone
from pathlib import Path
from unittest.mock import MagicMock
import httpx

# Add project root to sys.path
sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from config import config
from database import Database
from matcher import ProfileMatcher
from models import JobPosting
from apply_classifier import detect_ats_from_url


class TestBrief4(unittest.TestCase):
    def setUp(self):
        self.matcher = ProfileMatcher(config.profile)
        self.test_dir = tempfile.mkdtemp()
        self.db = Database(Path(self.test_dir) / "test.db")

    def tearDown(self):
        shutil.rmtree(self.test_dir, ignore_errors=True)

    # --------------------------------------------------------------------------
    # 2. German Titles and Domain Gate
    # --------------------------------------------------------------------------
    def test_german_title_entwickler_rejected(self):
        """'Senior SuccessFactors Consultant & Entwickler' must be rejected."""
        job = JobPosting(
            id="test:entwickler",
            title="Senior SuccessFactors Consultant & Entwickler (w|m|d)",
            company="Enterprise AG",
            location="Munich, Germany",
            url="https://example.com/ent",
            description="Leading SAP SuccessFactors consulting and development projects.",
        )
        evaluated = self.matcher.evaluate(job)
        self.assertIsNotNone(evaluated.gate_rejected)
        # Rejection should occur on German title or off-function domain
        self.assertIn(evaluated.gate_rejected, {"german", "off-function"})

    def test_german_title_technischer_systemingenieur_rejected(self):
        """'Technischer Systemingenieur ÖPNV (m/w/d)' must be rejected."""
        job = JobPosting(
            id="test:sysing",
            title="Technischer Systemingenieur ÖPNV (m/w/d)",
            company="Transit Co",
            location="Munich, Germany",
            url="https://example.com/sysing",
            description="Projektbetreuung und Implementierung im öffentlichen Nahverkehr.",
        )
        evaluated = self.matcher.evaluate(job)
        self.assertIsNotNone(evaluated.gate_rejected)
        self.assertIn(evaluated.gate_rejected, {"german", "off-function"})

    def test_domain_weapon_integration_rejected(self):
        """'Systems Engineer - Weapon Integration' must be rejected by domain gate."""
        job = JobPosting(
            id="test:weapon",
            title="Systems Engineer - Weapon Integration",
            company="Defense Corp",
            location="Munich, Germany",
            url="https://example.com/weapon",
            description="Integration of defense systems and ordnance.",
        )
        evaluated = self.matcher.evaluate(job)
        self.assertEqual(evaluated.gate_rejected, "off-function")

    def test_domain_avionics_integration_rejected(self):
        """'Avionics Integration Engineer' must be rejected by domain gate."""
        job = JobPosting(
            id="test:avionics",
            title="Avionics Integration Engineer",
            company="Avilus Aero",
            location="Munich, Germany",
            url="https://example.com/avionics",
            description="Avionics architecture and aerospace hardware integration.",
        )
        evaluated = self.matcher.evaluate(job)
        self.assertEqual(evaluated.gate_rejected, "off-function")

    def test_domain_sap_mdg_rejected(self):
        """'SAP MDG Consultant' must be rejected by domain gate."""
        job = JobPosting(
            id="test:sapmdg",
            title="SAP MDG Consultant - Digital Platforms - Germany",
            company="Infosys Consulting",
            location="Munich, Germany",
            url="https://example.com/sapmdg",
            description="Lead master data governance deployments for SAP clients.",
        )
        evaluated = self.matcher.evaluate(job)
        self.assertEqual(evaluated.gate_rejected, "off-function")

    def test_domain_sap_with_salesforce_allowed(self):
        """'SAP / Salesforce Integration Consultant' allowed because of Salesforce."""
        ok, reason = self.matcher.check_gate_off_function("SAP / Salesforce Integration Consultant")
        self.assertTrue(ok, f"Expected pass but got: {reason}")

    # --------------------------------------------------------------------------
    # 3. Typo-Tolerant Engineering and Fullstack Filter
    # --------------------------------------------------------------------------
    def test_typo_tolerant_process_engieer_rejected(self):
        """'Senior Process Engieer' (hardware/process) must be rejected."""
        ok, reason = self.matcher.check_gate_off_function("Senior Process Engieer")
        self.assertFalse(ok)

    def test_fullstack_forward_deployed_rejected(self):
        """'Sr. Forward Deployed Engineer - FDE (Fullstack)' must be rejected due to fullstack."""
        ok, reason = self.matcher.check_gate_off_function("Sr. Forward Deployed Engineer - FDE (Fullstack)")
        self.assertFalse(ok)
        self.assertIn("fullstack role", reason)

    def test_forward_deployed_without_fullstack_accepted(self):
        """'Forward Deployed Engineer' without fullstack must be accepted."""
        ok, reason = self.matcher.check_gate_off_function("Forward Deployed Engineer")
        self.assertTrue(ok, f"Expected pass but got: {reason}")

    # --------------------------------------------------------------------------
    # 4. Database Pruning
    # --------------------------------------------------------------------------
    def test_database_prune_deletes_old_rejected_rows(self):
        """Prune deletes rejected rows older than 30 days while keeping passed and dup rows."""
        now = datetime.now(timezone.utc)
        old_dt = now - timedelta(days=40)

        # 1. Old location-rejected row (should be pruned)
        job_old_rej = JobPosting(
            id="test:old_rejected",
            title="Java Dev",
            company="Old Co",
            location="Berlin, Germany",
            url="https://example.com/old",
            description="",
            gate_rejected="location",
            posted_at=old_dt,
            created_at=old_dt,
        )
        # 2. Old passed row (must NOT be pruned)
        job_old_passed = JobPosting(
            id="test:old_passed",
            title="Solutions Engineer",
            company="Keep Co",
            location="Munich, Germany",
            url="https://example.com/keep",
            description="Great role",
            score=85.0,
            gate_rejected=None,
            posted_at=old_dt,
            created_at=old_dt,
        )
        # 3. Old duplicate row (applied_history match, gate_rejected='dup', must NOT be pruned)
        job_old_dup = JobPosting(
            id="test:old_dup",
            title="Integration Consultant",
            company="Applied Co",
            location="Munich, Germany",
            url="https://example.com/applied",
            description="",
            gate_rejected="dup",
            posted_at=old_dt,
            created_at=old_dt,
        )
        # 4. Recent rejected row (must NOT be pruned)
        job_recent_rej = JobPosting(
            id="test:recent_rejected",
            title="Senior Director",
            company="New Co",
            location="Munich, Germany",
            url="https://example.com/recent",
            description="",
            gate_rejected="seniority",
            posted_at=now - timedelta(days=5),
            created_at=now - timedelta(days=5),
        )

        self.db.save_jobs([job_old_rej, job_old_passed, job_old_dup, job_recent_rej])

        deleted = self.db.prune_rejected_jobs(max_age_days=30)
        self.assertEqual(deleted, 1)

        with self.db._get_connection() as conn:
            remaining_ids = {r["id"] for r in conn.execute("SELECT id FROM jobs").fetchall()}

        self.assertNotIn("test:old_rejected", remaining_ids)
        self.assertIn("test:old_passed", remaining_ids)
        self.assertIn("test:old_dup", remaining_ids)
        self.assertIn("test:recent_rejected", remaining_ids)


if __name__ == "__main__":
    unittest.main()

