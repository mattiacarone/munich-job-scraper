"""
Tests for IMPROVEMENT_BRIEF_12.md:
1. Section 1: Watchdog & heartbeat (>30m restart + log), heartbeat during cycle, back-to-back run_forever loop.
2. Section 2: Daily company discovery (slug variants, 8 ATS probes, 26 known Munich slugs imported, weekly miss retry).
3. Section 3: LinkedIn removed from scheduled JobEngine.scrapers.
4. Section 4: Low-yield source cadences (stepstone & xing weekly 168h, arbeitnow daily 24h).
5. Section 5: Reject aggregator postings without employer apply link (weworkremotely, remoteok, arbeitsagentur captcha/portal, aggregator self-links).
6. Section 6: Hard blocks on full description with quote saved (german_required + 'is a plus' exemption, years_required + founder/CTO, contract terms).
7. Section 7: Data quality (300-char description gate + IntakeFixer fetch, first_seen backfill, triple_key dedupe + prolificacademic/true-talent-solutions cap, Clera role-family + Munich/remote-EU filter).
"""

from datetime import datetime, timedelta, timezone
from pathlib import Path
from unittest.mock import MagicMock, patch
import pytest

from company_discovery import (
    KNOWN_MUNICH_SLUGS,
    discover_names,
    import_known_slugs,
    slug_variants,
)
from config import ProfileConfig
from database import Database
from intake_checks import (
    MIN_DESCRIPTION_CHARS,
    check_clera,
    check_employer_link,
    check_hard_blocks,
    dedupe_and_cap,
    find_employer_link,
    is_clera,
    triple_key,
)
from intake_fixer import IntakeFixer
from matcher import ProfileMatcher
from models import JobPosting
from scrapers.arbeitsagentur import ArbeitsagenturScraper
from supervisor import (
    heartbeat_age_minutes,
    mark_source_run,
    run_forever,
    source_is_due,
    touch_heartbeat,
    watchdog_check,
)


LONG_VALID_DESC = (
    "We are looking for a Solutions Consultant to join our team in Munich, Germany. "
    "In this full-time permanent role you will design API and iPaaS integrations, lead technical discovery workshops, "
    "configure enterprise SaaS workflows, and partner closely with Customer Success and Product teams across Europe. "
    "English is our working language; German is a plus."
)


@pytest.fixture
def tmp_db(tmp_path):
    return Database(tmp_path / "test_b12.db")


@pytest.fixture
def strict_matcher():
    m = ProfileMatcher(ProfileConfig())
    m.min_description_chars = MIN_DESCRIPTION_CHARS
    return m


# ============================================================================
# Section 1: Keep the daemon alive (watchdog, heartbeat, back-to-back loop)
# ============================================================================

def test_s1_watchdog_restarts_stale_heartbeat_and_logs(tmp_path):
    hb = tmp_path / "daemon.heartbeat"
    pid_file = tmp_path / "daemon.pid"
    log_file = tmp_path / "watchdog.log"

    # Write a heartbeat 45 minutes in the past (> 30 min threshold)
    stale_time = datetime.now(timezone.utc) - timedelta(minutes=45)
    hb.write_text(stale_time.isoformat(), encoding="utf-8")

    started = []

    def fake_start():
        started.append(True)
        return 42424

    restarted = watchdog_check(
        max_age_min=30.0,
        heartbeat_path=hb,
        pid_path=pid_file,
        log_path=log_file,
        start_fn=fake_start,
    )
    assert restarted == "restarted"
    assert len(started) == 1
    assert log_file.exists()
    assert "new pid 42424" in log_file.read_text(encoding="utf-8")

    # Fresh heartbeat (< 30 min) should NOT trigger restart
    touch_heartbeat(hb)
    assert heartbeat_age_minutes(hb) < 1.0
    restarted_again = watchdog_check(
        max_age_min=30.0,
        heartbeat_path=hb,
        pid_path=pid_file,
        log_path=log_file,
        start_fn=fake_start,
        is_running_fn=lambda _pid: True,
    )
    assert restarted_again == "ok"


def test_s1_run_forever_runs_cycles_back_to_back():
    calls = []

    def cycle():
        calls.append(datetime.now(timezone.utc))

    n = run_forever(cycle, pause_seconds=0, stop_after=3)
    assert n == 3
    assert len(calls) == 3


# ============================================================================
# Section 2: Daily company discovery & 26 known Munich slugs
# ============================================================================

def test_s2_slug_variants_and_26_known_slugs(tmp_db):
    variants = slug_variants("AppliedAI GmbH")
    assert "appliedai" in variants
    variants_dash = slug_variants("Miles Mobility GmbH")
    assert "miles-mobility" in variants_dash
    assert "milesmobility" in variants_dash

    total_slugs = sum(len(v) for v in KNOWN_MUNICH_SLUGS.values())
    assert total_slugs == 26
    added = import_known_slugs(tmp_db)
    assert added == 26

    # Verify all 26 slugs exist in companies table
    with tmp_db._get_connection() as conn:
        for ats, lst in KNOWN_MUNICH_SLUGS.items():
            for slug in lst:
                row = conn.execute(
                    "SELECT 1 FROM companies WHERE source = ? AND slug = ?", (ats, slug)
                ).fetchone()
                assert row is not None, f"Missing known slug {ats}:{slug}"


def test_s2_discover_names_saves_hits_and_weekly_misses(tmp_db):
    fake_probes = {
        "ashby": lambda _client, slug: 5 if slug == "teststartup" else 0,
        "greenhouse": lambda _client, _slug: 0,
    }

    stats1 = discover_names(
        tmp_db,
        ["TestStartup GmbH", "MissingCorp AG"],
        concurrency=2,
        probes=fake_probes,
    )
    assert stats1["hits"] == 1
    assert stats1["misses"] == 1

    # Immediate second run should skip both (probed_companies < 7 days)
    stats2 = discover_names(
        tmp_db,
        ["TestStartup GmbH", "MissingCorp AG"],
        concurrency=2,
        probes=fake_probes,
    )
    assert stats2["probed"] == 0


# ============================================================================
# Section 3 & 4: LinkedIn removed + Low-yield source cadences
# ============================================================================

def test_s3_linkedin_removed_from_scheduled_scrapers(monkeypatch, tmp_db):
    import main as main_mod
    monkeypatch.setattr(main_mod.config, "db_path", tmp_db.db_path)
    engine = main_mod.JobEngine()
    scraper_names = [s.name for s in engine.scrapers]
    assert "linkedin" not in scraper_names


def test_s4_low_yield_source_cadences(tmp_db):
    now = datetime(2026, 10, 31, 12, 0, tzinfo=timezone.utc)

    # First time: all sources are due
    assert source_is_due(tmp_db, "stepstone", now=now) is True
    assert source_is_due(tmp_db, "arbeitsagentur", now=now) is True
    assert source_is_due(tmp_db, "munichstartup", now=now) is True
    assert source_is_due(tmp_db, "arbeitnow", now=now) is True
    assert source_is_due(tmp_db, "greenhouse", now=now) is True

    mark_source_run(tmp_db, "stepstone", now=now)
    mark_source_run(tmp_db, "arbeitsagentur", now=now)
    mark_source_run(tmp_db, "munichstartup", now=now)
    mark_source_run(tmp_db, "arbeitnow", now=now)
    mark_source_run(tmp_db, "greenhouse", now=now)

    # 12 hours later: greenhouse is due, arbeitnow/arbeitsagentur/munichstartup (24h) and stepstone (168h) are NOT due
    t_12h = now + timedelta(hours=12)
    assert source_is_due(tmp_db, "greenhouse", now=t_12h) is True
    assert source_is_due(tmp_db, "arbeitnow", now=t_12h) is False
    assert source_is_due(tmp_db, "arbeitsagentur", now=t_12h) is False
    assert source_is_due(tmp_db, "munichstartup", now=t_12h) is False
    assert source_is_due(tmp_db, "stepstone", now=t_12h) is False

    # 25 hours later: arbeitnow, arbeitsagentur, munichstartup are due, stepstone still NOT due
    t_25h = now + timedelta(hours=25)
    assert source_is_due(tmp_db, "arbeitnow", now=t_25h) is True
    assert source_is_due(tmp_db, "arbeitsagentur", now=t_25h) is True
    assert source_is_due(tmp_db, "munichstartup", now=t_25h) is True
    assert source_is_due(tmp_db, "stepstone", now=t_25h) is False

    # 169 hours later (> 7 days): stepstone is due
    t_169h = now + timedelta(hours=169)
    assert source_is_due(tmp_db, "stepstone", now=t_169h) is True


# ============================================================================
# Section 5: Reject aggregator postings without employer apply link
# ============================================================================

def test_s5_aggregator_without_employer_link_rejected(strict_matcher):
    for src, agg_url in [
        ("weworkremotely", "https://weworkremotely.com/remote-jobs/acme-solutions-engineer"),
        ("remoteok", "https://remoteok.com/remote-jobs/991122"),
        ("arbeitsagentur", "https://www.arbeitsagentur.de/jobsuche/jobdetail/10000-111-S"),
        ("englishjobsde", "https://englishjobs.de/clickout/12345"),
        ("arbeitnow", "https://www.arbeitnow.com/jobs/companies/acme/solutions-engineer-123"),
        ("germantechjobs", None),
        ("jobicy", "https://jobicy.com/jobs/12345"),
        ("himalayas", "https://himalayas.app/jobs/acme-solutions"),
        ("workingnomads", "https://www.workingnomads.com/jobs?job=123"),
        ("remotive", "https://remotive.com/remote-jobs/software-dev/solutions-engineer-1"),
    ]:
        job = JobPosting(
            id=f"{src}:test:1",
            title="Solutions Consultant",
            company="Acme Cloud",
            location="Munich, Germany",
            url=agg_url or "https://germantechjobs.de/jobs/acme",
            apply_url=agg_url,
            description=LONG_VALID_DESC,
            source=src,
        )
        ev = strict_matcher.evaluate(job)
        assert ev.gate_rejected == "no-employer-link", f"Expected {src} with {agg_url} to be rejected as no-employer-link, got {ev.gate_rejected}"

    # When aggregator posting HAS a real employer ATS link, it passes
    good_job = JobPosting(
        id="weworkremotely:test:good",
        title="Solutions Consultant",
        company="Acme Cloud",
        location="Munich, Germany",
        url="https://weworkremotely.com/remote-jobs/acme-solutions-consultant",
        apply_url="https://boards.greenhouse.io/acmecloud/jobs/987654",
        description=LONG_VALID_DESC,
        source="weworkremotely",
    )
    ev_good = strict_matcher.evaluate(good_job)
    assert ev_good.gate_rejected is None
    assert ev_good.score > 0


def test_s5_arbeitsagentur_externe_url_and_captcha_rejection(strict_matcher):
    # BA posting with no externeURL -> apply_url is None -> rejected as no-employer-link
    ok, reason = check_employer_link(
        "arbeitsagentur",
        None,
        "Bewerbung über die Bundesagentur für Arbeit. Bitte geben Sie die Dargestellte Zeichen ein.",
    )
    assert ok is False

    # BA scraper extracts externeURL when present
    mock_search = {
        "ergebnisliste": [
            {
                "stellenangebotsTitel": "Solutions Architect (m/f/d)",
                "firma": "Munich Tech GmbH",
                "referenznummer": "10000-777-S",
                "externeURL": "https://jobs.ashbyhq.com/munichtech/12345?utm_source=ba",
                "stellenlokationen": [{"adresse": {"ort": "München", "plz": "80331"}}],
            }
        ]
    }
    mock_detail = {
        "stellenangebotsBeschreibung": "We are looking for a Solutions Architect to design enterprise integrations in Munich. English is our working language.",
        "externeURL": "https://jobs.ashbyhq.com/munichtech/12345?utm_source=ba",
        "eintrittsdatum": "2026-11-01",
    }
    scraper = ArbeitsagenturScraper(titles=["Solutions Architect"])
    with patch("httpx.Client.get") as mock_get:
        def side_effect(url, **kwargs):
            res = MagicMock(status_code=200)
            res.json.return_value = mock_detail if "jobdetails" in url else mock_search
            return res

        mock_get.side_effect = side_effect
        jobs = scraper.scrape()
        assert len(jobs) == 1
        assert jobs[0].apply_url == "https://jobs.ashbyhq.com/munichtech/12345"


# ============================================================================
# Section 6: Catch hard blocks in code before export (with matched quote saved)
# ============================================================================

@pytest.mark.parametrize(
    "title,desc_snippet,expected_gate",
    [
        ("German-speaking Solutions Consultant", "Join our Munich team.", "german_required"),
        ("Solutions Consultant", "Requirements: You must be fluent in German and English.", "german_required"),
        ("Solutions Consultant", "We require fluency in German for DACH customer workshops.", "german_required"),
        ("Solutions Consultant", "Languages: German (C1) and English (C1).", "german_required"),
        ("Solutions Consultant", "Du bringst verhandlungssichere Deutsch- und Englischkenntnisse mit.", "german_required"),
        ("Solutions Consultant", "Fließend Deutsch in Wort und Schrift wird vorausgesetzt.", "german_required"),
        ("Solutions Consultant", "Sehr gute Deutschkenntnisse sowie Erfahrung im Consulting.", "german_required"),
        ("Solutions Consultant", "Deutsch (C2) ist erforderlich für diese Rolle.", "german_required"),
        ("Solutions Consultant", "This is a fixed-term position for our Munich office.", "contract"),
        ("Solutions Consultant", "Die Stelle ist zunächst befristet für Projektarbeit.", "contract"),
        ("Solutions Consultant (Maternity Cover)", "Join our Customer Success team.", "contract"),
        ("Solutions Consultant", "Diese Position ist eine Elternzeitvertretung in München.", "contract"),
        ("Solutions Consultant", "We offer an initial 12 months contract with high impact.", "contract"),
        ("Solutions Consultant", "Contract duration: 18 months role in Munich.", "contract"),
        ("Solutions Consultant", "This is a temporary position supporting our rollout.", "contract"),
        ("Part-time Solutions Consultant", "Work 20 hours per week.", "contract"),
        ("Solutions Consultant", "This position is a Werkstudent role in Munich.", "contract"),
        ("Solutions Consultant", "This position is a 6-month Praktikum in our Munich office.", "contract"),
    ],
)
def test_s6_hard_blocks_gate_and_save_quote(strict_matcher, title, desc_snippet, expected_gate):
    full_desc = f"{LONG_VALID_DESC}\n\n{desc_snippet}"
    gate, quote = check_hard_blocks(title, full_desc)
    assert gate == expected_gate, f"Expected {expected_gate} for '{title}' / '{desc_snippet}', got {gate}"
    assert quote and len(quote) > 0

    job = JobPosting(
        id="greenhouse:testco:1",
        title=title,
        company="TestCo",
        location="Munich, Germany",
        url="https://boards.greenhouse.io/testco/jobs/1",
        apply_url="https://boards.greenhouse.io/testco/jobs/1",
        description=full_desc,
        source="greenhouse",
    )
    ev = strict_matcher.evaluate(job)
    assert ev.gate_rejected in (expected_gate, "german" if expected_gate == "german_required" else expected_gate)
    assert ev.gate_reason and len(ev.gate_reason) > 0


def test_s6_german_is_a_plus_does_not_block(strict_matcher):
    for phrase in [
        "Fluent German is a plus.",
        "German (C1) is nice to have.",
        "Fließend Deutsch ist von Vorteil, aber kein Muss.",
    ]:
        gate, _ = check_hard_blocks("Solutions Consultant", f"{LONG_VALID_DESC} {phrase}")
        assert gate is None, f"Should not block on plus phrase: {phrase}"


# ============================================================================
# Section 7: Data quality (300-char description, first_seen, dedupe & cap, Clera)
# ============================================================================

def test_s7_short_description_rejected_and_intake_fixer_repairs(strict_matcher):
    short_job = JobPosting(
        id="greenhouse:acme:short1",
        title="Solutions Consultant",
        company="Acme GmbH",
        location="Munich, Germany",
        url="https://boards.greenhouse.io/acme/jobs/short1",
        apply_url="https://boards.greenhouse.io/acme/jobs/short1",
        description="Too short description.",
        source="greenhouse",
    )
    ev = strict_matcher.evaluate(short_job)
    assert ev.gate_rejected == "no-description"

    # IntakeFixer fetches >= 300 chars from public page and repairs it
    fake_client = MagicMock()
    fake_resp = MagicMock(status_code=200, headers={"content-type": "text/html"})
    fake_resp.text = f"<html><body><main><p>{LONG_VALID_DESC}</p></main></body></html>"
    fake_client.get.return_value = fake_resp

    fixer = IntakeFixer(max_fetches=5, client=fake_client)
    repaired = fixer.fix(ev)
    assert repaired is True
    assert len(ev.description) >= MIN_DESCRIPTION_CHARS

    ev2 = strict_matcher.evaluate(ev)
    assert ev2.gate_rejected is None
    assert ev2.score > 0


def test_s7_first_seen_backfilled_and_set(tmp_db):
    job = JobPosting(
        id="greenhouse:acme:fs1",
        title="Solutions Consultant",
        company="Acme GmbH",
        location="Munich, Germany",
        url="https://boards.greenhouse.io/acme/jobs/fs1",
        description=LONG_VALID_DESC,
        source="greenhouse",
        first_seen=None,
    )
    tmp_db.save_jobs([job])
    with tmp_db._get_connection() as conn:
        # Simulate a legacy row with NULL first_seen
        conn.execute("UPDATE jobs SET first_seen = NULL WHERE id = 'greenhouse:acme:fs1'")
    # Re-initializing Database runs the migration backfill
    db2 = Database(tmp_db.db_path)
    with db2._get_connection() as conn:
        row = conn.execute("SELECT first_seen, created_at, triple_key FROM jobs WHERE id = 'greenhouse:acme:fs1'").fetchone()
        assert row["first_seen"] is not None
        assert row["first_seen"] == row["created_at"]
        assert row["triple_key"] == triple_key("Acme GmbH", "Solutions Consultant", "Munich, Germany")


def test_s7_dedupe_triple_and_company_caps(tmp_db):
    # 1. Duplicate company + title + location in same batch
    dup_batch = [
        JobPosting(
            id=f"personio:prolificacademic:{i}",
            title="Operations Manager (m/w/d)",
            company="Prolific Academic Ltd",
            location="Munich, Germany",
            url=f"https://prolific.jobs.personio.de/job/{i}",
            description=LONG_VALID_DESC,
            source="personio",
        )
        for i in range(3)
    ]
    kept, stats = dedupe_and_cap(dup_batch)
    assert len(kept) == 1
    assert stats["duplicate_triple"] == 2

    # 2. Per-company cap (5) on prolificacademic across distinct titles
    many_distinct = [
        JobPosting(
            id=f"personio:prolificacademic:r{i}",
            title=f"Solutions Consultant Level {i}",
            company="Prolific Academic Ltd",
            location="Munich, Germany",
            url=f"https://prolific.jobs.personio.de/job/r{i}",
            description=LONG_VALID_DESC,
            source="personio",
        )
        for i in range(12)
    ]
    kept_cap, stats_cap = tmp_db.filter_new_duplicates(many_distinct)
    assert len(kept_cap) == 5
    assert stats_cap["company_cap"] == 7


def test_s7_clera_filter_role_family_and_location(strict_matcher):
    # Clera robotics / founder role in Munich -> rejected
    clera_bad_role = JobPosting(
        id="ashby:clera:1",
        title="Robotics Hardware Engineer",
        company="Clera",
        location="Munich, Germany",
        url="https://jobs.ashbyhq.com/clera/1",
        apply_url="https://jobs.ashbyhq.com/clera/1",
        description=LONG_VALID_DESC,
        source="ashby",
    )
    assert is_clera(clera_bad_role.id, clera_bad_role.company) is True
    ok, _ = check_clera(clera_bad_role.title, clera_bad_role.location)
    assert ok is False

    # Clera target role family in Berlin (not Munich / not remote EU) -> rejected
    ok_berlin, _ = check_clera("Solutions Engineer", "Berlin, Germany (On-site)")
    assert ok_berlin is False

    # Clera target role family in Munich -> passes
    clera_good = JobPosting(
        id="ashby:clera:2",
        title="Forward Deployed Solutions Engineer",
        company="Clera",
        location="Munich, Germany",
        url="https://jobs.ashbyhq.com/clera/2",
        apply_url="https://jobs.ashbyhq.com/clera/2",
        description=LONG_VALID_DESC,
        source="ashby",
    )
    ev_good = strict_matcher.evaluate(clera_good)
    assert ev_good.gate_rejected is None
    assert ev_good.score > 0
