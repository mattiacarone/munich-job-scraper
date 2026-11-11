from datetime import datetime, timezone
import json
from pathlib import Path
import pytest

from models import JobPosting
from matcher import ProfileMatcher, MUNICH_SUBURBS, MUNICH_POSTAL_CODES_RE
from scrapers.english_boards import EnglishJobsDeScraper, MunichStartupScraper
from scrapers.stepstone import StepStoneScraper
from config import ProfileConfig

DATA_DIR = Path(__file__).resolve().parent.parent / "data"


def test_munich_suburbs_and_postal_codes():
    matcher = ProfileMatcher(ProfileConfig())

    # Suburbs test
    for suburb in ["Gräfelfing", "Krailling", "Weßling", "Oberpfaffenhofen", "Karlsfeld", "Holzkirchen", "Unterhaching"]:
        job = JobPosting(
            id=f"test:{suburb.lower()}",
            title="Senior Solutions Consultant",
            company="Munich Tech GmbH",
            location=f"{suburb}, Germany",
            url=f"https://boards.greenhouse.io/test/{suburb.lower()}",
            description="We are looking for a Solutions Consultant fluent in English with 3 years of experience with SaaS and APIs.",
            source="greenhouse",
        )
        evaluated = matcher.evaluate(job)
        assert evaluated.gate_rejected is None, f"Failed on suburb {suburb}: {evaluated.gate_rejected}"
        assert evaluated.location_scope == "munich", f"Expected 'munich' scope for {suburb}, got {evaluated.location_scope}"

    # Munich postal codes test
    for postal in ["80807", "85748", "82234"]:
        job = JobPosting(
            id=f"test:{postal}",
            title="Customer Success Manager",
            company="Munich Cloud GmbH",
            location=f"DE-{postal} Bayern, Germany",
            url=f"https://jobs.personio.de/test/{postal}",
            description="Leading customer success for enterprise accounts. Fluent English required. 3 years experience.",
            source="personio",
        )
        evaluated = matcher.evaluate(job)
        assert evaluated.gate_rejected is None, f"Failed on postal {postal}: {evaluated.gate_rejected}"
        assert evaluated.location_scope == "munich", f"Expected 'munich' scope for {postal}, got {evaluated.location_scope}"


def test_discovered_boards_contains_new_munich_slugs():
    p = DATA_DIR / "discovered_boards.json"
    assert p.exists()
    d = json.loads(p.read_text(encoding="utf-8"))
    
    # Check SmartRecruiters Munich enterprise slugs
    sr_slugs = d.get("smartrecruiters", [])
    assert "Brainlab" in sr_slugs
    assert "BoschGroup" in sr_slugs
    assert "SiemensHealthineers" in sr_slugs

    # Check Ashby & Greenhouse
    assert "taktile" in d.get("ashby", [])
    assert "celonis" in d.get("greenhouse", [])
    assert "navvis" in d.get("greenhouse", [])


def test_english_jobs_de_scraper_structure():
    scraper = EnglishJobsDeScraper(max_pages=1)
    assert scraper.name == "englishjobsde"
    assert scraper.base_url == "https://englishjobs.de/in/munich"


def test_munich_startup_scraper_structure():
    scraper = MunichStartupScraper(max_items=5)
    assert scraper.name == "munichstartup"
    assert scraper.list_url == "https://www.munich-startup.de/jobs/stellenangebote"


def test_stepstone_reverse_ats_lookup_integration(monkeypatch):
    class DummyDB:
        def __init__(self):
            pass

    scraper = StepStoneScraper(db=DummyDB())
    job = JobPosting(
        id="stepstone:12345",
        title="Solutions Engineer",
        company="Celonis",
        location="Munich",
        url="https://www.stepstone.de/stellenangebote--12345.html",
        source="stepstone"
    )

    # Mock resolve_employer_link_via_registry to simulate finding a Celonis board
    import intake_checks

    def fake_resolve(j, db):
        j.apply_url = "https://boards.greenhouse.io/celonis/jobs/999"
        j.ats = "greenhouse"
        j.ats_slug = "celonis"
        j.apply_kind = "auto"
        j.apply_route = "greenhouse"
        j.apply_host = "boards.greenhouse.io"
        return True

    monkeypatch.setattr(intake_checks, "resolve_employer_link_via_registry", fake_resolve)

    scraper._parse_detail_page(job, "<html><body><div>Job at Celonis in Munich</div></body></html>")
    assert job.apply_url == "https://boards.greenhouse.io/celonis/jobs/999"
    assert job.apply_kind == "auto"
    assert job.apply_route == "greenhouse" or job.apply_host == "boards.greenhouse.io"


def test_dashboard_generation(tmp_path):
    from dashboard_generator import generate_dashboard
    from database import Database
    from config import config

    test_out = tmp_path / "test_dash.html"
    db = Database(config.db_path)
    res_path = generate_dashboard(db=db, output_path=test_out)
    assert res_path.exists()
    assert res_path.stat().st_size > 5000
    content = res_path.read_text(encoding="utf-8")
    assert "MUNICH SCOUT" in content
    assert "Improvement Engine" in content

