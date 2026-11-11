import json
import subprocess
from datetime import datetime, timezone
from logging.handlers import RotatingFileHandler
from pathlib import Path

from company_discovery import (
    is_agency_employer_name,
    load_linkedin_employer_seeds,
    normalize_employer_name,
    run_daily_discovery,
)
from config import config
from database import Database
from intake_checks import resolve_employer_link_via_registry
from main import (
    SLOW_SOURCE_EXEMPT,
    SLOW_SOURCE_THRESHOLD_SECONDS,
    record_source_duration,
    should_skip_slow_source,
)
from matcher import ProfileMatcher
from models import JobPosting
import supervisor


def test_a1_greenhouse_and_ashby_never_skipped_and_240s_threshold():
    """A1: greenhouse and ashby are never skipped; another source is skipped after taking > 240s twice."""
    assert "greenhouse" in SLOW_SOURCE_EXEMPT
    assert "ashby" in SLOW_SOURCE_EXEMPT
    assert SLOW_SOURCE_THRESHOLD_SECONDS == 240.0

    slow_state = {}
    # Greenhouse takes 300s three times in a row -> never skipped
    for _ in range(3):
        record_source_duration(slow_state, "greenhouse", 300.0)
        assert should_skip_slow_source(slow_state, "greenhouse") is False
        assert slow_state["greenhouse"]["skip_remaining"] == 0

    # Ashby takes 260s twice -> never skipped
    for _ in range(2):
        record_source_duration(slow_state, "ashby", 260.0)
        assert should_skip_slow_source(slow_state, "ashby") is False

    # Another source ("personio") takes 180s (< 240s) twice -> not skipped
    record_source_duration(slow_state, "personio", 180.0)
    record_source_duration(slow_state, "personio", 180.0)
    assert should_skip_slow_source(slow_state, "personio") is False

    # "personio" takes 250s (> 240s) once -> not skipped yet
    record_source_duration(slow_state, "personio", 250.0)
    assert should_skip_slow_source(slow_state, "personio") is False

    # "personio" takes 250s (> 240s) a second time in a row -> skipped for next 3 cycles
    record_source_duration(slow_state, "personio", 250.0)
    assert slow_state["personio"]["skip_remaining"] == 3
    assert should_skip_slow_source(slow_state, "personio") is True
    assert should_skip_slow_source(slow_state, "personio") is True
    assert should_skip_slow_source(slow_state, "personio") is True
    # 4th cycle -> runs again
    assert should_skip_slow_source(slow_state, "personio") is False


def test_a2_vague_locations_and_remote_ok_and_150_cap():
    """
    A2:
    - a vague location with Munich in the description passes (location_source = 'description')
    - 'Remote (Germany eligible)' passes at Gate 2
    - 'Remote' with a US-only description is rejected
    - Germany-wide with no other city passes with location_scope = 'germany-wide'
    - the 150-fetch cap holds
    """
    matcher = ProfileMatcher(config.profile)
    now = datetime.now(timezone.utc)

    # 1. "Remote (Germany eligible)" passes directly at Gate 2 via role_rules remote_ok
    j_rem = JobPosting(
        id="ashby:testco:0",
        title="Solutions Engineer",
        company="TestCo",
        location="Remote (Germany eligible)",
        url="https://jobs.ashbyhq.com/testco/0",
        source="ashby",
    )
    ok_rem, _ = matcher.check_gate_location("Remote (Germany eligible)", "Solutions Engineer", job=j_rem)
    assert ok_rem is True
    assert j_rem.location_scope in ("remote-de", "remote-eu")

    # 2. Vague location ("Hybrid") with Munich in description passes with location_source = 'description'
    job_munich_desc = JobPosting(
        id="ashby:testco:1",
        title="Solutions Engineer",
        company="TestCo",
        location="Hybrid",
        url="https://jobs.ashbyhq.com/testco/1",
        apply_url="https://jobs.ashbyhq.com/testco/1",
        description=(
            "We are hiring a Solutions Engineer to work with enterprise customers on API and SaaS integrations. "
            "This hybrid role is based out of our office in Munich, Germany (3 days per week on-site)."
        ),
        source="ashby",
        posted_at=now,
    )
    eval_munich = matcher.evaluate(job_munich_desc)
    assert eval_munich.gate_rejected is None
    assert eval_munich.location_source == "description"
    assert eval_munich.location_scope == "munich"

    # 3. "Remote" with a US-only description is rejected
    job_us_only = JobPosting(
        id="ashby:testco:2",
        title="Solutions Engineer",
        company="TestCo",
        location="Remote",
        url="https://jobs.ashbyhq.com/testco/2",
        apply_url="https://jobs.ashbyhq.com/testco/2",
        description=(
            "We are looking for a Solutions Engineer for API integrations. "
            "Candidates must reside in the United States and have US work authorization (US only)."
        ),
        source="ashby",
        posted_at=now,
    )
    eval_us = matcher.evaluate(job_us_only)
    assert eval_us.gate_rejected == "location"

    # 3b. Vague location ("Hybrid") with no Munich or remote-EU signal in description is rejected with exact A2 reason
    job_no_signal = JobPosting(
        id="ashby:testco:2b",
        title="Solutions Engineer",
        company="TestCo",
        location="Hybrid",
        url="https://jobs.ashbyhq.com/testco/2b",
        apply_url="https://jobs.ashbyhq.com/testco/2b",
        description="We are looking for a Solutions Engineer to build API integrations for enterprise clients.",
        source="ashby",
        posted_at=now,
    )
    eval_no_sig = matcher.evaluate(job_no_signal)
    assert eval_no_sig.gate_rejected == "location"
    assert eval_no_sig.rejection_reason == "vague location, no Munich or remote-EU signal in description"

    # 4. Germany-wide ("Germany") with no other city in description passes with location_scope = 'germany-wide'
    job_de_wide = JobPosting(
        id="ashby:testco:3",
        title="Solutions Engineer",
        company="TestCo",
        location="Germany",
        url="https://jobs.ashbyhq.com/testco/3",
        apply_url="https://jobs.ashbyhq.com/testco/3",
        description=(
            "Join our team as a Solutions Engineer supporting DACH enterprise clients with SaaS and REST API "
            "implementations across the region."
        ),
        source="ashby",
        posted_at=now,
    )
    eval_de = matcher.evaluate(job_de_wide)
    assert eval_de.gate_rejected is None
    assert eval_de.location_scope == "germany-wide"

    # 5. The 150-fetch cap holds per cycle and caches by job id
    matcher.reset_vague_fetch_budget()
    assert matcher.max_vague_fetches_per_cycle == 150
    claimed = []
    for i in range(155):
        jid = f"greenhouse:captest:{i}"
        assert matcher.should_fetch_for_vague_location("Hybrid", "Solutions Engineer") is True
        ok_claim = matcher.try_claim_vague_fetch(jid)
        if i < 150:
            assert ok_claim is True
            claimed.append(jid)
        else:
            assert ok_claim is False

    assert len(claimed) == 150
    assert matcher.vague_fetches_this_cycle == 150

    # Calling again for an already-fetched job id returns False without incrementing counter
    assert matcher.try_claim_vague_fetch("greenhouse:captest:0") is False
    assert matcher.vague_fetches_this_cycle == 150


def test_a3_eight_plus_years_never_blocks():
    """A3: an '8+ years' posting with a fitting title passes and records years_required = 8."""
    matcher = ProfileMatcher(config.profile)
    now = datetime.now(timezone.utc)
    job = JobPosting(
        id="greenhouse:robocloud:888",
        title="Senior Solutions Engineer (m/f/d)",
        company="RoboCloud Munich GmbH",
        location="Munich, Germany",
        url="https://boards.greenhouse.io/robocloud/jobs/888",
        apply_url="https://boards.greenhouse.io/robocloud/jobs/888",
        description=(
            "We are seeking a Senior Solutions Engineer in Munich. "
            "Requirements: 8+ years of experience in enterprise SaaS, API integrations, SQL, and Python. "
            "Fluent English required."
        ),
        source="greenhouse",
        posted_at=now,
    )
    evaluated = matcher.evaluate(job)
    assert evaluated.gate_rejected is None
    assert evaluated.years_required == 8
    assert evaluated.score is not None and evaluated.score > 0


def test_a4_linkedin_employer_seeds_skip_agencies_and_probed(tmp_path, monkeypatch):
    """A4: seeds come from linkedin_employers.json; agencies and already-probed/registered names are skipped."""
    import company_discovery

    db_path = tmp_path / "test_a4.db"
    db = Database(db_path)

    # Pre-populate one company in `companies` and one in `probed_companies`
    db.save_company_display("greenhouse", "celonis", "Celonis SE")
    db.record_probed_company("AlreadyProbed GmbH", status="miss")

    emp_file = tmp_path / "linkedin_employers.json"
    emp_data = {
        "employers": [
            {"name": "Celonis GmbH", "postings": 12, "last_seen": "2026-11-07"},
            {"name": "AlreadyProbed AG", "postings": 9, "last_seen": "2026-11-07"},
            {"name": "Acme Staffing Solutions GmbH", "postings": 8, "last_seen": "2026-11-07"},
            {"name": "Global Talent Partners", "postings": 7, "last_seen": "2026-11-07"},
            {"name": "Munich Career Group", "postings": 6, "last_seen": "2026-11-07"},
            {"name": "TechRecruit DE", "postings": 5, "last_seen": "2026-11-07"},
            {"name": "Deutsche Personal Holding", "postings": 4, "last_seen": "2026-11-07"},
            {"name": "Quantum Munich Robotics GmbH", "postings": 3, "last_seen": "2026-11-07"},
            {"name": "Bavaria Cloud Systems SE", "postings": 2, "last_seen": "2026-11-07"},
        ]
    }
    emp_file.write_text(json.dumps(emp_data), encoding="utf-8")

    assert normalize_employer_name("Quantum Munich Robotics GmbH") == "quantum munich robotics"
    assert is_agency_employer_name("Acme Staffing Solutions GmbH") is True
    assert is_agency_employer_name("Quantum Munich Robotics GmbH") is False

    seeds = load_linkedin_employer_seeds(db, path=emp_file)
    assert seeds == ["quantum munich robotics", "bavaria cloud systems"]

    # Verify run_daily_discovery probes linkedin_employers first
    probed_order = []

    def fake_probe_name(client, name, probes=None):
        probed_order.append(name)
        if name == "quantum munich robotics":
            return [("ashby", "quantummunichrobotics", 4)]
        return []

    monkeypatch.setattr(company_discovery, "probe_name", fake_probe_name)
    monkeypatch.setattr(company_discovery, "record_discovered_slug", lambda ats, slug: None)
    monkeypatch.setattr(company_discovery, "robots_allows", lambda client, url: False)

    stats = run_daily_discovery(
        db=db,
        max_names=2,
        force=True,
        linkedin_employers_path=emp_file,
    )
    assert stats["employer_names_probed"] == 2
    assert stats["employer_boards_found"] == 1
    assert set(probed_order) == {"quantum munich robotics", "bavaria cloud systems"}


def test_a5_resolve_employer_link_via_registry(tmp_path):
    """A5: an aggregator row missing an employer link is resolved through the companies table registry."""
    db_path = tmp_path / "test_a5.db"
    db = Database(db_path)
    db.save_company_display("ashby", "robofirm", "RoboFirm GmbH")

    now = datetime.now(timezone.utc)
    # Save the canonical ATS posting in jobs table
    ats_job = JobPosting(
        id="ashby:robofirm:abc-123",
        title="Solutions Engineer (m/f/d)",
        company="RoboFirm GmbH",
        location="Munich, Germany",
        url="https://jobs.ashbyhq.com/robofirm/abc-123",
        apply_url="https://jobs.ashbyhq.com/robofirm/abc-123",
        description="Solutions Engineer role in Munich working with APIs and Python.",
        source="ashby",
        posted_at=now,
    )
    db.save_jobs([ats_job])

    # Aggregator posting with no employer link
    agg_job = JobPosting(
        id="englishjobsde:9999",
        title="Solutions Engineer",
        company="RoboFirm",
        location="Munich, Germany",
        url="https://englishjobs.de/jobs/solutions-engineer-9999",
        apply_url="https://englishjobs.de/jobs/solutions-engineer-9999",
        description=(
            "We are hiring a Solutions Engineer in Munich to work with enterprise customers on API and SaaS "
            "integrations, SQL, Python, and technical onboarding across DACH."
        ),
        source="englishjobsde",
        posted_at=now,
        gate_rejected="no-employer-link",
        rejection_reason="Aggregator posting without resolved employer apply URL",
    )

    matcher = ProfileMatcher(config.profile)
    resolved_url = resolve_employer_link_via_registry(agg_job, db, matcher=matcher)
    assert resolved_url == "https://jobs.ashbyhq.com/robofirm/abc-123"
    assert agg_job.apply_url == "https://jobs.ashbyhq.com/robofirm/abc-123"
    assert agg_job.apply_url_source == "registry"
    assert agg_job.gate_rejected is None


def test_b5_scraper_log_written_and_daemon_log_not_used(tmp_path, monkeypatch):
    """B5: scraper.log is attached via RotatingFileHandler and start_daemon sends stdout/stderr to DEVNULL."""
    import logging
    import web_app  # Importing web_app must not attach a root basicConfig before main

    root_handlers = logging.getLogger().handlers
    rotating_handlers = [
        h for h in root_handlers
        if isinstance(h, RotatingFileHandler) and Path(h.baseFilename).name == "scraper.log"
    ]
    assert len(rotating_handlers) >= 1, "main.py's RotatingFileHandler for scraper.log must be attached"

    # Write a log line and flush to ensure scraper.log is written
    test_logger = logging.getLogger("Brief14Test")
    test_logger.info("Brief 14 B5 test log entry")
    for h in rotating_handlers:
        h.flush()

    scraper_log_path = Path(rotating_handlers[0].baseFilename)
    assert scraper_log_path.exists()
    assert scraper_log_path.stat().st_size > 0

    # Verify start_daemon uses subprocess.DEVNULL and never opens daemon.log
    popen_kwargs = {}

    class DummyProc:
        pid = 42424

    def fake_popen(cmd, **kwargs):
        popen_kwargs.update(kwargs)
        return DummyProc()

    monkeypatch.setattr(supervisor.subprocess, "Popen", fake_popen)
    monkeypatch.setattr(supervisor, "PID_FILE", tmp_path / "daemon.pid")
    monkeypatch.setattr(supervisor, "HEARTBEAT_FILE", tmp_path / "daemon.heartbeat")

    pid = supervisor.start_daemon()
    assert pid == 42424
    assert popen_kwargs.get("stdout") == subprocess.DEVNULL
    assert popen_kwargs.get("stderr") == subprocess.DEVNULL
    assert not (config.db_path.parent / "daemon.log").exists()


def test_part_b_removed_sources_firecrawl_exports_and_db_rules(tmp_path):
    """B1, B2, B3, B4, B6: removed sources/files are gone, live-only data paths, and rejected descriptions are NULL."""
    base_dir = config.db_path.parent

    # B1 & B2 & B4: removed files do not exist
    for rel in [
        "firecrawl_client.py",
        "exporter.py",
        "run_once.bat",
        "run_daemon.bat",
        "scrapers/firecrawl_search.py",
        "scrapers/softgarden.py",
        "scrapers/join.py",
        "scrapers/xing.py",
        "scrapers/workday.py",
        "scrapers/bamboohr.py",
        "scrapers/breezy.py",
        "scrapers/pinpoint.py",
        "scrapers/amazon_jobs.py",
        "scrapers/eures.py",
        "scrapers/vc_boards.py",
        "scrapers/linkedin.py",
    ]:
        assert not (base_dir / rel).exists(), f"{rel} should be deleted"

    # B3: live-only data paths point to LIVE_MEMORY_DIR
    assert "Job Application material" in str(config.APPLIED_HISTORY)
    assert "Job Application material" in str(config.GER_SKIP_LIST)

    # B6: save_jobs strips description to NULL for rejected rows and keeps description for passing rows
    db = Database(tmp_path / "test_b6.db")
    now = datetime.now(timezone.utc)
    rejected_job = JobPosting(
        id="ashby:test:rej1",
        title="Solutions Engineer",
        company="TestCo",
        location="London, UK",
        url="https://jobs.ashbyhq.com/test/rej1",
        description="Long description that should be stripped on save because location was rejected.",
        source="ashby",
        gate_rejected="location",
        rejection_reason="Location rejected: non-Munich city",
        posted_at=now,
    )
    passing_job = JobPosting(
        id="ashby:test:pass1",
        title="Solutions Engineer",
        company="TestCo",
        location="Munich, Germany",
        url="https://jobs.ashbyhq.com/test/pass1",
        description="Full description that must be preserved for passing rows.",
        source="ashby",
        score=82.0,
        gate_rejected=None,
        posted_at=now,
    )
    db.save_jobs([rejected_job, passing_job])
    with db._get_connection() as conn:
        r_rej = conn.execute("SELECT description FROM jobs WHERE id = 'ashby:test:rej1'").fetchone()
        r_pass = conn.execute("SELECT description FROM jobs WHERE id = 'ashby:test:pass1'").fetchone()
    assert r_rej["description"] is None
    assert r_pass["description"] == "Full description that must be preserved for passing rows."

