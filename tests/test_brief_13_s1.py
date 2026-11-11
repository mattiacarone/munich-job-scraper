import pytest
import config
from database import Database
from matcher import ProfileMatcher
from models import JobPosting


@pytest.fixture
def matcher():
    m = ProfileMatcher(config.config.profile)
    m.load_role_rules()
    return m


def test_role_rules_fits_accepted_titles(matcher):
    # Target role families including newly allowed developer tracks
    assert matcher.title_fits_role_rules("Associate Solutions Architect") is True
    assert matcher.title_fits_role_rules("Software Developer") is True
    assert matcher.title_fits_role_rules("Backend Developer") is True
    assert matcher.title_fits_role_rules("Python Developer") is True
    assert matcher.title_fits_role_rules("AI Developer") is True
    assert matcher.title_fits_role_rules("Solutions Consultant") is True
    assert matcher.title_fits_role_rules("Integration Engineer") is True


def test_role_rules_rejects_unfitting_titles(matcher):
    # Seniority rejection
    assert matcher.title_fits_role_rules("Senior Software Engineer") is False
    assert matcher.title_fits_role_rules("Sales Director, EMEA") is False
    assert matcher.title_fits_role_rules("Lead Backend Engineer") is False
    # Family not rejection
    assert matcher.title_fits_role_rules("Recruitment Consultant") is False
    assert matcher.title_fits_role_rules("Talent Acquisition Specialist") is False
    assert matcher.title_fits_role_rules("Hardware Engineer") is False
    # Out of family
    # v7 (4 Nov 2026): marketing is family 5 and allowed in small numbers, so no assertion on "Marketing Manager"


def test_role_rules_precedence_over_off_function(matcher):
    # Software developer / backend developer would normally hit OFF_FUNCTION / ENGINEERING_ROLE_PAT
    # but role-rules takes precedence
    ok, reason = matcher.check_gate_off_function("Software Developer")
    assert ok is True
    assert reason is None

    ok, reason = matcher.check_gate_off_function("Backend Developer")
    assert ok is True
    assert reason is None

    ok, reason = matcher.check_gate_off_function("Python Developer")
    assert ok is True
    assert reason is None


def test_years_block_in_gate_4c(matcher):
    # Brief 14 A3: years_required is parsed and stored on the row, and never rejects
    desc_with_years = "We are seeking a candidate with 6+ years of experience in an engineering role."
    ok, reason = matcher.check_gate_years_required("Software Developer", desc_with_years)
    assert ok is True
    ok, reason = matcher.check_gate_years_required("Senior Software Developer", desc_with_years)
    assert ok is True
    assert matcher.parse_years_required("Senior Software Developer", desc_with_years)[0] == 6

    desc_ok = "We are seeking a junior candidate with 1-2 years of experience."
    ok, reason = matcher.check_gate_years_required("Software Developer", desc_ok)
    assert ok is True


def test_negative_keywords_gate(matcher):
    # Brief 13 Section 4: negative keywords title gate
    neg_titles = [
        "Construction Manager",
        "Senior Bauarbeiter gesucht",
        "Site Manager Munich",
        "Electrician (m/w/d)",
        "Automotive Mechanic",
        "Dental Assistant",
        "Technical Recruiter",
        "Recruitment Consultant Tech",
        "Procurement Specialist",
        "Purchasing Manager",
        "Thermal Engineer",
        "Propulsion Systems Specialist",
        "Powertrain Architect",
    ]
    for tit in neg_titles:
        ok, reason = matcher.check_gate_negative_keywords(tit)
        assert ok is False, f"Expected {tit} to be rejected by negative-keyword gate"
        assert "Negative keyword rejected" in reason


def test_arbeitnow_sales_director_gated(matcher, tmp_path):
    # An arbeitnow row titled 'Sales Director, EMEA' must end up with gate_rejected set
    job = JobPosting(
        id="arbeitnow:sales-director-emea-london-test",
        title="Sales Director, EMEA",
        company="Talos Trading",
        location="London",
        url="https://arbeitnow.com/jobs/123",
        description="Full description here",
        source="arbeitnow",
    )
    ev = matcher.evaluate(job)
    assert ev.gate_rejected is not None
    assert ev.gate_rejected in ("location", "role-rules", "seniority")


def test_deduplicate_all_jobs_never_unmasks_invalid_rows(matcher, tmp_path):
    # Ensure deduplicate_all_jobs does not set gate_rejected = None on invalid rows
    db = Database(str(tmp_path / "test_dedupe.db"))
    job1 = JobPosting(
        id="greenhouse:acme:1",
        company="Acme Corp",
        title="Sales Director, EMEA",
        location="London",
        url="https://example.com/1",
        gate_rejected="location",
        gate_reason="Location rejected: London",
        score=0.0,
        source="greenhouse",
    )
    job2 = JobPosting(
        id="arbeitnow:acme-sales-director",
        company="Acme Corp",
        title="Sales Director, EMEA",
        location="London",
        url="https://example.com/2",
        gate_rejected="location",
        gate_reason="Location rejected: London",
        score=0.0,
        source="arbeitnow",
    )
    db.save_jobs([job1, job2])

    dups = db.deduplicate_all_jobs(matcher=matcher)
    assert dups == 0

    with db._get_connection() as conn:
        rows = conn.execute("SELECT id, gate_rejected FROM jobs").fetchall()
        for r in rows:
            assert r["gate_rejected"] == "location"
