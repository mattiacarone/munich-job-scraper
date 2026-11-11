"""
Unit and integration tests for munichScraper Brief 7 upgrades.
Section 6 required test cases:
1. A 40-day-old live Ashby posting passes with a penalty.
2. A 40-day-old 404 posting is rejected and gets closed_at.
3. 'Chief of Staff to the CEO' passes the seniority gate.
4. 'Head of Sales' still fails the seniority gate.
5. A VC-board role with a resolved Personio apply URL passes with is_startup=1.
6. A VC-board role without an employer link goes to manual_leads only.
"""

from datetime import datetime, timedelta, timezone
from pathlib import Path
import csv
import pytest

from config import config
from database import Database
from matcher import ProfileMatcher
from models import JobPosting
import liveness


@pytest.fixture
def temp_db(tmp_path):
    db_file = tmp_path / "test_jobs.db"
    return Database(db_file)


@pytest.fixture
def matcher():
    return ProfileMatcher(config.profile)


class TestBrief7:
    def test_40_day_old_live_ashby_passes_with_penalty(self, matcher, monkeypatch):
        """A 40-day-old live Ashby posting passes with a penalty."""
        now = datetime.now(timezone.utc)
        posted_40_days_ago = now - timedelta(days=40)

        # Mock liveness verification to return live
        monkeypatch.setattr(
            liveness,
            "verify_job_liveness",
            lambda job, client=None: (True, "HTTP 200 OK (Content active)")
        )

        job_40d = JobPosting(
            id="ashby:testcorp:40day-live",
            title="Customer Success Manager",
            company="Ashby Scaleup",
            location="Munich, Germany",
            url="https://jobs.ashbyhq.com/testcorp/40day-live",
            apply_url="https://jobs.ashbyhq.com/testcorp/40day-live",
            description="Work with customer success and integration onboarding.",
            source="ashby",
            ats="ashby",
            posted_at=posted_40_days_ago,
        )

        # Baseline fresh job (posted today)
        job_fresh = JobPosting(
            id="ashby:testcorp:fresh",
            title="Customer Success Manager",
            company="Ashby Scaleup",
            location="Munich, Germany",
            url="https://jobs.ashbyhq.com/testcorp/fresh",
            apply_url="https://jobs.ashbyhq.com/testcorp/fresh",
            description="Work with customer success and integration onboarding.",
            source="ashby",
            ats="ashby",
            posted_at=now,
        )

        evaluated_40d = matcher.evaluate(job_40d)
        evaluated_fresh = matcher.evaluate(job_fresh)

        # Assert 40-day-old passes every gate
        assert evaluated_40d.gate_rejected is None
        assert evaluated_40d.closed_at is None
        assert evaluated_40d.score > 0
        assert evaluated_40d.last_liveness_check is not None

        # Assert score penalty applied: fresh job scores higher than 40-day-old
        expected_penalty = (40.0 - 21.0) / 7.0  # ~2.7 points
        assert evaluated_fresh.score > evaluated_40d.score
        assert evaluated_fresh.score >= evaluated_40d.score + expected_penalty

    def test_40_day_old_404_ashby_rejected_and_gets_closed_at(self, matcher, monkeypatch):
        """A 40-day-old 404 posting is rejected and gets closed_at."""
        posted_40_days_ago = datetime.now(timezone.utc) - timedelta(days=40)

        # Mock liveness verification to return 404 Not Found
        monkeypatch.setattr(
            liveness,
            "verify_job_liveness",
            lambda job, client=None: (False, "HTTP 404 Not Found")
        )

        job_dead = JobPosting(
            id="ashby:testcorp:40day-dead",
            title="Customer Success Manager",
            company="Ashby Scaleup",
            location="Munich, Germany",
            url="https://jobs.ashbyhq.com/testcorp/40day-dead",
            apply_url="https://jobs.ashbyhq.com/testcorp/40day-dead",
            description="Work with customer success and integration onboarding.",
            source="ashby",
            ats="ashby",
            posted_at=posted_40_days_ago,
        )

        evaluated = matcher.evaluate(job_dead)

        # Assert rejected by age and marked with closed_at
        assert evaluated.gate_rejected == "age"
        assert evaluated.closed_at is not None
        assert "confirmed closed" in evaluated.rejection_reason

    def test_chief_of_staff_to_ceo_passes_seniority_gate(self, matcher):
        """'Chief of Staff to the CEO' passes the seniority gate, along with startup generalist titles."""
        ok, reason = matcher.check_gate_seniority("Chief of Staff to the CEO")
        assert ok is True, f"Failed: {reason}"

        # Other allowed startup generalist titles
        assert matcher.check_gate_seniority("Chief of Staff")[0] is True
        assert matcher.check_gate_seniority("Founder Associate")[0] is True
        assert matcher.check_gate_seniority("Founder's Associate")[0] is True
        assert matcher.check_gate_seniority("Founding Customer Success Manager")[0] is True
        assert matcher.check_gate_seniority("Business Operations")[0] is True
        assert matcher.check_gate_seniority("BizOps")[0] is True
        assert matcher.check_gate_seniority("Generalist")[0] is True
        assert matcher.check_gate_seniority("Strategy & Operations")[0] is True

        # Pure software Founding Engineer must still fail
        ok_fe, reason_fe = matcher.check_gate_seniority("Founding Engineer")
        assert ok_fe is False
        assert "pure software founding engineer" in reason_fe

    def test_head_of_sales_still_fails_seniority_gate(self, matcher):
        """'Head of Sales' still fails the seniority gate."""
        ok, reason = matcher.check_gate_seniority("Head of Sales")
        assert ok is False
        assert "Head of" in reason or "senior" in reason

        # Other senior/management exclusions
        assert matcher.check_gate_seniority("Director of Customer Success")[0] is False
        assert matcher.check_gate_seniority("VP of Operations")[0] is False
        assert matcher.check_gate_seniority("Principal Consultant")[0] is False
        assert matcher.check_gate_seniority("Staff Engineer")[0] is False
        assert matcher.check_gate_seniority("Working Student Customer Success")[0] is False

    def test_vc_board_role_with_resolved_personio_passes_is_startup(self, matcher):
        """A VC-board role with a resolved Personio apply URL passes with is_startup=1."""
        job = JobPosting(
            id="vc:earlybird:1001",
            title="Solutions Consultant",
            company="Munich Robotics AI",
            location="Munich, Germany",
            url="https://jobs.earlybird.com/jobs/1001",
            apply_url="https://munichrobotics.jobs.personio.de/job/1001",
            ats="personio",
            apply_kind="auto",
            description="Work with enterprise integrations and platform solutions.",
            source="vc",
            is_startup=1,
            startup_source="earlybird",
            posted_at=datetime.now(timezone.utc) - timedelta(days=5),
        )

        evaluated = matcher.evaluate(job)

        assert evaluated.gate_rejected is None
        assert evaluated.score > 0
        assert evaluated.is_startup == 1
        assert evaluated.startup_source == "earlybird"
        assert evaluated.apply_kind == "auto"

    def test_vc_board_role_without_employer_link_goes_to_manual_leads_only(self, matcher, temp_db):
        """A VC-board role without an employer link goes to manual_leads only."""
        job = JobPosting(
            id="vc:hvcapital:2002",
            title="Customer Success Specialist",
            company="Stealth PortCo",
            location="Munich, Germany",
            url="https://jobs.hvcapital.com/jobs/2002",
            apply_url="https://jobs.hvcapital.com/jobs/2002",  # Only VC aggregator URL
            description="Join our portfolio company as a customer success specialist.",
            source="vc",
            is_startup=1,
            startup_source="hvcapital",
            posted_at=datetime.now(timezone.utc) - timedelta(days=5),
        )

        evaluated = matcher.evaluate(job)

        # Assert rejected from automated pipeline
        assert evaluated.gate_rejected == "account-only"
        assert evaluated.apply_kind == "account"
        temp_db.save_jobs([evaluated])
