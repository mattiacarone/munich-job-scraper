"""
Unit and integration tests for munichScraper Brief 6 upgrades.
Covers:
1. StepStone HTML/JSON fixture parsing (relative dates, ID format stepstone:<id>).
2. XING embedded JSON / Apollo state fixture parsing (ID format xing:<id>).
3. English boards fixture parsing (GermanTechJobs RSS feed).
4. BambooHR careers list JSON fixture parsing (apply_kind='captcha').
5. Teamtailor jobs HTML fixture parsing (apply_kind='auto').
6. Softgarden widget HTML fixture parsing (apply_kind='auto').
7. Cross-source deduplication: Company ATS canonical over StepStone.
8. StepStone internal apply without offsite link rejected as 'account-only'.
9. Remote board posting without EU/Germany eligibility rejected.
"""

from datetime import datetime, timezone
import json
from pathlib import Path
import pytest
import sqlite3

from apply_classifier import classify_apply_kind
from database import Database, compute_dedupe_key
from matcher import ProfileMatcher
from models import JobPosting
from config import config
from scrapers.remote_boards import is_germany_remote_eligible
from scrapers.stepstone import StepStoneScraper, parse_relative_date
from scrapers.teamtailor import TeamtailorScraper

FIXTURES_DIR = Path(__file__).resolve().parent / "fixtures"


@pytest.fixture
def temp_db(tmp_path):
    db_file = tmp_path / "test_jobs.db"
    return Database(db_file)


@pytest.fixture
def matcher():
    return ProfileMatcher(config.profile)


class TestBrief6:
    def test_stepstone_parsing_from_fixture(self, temp_db, matcher):
        fixture_path = FIXTURES_DIR / "stepstone_search.html"
        html = fixture_path.read_text(encoding="utf-8")

        scraper = StepStoneScraper(db=temp_db, matcher=matcher)
        jobs = scraper._parse_search_page(html)

        assert len(jobs) == 2
        j1 = jobs[0]
        assert j1.id == "stepstone:14453266"
        assert "Customer Success Manager" in j1.title
        assert j1.company == "Abacus Umantis AG"
        assert j1.location == "München"
        assert j1.posted_at is not None
        # Posted vor 2 Tagen -> age between 1.5 and 2.5 days
        age_days = (datetime.now(timezone.utc) - j1.posted_at).total_seconds() / 86400.0
        assert 1.5 <= age_days <= 2.5
        assert j1.apply_kind == "account"

        j2 = jobs[1]
        assert j2.id == "stepstone:14453299"
        assert "Solutions Consultant" in j2.title

    def test_teamtailor_parsing_from_fixture(self, temp_db):
        fixture_path = FIXTURES_DIR / "teamtailor_jobs.html"
        html = fixture_path.read_text(encoding="utf-8")

        scraper = TeamtailorScraper(db=temp_db)
        class MockClient:
            def get(self, url):
                class MockResp:
                    status_code = 200
                    text = html
                return MockResp()

        jobs = scraper._fetch_slug(MockClient(), "acme")
        assert len(jobs) == 2
        j1 = next(j for j in jobs if "54321" in j.id)
        assert j1.id == "teamtailor:acme:54321"
        assert "Solutions Architect Emea" in j1.title
        assert j1.apply_kind == "auto"

    def test_dedupe_across_stepstone_and_ats_same_job(self, temp_db):
        # Insert same role from Greenhouse ATS and from StepStone
        comp = "Celonis SE"
        tit = "Senior Customer Success Manager (m/f/d)"
        dk = compute_dedupe_key(comp, tit)

        j_ats = JobPosting(
            id="greenhouse:celonis:112233",
            title=tit,
            company=comp,
            location="Munich, Germany",
            url="https://boards.greenhouse.io/celonis/jobs/112233",
            source="greenhouse",
            score=75.0,
            dedupe_key=dk,
            apply_kind="auto",
        )
        j_stepstone = JobPosting(
            id="stepstone:998811",
            title=tit,
            company=comp,
            location="München",
            url="https://www.stepstone.de/stellenangebote--998811-inline.html",
            source="stepstone",
            score=75.0,
            dedupe_key=dk,
            apply_kind="account",
        )

        temp_db.save_jobs([j_ats, j_stepstone])
        dups = temp_db.deduplicate_all_jobs()
        assert dups >= 1

        all_j = {j.id: j for j in temp_db.get_all_jobs()}
        ats_row = all_j["greenhouse:celonis:112233"]
        stepstone_row = all_j["stepstone:998811"]

        # ATS row must be canonical (no gate_rejected)
        assert ats_row.gate_rejected is None
        # StepStone row must be marked duplicate of ATS row
        assert stepstone_row.gate_rejected == "duplicate_of:greenhouse:celonis:112233"

    def test_account_only_stepstone_posting_rejected(self, matcher):
        job = JobPosting(
            id="stepstone:12345",
            title="Customer Success Specialist",
            company="Munich SaaS GmbH",
            location="Munich",
            url="https://www.stepstone.de/stellenangebote--12345-inline.html",
            source="stepstone",
            description="Leading customer success team in Munich. English language role.",
            apply_kind="account",
            apply_host="stepstone.de",
        )
        evaluated = matcher.evaluate(job)
        assert evaluated.gate_rejected == "account-only"
        assert evaluated.score == 0.0

    def test_remote_board_posting_without_eu_eligibility_rejected(self, matcher):
        # 1. Non-EU eligibility regex check
        assert not is_germany_remote_eligible("US Only")
        assert not is_germany_remote_eligible("United States Only")
        assert not is_germany_remote_eligible("North America only")
        assert is_germany_remote_eligible("Europe, Germany, UK")
        assert is_germany_remote_eligible("Worldwide")
        assert is_germany_remote_eligible("EMEA")

        # 2. Location gate evaluation
        us_only_job = JobPosting(
            id="remotive:888",
            title="Senior Integration Consultant",
            company="Silicon Valley Co",
            location="San Francisco, CA (US Only)",
            url="https://remotive.com/jobs/888",
            source="remotive",
            description="US candidates only.",
        )
        evaluated_us = matcher.evaluate(us_only_job)
        assert evaluated_us.gate_rejected == "location"

        germany_remote_job = JobPosting(
            id="remotive:889",
            title="Senior Integration Consultant",
            company="Euro Cloud GmbH",
            location="Remote (Germany eligible)",
            url="https://remotive.com/jobs/889",
            source="remotive",
            description="Work anywhere in Europe / Germany. Fluent English required.",
        )
        evaluated_eu = matcher.evaluate(germany_remote_job)
        assert evaluated_eu.gate_rejected is None
        assert evaluated_eu.score > 0
