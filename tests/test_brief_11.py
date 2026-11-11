"""
Unit and integration tests for Brief 11 implementations.
(Tests for removed modules AmazonJobsScraper, WorkdayScraper, EuresScraper, exporter removed in Brief 14 Part B.)
"""

from datetime import datetime, timedelta, timezone
from pathlib import Path
import pytest
from unittest.mock import MagicMock, patch

from config import ProfileConfig
from database import Database
from matcher import ProfileMatcher
from models import JobPosting
from scrapers.arbeitsagentur import ArbeitsagenturScraper
from scrapers.remote_boards import RemoteOkScraper
from ecosystem_partners import seed_ecosystem_partners


@pytest.fixture
def matcher():
    return ProfileMatcher(ProfileConfig())


@pytest.fixture
def tmp_db(tmp_path):
    db_file = tmp_path / "test_brief11.db"
    return Database(db_file)


# ============================================================================
# 1. Section 5: Remote Location Parsing Tests
# ============================================================================

def test_brief11_remote_locations_in_scope(matcher):
    """
    Treat as in scope:
    'Remote (Europe)', 'Remote - EMEA', 'EU timezone', 'CET/CEST',
    'Europe (remote)', 'Worldwide', 'Anywhere'.
    All should map to 'remote-eu'.
    """
    in_scope_cases = [
        "Remote (Europe)",
        "Remote - EMEA",
        "EU timezone",
        "CET/CEST",
        "Europe (remote)",
        "Worldwide",
        "Anywhere",
    ]
    for loc in in_scope_cases:
        job = JobPosting(
            id=f"test:1:{loc[:4]}",
            title="Solutions Consultant",
            company="Global Tech",
            location=loc,
            url="https://example.com/job",
            description="Work with customers across our platform. Great benefits.",
            source="greenhouse",
        )
        ok, reason = matcher.check_gate_location(loc, job.title, job.description, job.url, job=job)
        assert ok is True, f"Expected {loc} to pass gate, but failed: {reason}"
        assert job.location_scope == "remote-eu", f"Expected 'remote-eu' for {loc}, got {job.location_scope}"


def test_brief11_remote_locations_out_of_scope(matcher):
    """
    Out of scope: remote limited to one non-German country:
    'Remote - Spain', 'Remote UK only', 'Remote - France', 'Remote - Italy', 'Remote - US'.
    """
    out_of_scope_cases = [
        "Remote - Spain",
        "Remote UK only",
        "Remote - France",
        "Remote - Italy",
        "Remote - US",
        "Remote - Spain (EU)",
    ]
    for loc in out_of_scope_cases:
        job = JobPosting(
            id=f"test:2:{loc[:4]}",
            title="Solutions Consultant",
            company="Foreign Corp",
            location=loc,
            url="https://example.com/job",
            description="Remote role.",
            source="greenhouse",
        )
        ok, reason = matcher.check_gate_location(loc, job.title, job.description, job.url, job=job)
        assert ok is False, f"Expected {loc} to be rejected, but passed with scope {job.location_scope}"
        assert job.location_scope is None


def test_brief11_worldwide_with_us_residence_rejected(matcher):
    """
    'Worldwide' or 'Anywhere' must be rejected if text names a non-EU country
    or says US-only as required residence.
    """
    job = JobPosting(
        id="test:3:ww",
        title="Solutions Consultant",
        company="US Co",
        location="Worldwide",
        url="https://example.com/job",
        description="Candidates must reside in the United States and have authorization.",
        source="greenhouse",
    )
    ok, reason = matcher.check_gate_location(job.location, job.title, job.description, job.url, job=job)
    assert ok is False
    assert "restricted to foreign country" in reason


# ============================================================================
# 4. Arbeitsagentur Scraper Test
# ============================================================================

def test_arbeitsagentur_scraper_recorded():
    mock_search = {
        "ergebnisliste": [
            {
                "stellenangebotsTitel": "Partner Development Manager (m/f/d)",
                "firma": "Cloud Solutions AG",
                "referenznummer": "10000-999-S",
                "stellenlokationen": [{"adresse": {"ort": "München", "plz": "80331"}}],
            },
            {
                "stellenangebotsTitel": "Buchhalter (m/w/d)",
                "firma": "German Only GmbH",
                "referenznummer": "10000-888-S",
                "stellenlokationen": [{"adresse": {"ort": "München", "plz": "80331"}}],
            },
        ]
    }
    mock_detail_en = {
        "stellenangebotsBeschreibung": "We are seeking a Partner Development Manager to lead cloud alliances with enterprise software providers. Fluent English required.",
        "eintrittsdatum": "2026-11-01",
    }
    mock_detail_de = {
        "stellenangebotsBeschreibung": "Wir suchen einen erfahrenen Buchhalter für die Finanzbuchhaltung und Monatsabschlüsse in unserem Büro in München.",
        "eintrittsdatum": "2026-11-01",
    }

    scraper = ArbeitsagenturScraper(titles=["Partner Manager"])

    with patch("httpx.Client.get") as mock_get:
        import base64

        def side_effect(url, **kwargs):
            res = MagicMock(status_code=200)
            if "jobdetails" in url:
                if "10000-999-S" in str(base64.b64decode(url.split("/")[-1])):
                    res.json.return_value = mock_detail_en
                else:
                    res.json.return_value = mock_detail_de
            else:
                res.json.return_value = mock_search
            return res

        mock_get.side_effect = side_effect

        jobs = scraper.scrape()
        # German job must be filtered out; English job kept
        assert len(jobs) == 1
        j = jobs[0]
        assert j.id == "arbeitsagentur:10000-999-S"
        assert j.title == "Partner Development Manager (m/f/d)"
        assert j.apply_kind == "account_portal"
        assert j.apply_route == "arbeitsagentur"


# ============================================================================
# 6. RemoteOK Scraper Test
# ============================================================================

def test_remoteok_scraper_recorded():
    mock_data = [
        {"legal": "disclaimer"},
        {
            "id": "991122",
            "position": "Solutions Engineer",
            "company": "SaaS Platform",
            "location": "Worldwide",
            "url": "https://remoteok.com/remote-jobs/991122",
            "apply_url": "https://remoteok.com/remote-jobs/991122",
            "description": "Enterprise customer solutions role.",
            "date": "2026-10-27T10:00:00Z",
        },
        {
            "id": "991123",
            "position": "Sales Lead",
            "company": "US Only Corp",
            "location": "USA only",
            "url": "https://remoteok.com/remote-jobs/991123",
            "description": "US role.",
            "date": "2026-10-27T10:00:00Z",
        },
    ]

    scraper = RemoteOkScraper()
    with patch("httpx.Client.get") as mock_get:
        r = MagicMock(status_code=200)
        r.json.return_value = mock_data
        mock_get.return_value = r

        jobs = scraper.scrape()
        # Only the Worldwide one is kept, USA only is rejected
        assert len(jobs) == 1
        assert jobs[0].id == "remoteok:991122"
        assert jobs[0].company == "SaaS Platform"
        assert jobs[0].source == "remoteok"


# ============================================================================
# 7. Ecosystem Partners Seeding Test
# ============================================================================

def test_ecosystem_partners_seeding(tmp_db):
    count = seed_ecosystem_partners(tmp_db)
    assert count >= 30
    with tmp_db._get_connection() as conn:
        row = conn.execute("SELECT COUNT(*) FROM companies WHERE source LIKE 'partner_%'").fetchone()
        assert row[0] >= 30
        devoteam = conn.execute("SELECT display_name FROM companies WHERE slug = 'devoteam'").fetchone()
        assert devoteam is not None
        assert devoteam[0] == "Devoteam"


# ============================================================================
# 8. Recall per Title & Zero-Passing Query Suppression (Section 6)
# ============================================================================

def test_recall_queries_priority_and_suppression(tmp_db):
    queries = tmp_db.get_eligible_queries(["Integration Engineer", "ServiceNow Consultant", "Custom Other"])
    
    # Best-converting titles must be first
    assert queries[0] == "Business Systems Analyst"
    assert "Customer Enablement" in queries
    
    # Excluded titles must not appear
    assert not any("integration engineer" in q.lower() for q in queries)
    assert not any("servicenow" in q.lower() for q in queries)

    # After 2 runs with zero passing rows, the query is suppressed
    tmp_db.record_query_run("Custom Other", passing_count=0)
    tmp_db.record_query_run("Custom Other", passing_count=0)
    
    queries_after = tmp_db.get_eligible_queries(["Custom Other"])
    assert "Custom Other" not in queries_after


# ============================================================================
# 9. Freshness & Polling Frequency (Section 7)
# ============================================================================

def test_freshness_board_poll_decision(tmp_db):
    now = datetime.now(timezone.utc)
    
    # Case A: Board with 0 passing rows -> weekly cadence
    due_init, reason = tmp_db.should_poll_board("greenhouse", "zeroco")
    assert due_init is True
    assert "first_poll" in reason

    tmp_db.record_board_poll("greenhouse", "zeroco")
    # Immediately after poll, it should skip weekly
    due_after, reason = tmp_db.should_poll_board("greenhouse", "zeroco")
    assert due_after is False
    assert "skip_weekly" in reason

    # Case B: Board with 3+ passing rows in last 30d -> 6h cadence
    passing_jobs = [
        JobPosting(
            id=f"greenhouse:passco:{i}",
            title="Solutions Architect",
            company="PassCo",
            location="Munich, Germany",
            url=f"https://example.com/{i}",
            ats="greenhouse",
            ats_slug="passco",
            score=75.0,
            gate_rejected=None,
            source="greenhouse",
            created_at=now - timedelta(days=2),
        )
        for i in range(4)
    ]
    tmp_db.save_jobs(passing_jobs)

    # Set last poll to 7 hours ago -> should be due under 6h rule
    seven_hours_ago = (now - timedelta(hours=7)).isoformat()
    tmp_db.set_metadata("last_poll_greenhouse_passco", seven_hours_ago)
    
    due_pass, reason = tmp_db.should_poll_board("greenhouse", "passco")
    assert due_pass is True
    assert "due_6h" in reason
