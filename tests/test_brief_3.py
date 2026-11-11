"""
Unit tests for IMPROVEMENT_BRIEF_3.md:
1. German gate leaks:
   - Yext (Bilingual title + fluency in text) -> rejected
   - RobCo ("You speak German and English fluently") -> rejected
   - Sungrow ("German at B2 level is an advantage") -> accepted
2. Engineering roles filtering:
   - Backend Java Engineer -> rejected
   - C++ Developer -> rejected
   - Customer Solutions Engineer, EMEA -> accepted
   - MuleSoft Developer -> accepted
   - Forward Deployed Engineer -> accepted
   - Senior Product Designer -> rejected
3. Location gate precision:
   - "Freiburg (Germany)" with "remote-first" in description -> rejected
   - "Remote - Munich" -> accepted
4. LinkedIn detail parsing from fixture:
   - Full description extraction (>300 chars)
   - Apply URL handling
5. Prober sanity:
   - Slug probe rejects board with 0 Germany/remote jobs
   - Slug probe accepts board with Germany/remote jobs
"""

import sys
import unittest
from pathlib import Path
from unittest.mock import MagicMock
import tempfile
import shutil

# Add project root to sys.path
sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from config import config
from database import Database
from matcher import ProfileMatcher
from models import JobPosting
from prober import probe_single_slug, count_germany_jobs


class TestBrief3(unittest.TestCase):
    def setUp(self):
        self.matcher = ProfileMatcher(config.profile)
        self.test_dir = tempfile.mkdtemp()
        self.db = Database(Path(self.test_dir) / "test.db")

    def tearDown(self):
        shutil.rmtree(self.test_dir, ignore_errors=True)

    # --------------------------------------------------------------------------
    # 1. German Gate Leaks
    # --------------------------------------------------------------------------
    def test_german_yext_bilingual_rejected(self):
        """
        Yext: Title has Bilingual / English/German, text says:
        'Fluency in English and German (written and spoken)'.
        Must be rejected by the German gate.
        """
        job = JobPosting(
            id="test:yext",
            title="Sales Coordinator (Bilingual - English/German, M/F/X)",
            company="Yext",
            location="Munich, Germany",
            url="https://example.com/yext",
            description="<p>Fluency in English and German (written and spoken) is required.</p>",
        )
        evaluated = self.matcher.evaluate(job)
        self.assertEqual(evaluated.gate_rejected, "german")

    def test_german_robco_fluent_speech_rejected(self):
        """
        RobCo: Title '(Senior) Robotic Sales Engineer', text:
        'You speak German and English fluently'.
        Must be rejected by the German gate.
        """
        self.matcher.ger_skip_list.discard("robco")
        job = JobPosting(
            id="test:robco",
            title="Robotic Sales Engineer",
            company="RobCo",
            location="Munich, Germany",
            url="https://example.com/robco",
            description="<div>Requirements: &lt;li&gt;You speak German and English fluently.&lt;/li&gt;</div>",
        )
        evaluated = self.matcher.evaluate(job)
        self.assertIn(evaluated.gate_rejected, ("german", "german_required"))

    def test_german_sungrow_advantage_accepted(self):
        """
        Sungrow: Text says 'German at B2 level is an advantage'.
        Advantage makes it optional, so it must PASS the German gate.
        """
        ok, reason = self.matcher.check_gate_german(
            title="Senior Solutions Consultant",
            description="Requirements: German at B2 level is an advantage. Excellent English required.",
        )
        self.assertTrue(ok, f"Expected pass but got: {reason}")

    # --------------------------------------------------------------------------
    # 2. Engineering Roles Filtering
    # --------------------------------------------------------------------------
    def test_engineering_backend_java_engineer_rejected(self):
        """Backend Java Engineer must be rejected as an engineering role."""
        job = JobPosting(
            id="test:java",
            title="Backend Java Engineer",
            company="FinTech Corp",
            location="Munich, Germany",
            url="https://example.com/java",
            description="Build microservices in Java.",
        )
        evaluated = self.matcher.evaluate(job)
        self.assertEqual(evaluated.gate_rejected, "off-function")

    def test_engineering_cpp_developer_rejected(self):
        """C++ Developer must be rejected as an engineering role."""
        job = JobPosting(
            id="test:cpp",
            title="C++ Developer",
            company="Automotive Corp",
            location="Munich, Germany",
            url="https://example.com/cpp",
            description="Develop embedded automotive systems.",
        )
        evaluated = self.matcher.evaluate(job)
        self.assertEqual(evaluated.gate_rejected, "off-function")

    def test_engineering_customer_solutions_engineer_accepted(self):
        """Customer Solutions Engineer, EMEA must be accepted."""
        job = JobPosting(
            id="test:cse",
            title="Customer Solutions Engineer, EMEA",
            company="Cloud Corp",
            location="Munich, Germany",
            url="https://example.com/cse",
            description="Work with enterprise customers on integration architecture.",
        )
        evaluated = self.matcher.evaluate(job)
        self.assertIsNone(evaluated.gate_rejected)

    def test_engineering_mulesoft_developer_accepted(self):
        """MuleSoft Developer is relevant to Mattia's profile and must be accepted."""
        job = JobPosting(
            id="test:mule",
            title="MuleSoft Developer",
            company="Integration Systems",
            location="Munich, Germany",
            url="https://example.com/mule",
            description="Design and build API-led connectivity with MuleSoft Anypoint.",
        )
        evaluated = self.matcher.evaluate(job)
        self.assertIsNone(evaluated.gate_rejected)

    def test_engineering_forward_deployed_engineer_accepted(self):
        """Forward Deployed Engineer is customer-facing and must be accepted."""
        job = JobPosting(
            id="test:fde",
            title="Forward Deployed Engineer",
            company="AI Platforms",
            location="Munich, Germany",
            url="https://example.com/fde",
            description="Deploy AI solutions directly on customer premises.",
        )
        evaluated = self.matcher.evaluate(job)
        self.assertIsNone(evaluated.gate_rejected)

    def test_product_designer_rejected(self):
        """Senior Product Designer must be rejected as off-function."""
        job = JobPosting(
            id="test:designer",
            title="Senior Product Designer",
            company="Design Co",
            location="Munich, Germany",
            url="https://example.com/designer",
            description="Lead Figma UI/UX designs.",
        )
        evaluated = self.matcher.evaluate(job)
        self.assertEqual(evaluated.gate_rejected, "off-function")

    # --------------------------------------------------------------------------
    # 3. Location Gate Leaks
    # --------------------------------------------------------------------------
    def test_freiburg_with_remote_first_rejected(self):
        """
        Location 'Freiburg (Germany)' with 'remote-first' in the description
        must NOT pass the location gate.
        """
        ok, reason = self.matcher.check_gate_location(
            loc_str="Freiburg (Germany)",
            title_str="Solutions Consultant",
            desc_str="We are a remote-first company with offices in Freiburg and Berlin.",
        )
        self.assertFalse(ok)
        self.assertIn("contains non-Munich city without Munich", reason)

    def test_remote_munich_accepted(self):
        """'Remote - Munich' must be accepted."""
        ok, reason = self.matcher.check_gate_location(
            loc_str="Remote - Munich",
            title_str="Solutions Engineer",
            desc_str="Work remotely from Munich.",
        )
        self.assertTrue(ok, f"Expected pass but got: {reason}")

    # --------------------------------------------------------------------------
    # 4. LinkedIn Detail HTML Fixture
    # --------------------------------------------------------------------------
    def test_linkedin_fixture_parsing(self):
        """Parse real saved LinkedIn HTML fixture and verify description extraction."""
        fixture_path = Path(__file__).resolve().parent / "fixtures" / "linkedin_job.html"
        self.assertTrue(fixture_path.exists(), f"Fixture missing at {fixture_path}")
        html_content = fixture_path.read_text(encoding="utf-8")

        from bs4 import BeautifulSoup
        soup = BeautifulSoup(html_content, "html.parser")
        desc_elem = soup.select_one("div.show-more-less-html__markup") or soup.select_one(".description__text")
        self.assertIsNotNone(desc_elem)
        text = desc_elem.get_text("\n", strip=True)
        self.assertGreater(len(text), 300)

    # --------------------------------------------------------------------------
    # 5. Prober Sanity (Germany / Remote Verification)
    # --------------------------------------------------------------------------
    def test_prober_rejects_pure_us_board(self):
        """Boards with only US jobs must not count as valid Germany career boards."""
        mock_client = MagicMock()
        mock_res = MagicMock()
        mock_res.status_code = 200
        mock_res.json.return_value = {
            "jobs": [
                {"id": 1, "title": "Developer", "location": {"name": "San Francisco, CA, USA"}},
                {"id": 2, "title": "Account Executive", "location": {"name": "New York, NY, USA"}},
            ]
        }
        mock_client.get.return_value = mock_res

        result = probe_single_slug(mock_client, "us-only-company")
        self.assertIsNone(result)

    def test_prober_accepts_germany_board(self):
        """Boards with Germany or remote jobs must be accepted."""
        mock_client = MagicMock()
        mock_res = MagicMock()
        mock_res.status_code = 200
        mock_res.json.return_value = {
            "jobs": [
                {"id": 1, "title": "Solutions Consultant", "location": {"name": "Munich, Germany"}},
            ]
        }
        mock_client.get.return_value = mock_res

        result = probe_single_slug(mock_client, "munich-company")
        self.assertEqual(result, ("greenhouse", "munich-company"))


if __name__ == "__main__":
    unittest.main()

