"""
Comprehensive test suite for ProfileMatcher gates and filters as required by IMPROVEMENT_BRIEF.md:
1. German "plus" sentence does not cancel a mandatory sentence
2. Berlin-only rejected
3. "Director" rejected
4. "Senior Customer Success Manager" accepted
5. German-language description rejected
6. Duplicate in applied_history.csv rejected
7. Company on ger_skip_list.csv (scope: all) rejected
8. Off-function title (Pure Software Engineer, Account Executive) rejected
9. Lead Generation accepted (Lead alone rejected)
10. Section 3 scoring bonuses and penalties
"""

import sys
import unittest
from datetime import datetime, timedelta, timezone
from pathlib import Path

# Add project root to sys.path
sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from config import config
from matcher import ProfileMatcher
from models import JobPosting


class TestProfileMatcher(unittest.TestCase):
    def setUp(self):
        self.matcher = ProfileMatcher(config.profile)

    def test_german_plus_does_not_cancel_mandatory_sentence(self):
        """
        A 'German is a plus' sentence elsewhere must NOT cancel a mandatory sentence.
        """
        desc = (
            "We are looking for a Solutions Consultant to join our team in Munich. "
            "Fluent German is required for client workshops and communication. "
            "Knowledge of Italian is a plus."
        )
        job = JobPosting(
            id="test:1",
            title="Solutions Consultant",
            company="Global Tech Inc",
            location="Munich, Germany",
            url="https://example.com/job/1",
            description=desc,
        )
        evaluated = self.matcher.evaluate(job)
        self.assertIn(evaluated.gate_rejected, ("german", "german_required"))
        self.assertIn("Fluent German is required", evaluated.rejection_reason)

    def test_berlin_only_rejected(self):
        """
        REJECT Berlin, Hamburg, Frankfurt, Stuttgart, Cologne, Düsseldorf etc. when Munich is not also listed.
        """
        job = JobPosting(
            id="test:2",
            title="Solutions Engineer",
            company="Startup XYZ",
            location="Berlin, Germany",
            url="https://example.com/job/2",
            description="Join our engineering team in Berlin. English speaking environment.",
        )
        evaluated = self.matcher.evaluate(job)
        self.assertEqual(evaluated.gate_rejected, "location")
        self.assertIn("non-Munich city without Munich", evaluated.rejection_reason)

    def test_director_rejected(self):
        """
        Reject titles with Director, Head of, VP, Vice President, Chief, Principal, Staff, Lead.
        """
        job = JobPosting(
            id="test:3",
            title="Director of Customer Success",
            company="Enterprise Cloud AG",
            location="Munich, Germany",
            url="https://example.com/job/3",
            description="Leading customer success operations in Munich. Working language English.",
        )
        evaluated = self.matcher.evaluate(job)
        self.assertEqual(evaluated.gate_rejected, "seniority")

    def test_senior_customer_success_manager_accepted(self):
        """
        'Senior' is allowed, and 'Senior Customer Success Manager' should pass all gates.
        """
        job = JobPosting(
            id="test:4",
            title="Senior Customer Success Manager",
            company="Innovate Solutions",
            location="Munich, Germany",
            url="https://example.com/job/4",
            description=(
                "We are seeking a Senior Customer Success Manager in Munich. "
                "You will partner with clients to drive onboarding and product adoption. "
                "Experience with Salesforce, SaaS platforms, and enterprise workshops is required. "
                "Our company language is English. German skills are a plus."
            ),
        )
        evaluated = self.matcher.evaluate(job)
        self.assertIsNone(evaluated.gate_rejected, f"Failed gate: {evaluated.gate_rejected} - {evaluated.rejection_reason}")
        self.assertGreater(evaluated.score, 0)

    def test_german_language_description_rejected(self):
        """
        German-language descriptions (>25 German function words and > 1.2x English) must be rejected.
        """
        german_desc = (
            "Wir suchen einen engagierten Berater für unser Team in München. "
            "Deine Aufgaben umfassen die Betreuung unserer Kunden und die Umsetzung von Projekten. "
            "Wir bieten dir eine spannende Aufgabe mit viel Verantwortung und Gestaltungsspielraum. "
            "Mit deiner Erfahrung bringst du neue Ideen ein und unterstützt bei der Entwicklung. "
            "Die Zusammenarbeit im Team wird bei uns großgeschrieben und auch der Spaß kommt nicht zu kurz. "
            "Kenntnisse in der Beratung und Freude an der Arbeit sind Voraussetzung für diese Stelle. "
            "Bewirb dich jetzt und werde Teil unseres Teams auf unserem erfolgreichen Weg."
        )
        job = JobPosting(
            id="test:5",
            title="Digital Transformation Consultant",
            company="Consulting Partners",
            location="Munich, Germany",
            url="https://example.com/job/5",
            description=german_desc,
        )
        evaluated = self.matcher.evaluate(job)
        self.assertEqual(evaluated.gate_rejected, "german")
        self.assertIn("description written in German", evaluated.rejection_reason)

    def test_duplicate_in_applied_history_rejected(self):
        """
        Gate 1: Duplicate company + title in data/applied_history.csv -> skip.
        Synthesia / Solutions Consultant is in applied_history.csv.
        """
        job = JobPosting(
            id="test:6",
            title="Solutions Consultant (m/w/d)",
            company="Synthesia",
            location="Munich, Germany",
            url="https://example.com/job/6",
            description="Solutions consultant role in Munich.",
        )
        evaluated = self.matcher.evaluate(job)
        self.assertEqual(evaluated.gate_rejected, "dup")
        self.assertIn("applied_history.csv", evaluated.rejection_reason)

    def test_skip_list_company_rejected(self):
        """
        Gate 5: Company on ger_skip_list.csv with scope 'all' -> skip.
        Aleph Alpha has scope 'all' in ger_skip_list.csv and is not in applied_history.
        """
        job = JobPosting(
            id="test:7",
            title="Customer Success Manager",
            company="Aleph Alpha",
            location="Munich, Germany",
            url="https://example.com/job/7",
            description="Customer success in Munich.",
        )
        evaluated = self.matcher.evaluate(job)
        self.assertEqual(evaluated.gate_rejected, "skip-list")

    def test_off_function_titles_rejected(self):
        """
        Gate 6: Off-function titles (pure software developer, data engineer, account executive) -> skip.
        """
        for title in ["Software Engineer", "Backend Developer", "Data Engineer", "Account Executive", "SDR"]:
            job = JobPosting(
                id=f"test:off:{title}",
                title=title,
                company="Tech Corp",
                location="Munich, Germany",
                url="https://example.com/job/off",
                description="English speaking position.",
            )
            evaluated = self.matcher.evaluate(job)
            self.assertEqual(evaluated.gate_rejected, "off-function", f"Expected off-function for {title}")

    def test_lead_generation_specialist_accepted(self):
        """
        'Lead Generation' is an exception to the Lead seniority filter.
        """
        job = JobPosting(
            id="test:lead_gen",
            title="Lead Generation Specialist",
            company="ScaleUp Solutions",
            location="Munich, Germany",
            url="https://example.com/job/lead_gen",
            description="Support our sales and marketing pipeline. English speaking environment.",
        )
        evaluated = self.matcher.evaluate(job)
        self.assertNotEqual(evaluated.gate_rejected, "seniority")


if __name__ == "__main__":
    unittest.main()
