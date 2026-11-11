"""
Unit & Integration Tests for Brief 5 upgrades.
Verifies:
1. Host class classification for each class (auto, captcha, account, easy_apply, unknown).
2. Arbeitnow /apply redirect resolved to a Greenhouse URL (mock httpx).
3. Arbeitnow row left on arbeitnow.* rejected as 'no-employer-link'.
4. Workday URL rejected as 'account-only'.
5. The four audit leaks rejected.
6. "Customer Solutions Engineer, EMEA" and "Senior Customer Success Manager" still accepted.
7. cycle_metrics.passed equals audit candidate count.
8. data/filters.json contains the required keys.
"""

import json
from datetime import datetime, timezone
from pathlib import Path
from unittest.mock import MagicMock
import pytest
from apply_classifier import classify_apply_kind, strip_utm_params
from config import ProfileConfig, config
from database import Database
from matcher import ProfileMatcher, write_filters_json
from models import JobPosting
from scrapers.arbeitnow import resolve_arbeitnow_job


@pytest.fixture
def matcher():
    return ProfileMatcher(config.profile)


@pytest.fixture
def tmp_db(tmp_path):
    db_file = tmp_path / "test_jobs.db"
    return Database(db_file)


# ------------------------------------------------------------------------------
# 1. Host class classification for each class
# ------------------------------------------------------------------------------

def test_apply_kind_host_classes():
    # auto
    assert classify_apply_kind(apply_url="https://boards.greenhouse.io/company/jobs/1")[0] == "auto"
    assert classify_apply_kind(apply_url="https://job-boards.eu.greenhouse.io/company/jobs/2")[0] == "auto"
    assert classify_apply_kind(apply_url="https://jobs.lever.co/company/abc")[0] == "auto"
    assert classify_apply_kind(apply_url="https://company.jobs.personio.de/job/123")[0] == "auto"
    assert classify_apply_kind(apply_url="https://apply.workable.com/company/j/xyz")[0] == "auto"
    assert classify_apply_kind(apply_url="https://jobs.smartrecruiters.com/company/123")[0] == "auto"
    assert classify_apply_kind(apply_url="https://company.teamtailor.com/jobs/123")[0] == "auto"
    assert classify_apply_kind(apply_url="https://join.com/companies/test/123")[0] in ("auto", "join-account")

    # captcha
    assert classify_apply_kind(apply_url="https://company.recruitee.com/o/job")[0] == "captcha"
    assert classify_apply_kind(apply_url="https://company.bamboohr.com/careers/1")[0] == "captcha"
    assert classify_apply_kind(apply_url="https://jobs.ashbyhq.com/company/abc")[0] in ("captcha", "auto")
    assert classify_apply_kind(apply_url="https://some-job.com/apply", html_text="<div>Please solve hcaptcha</div>")[0] == "captcha"

    # account
    assert classify_apply_kind(apply_url="https://nvidia.wd5.myworkdayjobs.com/NVIDIAExternalCareerSite")[0] == "account"
    assert classify_apply_kind(apply_url="https://career5.successfactors.eu/career?company=test")[0] == "account"
    assert classify_apply_kind(apply_url="https://jobs.sap.com/job/Munich-Dev/123")[0] == "account"
    assert classify_apply_kind(apply_url="https://amazon.jobs/en/jobs/123")[0] == "account"
    assert classify_apply_kind(apply_url="https://foo.oraclecloud.com/hcmUI/CandidateExperience")[0] == "account"
    assert classify_apply_kind(apply_url="https://foo.taleo.net/careersection/jobdetail")[0] == "account"
    assert classify_apply_kind(apply_url="https://careers.icims.com/jobs/123")[0] == "account"
    assert classify_apply_kind(apply_url="https://foo.phenompeople.com/jobs/123")[0] == "account"
    assert classify_apply_kind(apply_url="https://foo.eightfold.ai/careers/job/123")[0] == "account"

    # easy_apply
    assert classify_apply_kind(url="https://www.linkedin.com/jobs/view/123", source="linkedin")[0] == "easy_apply"

    # unknown
    assert classify_apply_kind(apply_url="https://somecompany.com/custom-form")[0] == "unknown"


# ------------------------------------------------------------------------------
# 2. Arbeitnow /apply redirect resolved to a Greenhouse URL (mock httpx)
# ------------------------------------------------------------------------------

def test_arbeitnow_apply_redirect_resolution(tmp_db):
    mock_client = MagicMock()
    mock_response = MagicMock()
    mock_response.status_code = 200
    mock_response.url = "https://boards.greenhouse.io/databricks/jobs/8720995002?utm_source=arbeitnow.com&ref=aggregator"
    mock_client.get.return_value = mock_response

    job = JobPosting(
        id="arbeitnow:test-job-123",
        title="Solutions Architect",
        company="Databricks",
        location="Munich, Germany",
        url="https://www.arbeitnow.com/jobs/companies/databricks/test-job-123",
        source="arbeitnow",
    )

    resolved = resolve_arbeitnow_job(job, mock_client, db=tmp_db)

    assert resolved.apply_url == "https://boards.greenhouse.io/databricks/jobs/8720995002"
    assert resolved.apply_kind == "auto"
    assert resolved.ats == "greenhouse"
    assert resolved.ats_slug == "databricks"
    assert resolved.gate_rejected is None

    # Check database cache was populated
    cached = tmp_db.get_resolved_apply_url("https://www.arbeitnow.com/jobs/companies/databricks/test-job-123/apply")
    assert cached is not None
    assert cached["final_url"] == "https://boards.greenhouse.io/databricks/jobs/8720995002"


# ------------------------------------------------------------------------------
# 3. An Arbeitnow row left on arbeitnow.* is rejected as 'no-employer-link'
# ------------------------------------------------------------------------------

def test_arbeitnow_row_left_on_arbeitnow_rejected(tmp_db, matcher):
    mock_client = MagicMock()
    mock_response = MagicMock()
    mock_response.status_code = 200
    mock_response.url = "https://www.arbeitnow.com/jobs/companies/acme/aggregator-page-456"
    mock_client.get.return_value = mock_response

    job = JobPosting(
        id="arbeitnow:aggregator-page-456",
        title="Integration Specialist",
        company="Acme Corp",
        location="Munich, Germany",
        url="https://www.arbeitnow.com/jobs/companies/acme/aggregator-page-456",
        source="arbeitnow",
    )

    resolved = resolve_arbeitnow_job(job, mock_client, db=tmp_db)
    assert resolved.gate_rejected == "no-employer-link"
    assert resolved.score == 0.0

    # Also test matcher gate evaluation
    eval_res = matcher.evaluate(job)
    assert eval_res.gate_rejected == "no-employer-link"
    assert eval_res.score == 0.0


# ------------------------------------------------------------------------------
# 4. Workday URL rejected as 'account-only'
# ------------------------------------------------------------------------------

def test_workday_url_rejected_as_account_only(matcher):
    job = JobPosting(
        id="greenhouse:nvidia:12345",
        title="Solution Consultant",
        company="Nvidia",
        location="Munich, Germany",
        url="https://nvidia.wd5.myworkdayjobs.com/NVIDIAExternalCareerSite/job/Munich",
        apply_url="https://nvidia.wd5.myworkdayjobs.com/NVIDIAExternalCareerSite/job/Munich",
    )

    result = matcher.evaluate(job)
    # Brief 9 Section 3: Workday rows get apply_route='workday' and needs_account=1 (parked for Mattia, not gated)
    assert result.apply_route == "workday"
    assert result.needs_account == 1
    assert result.gate_rejected is None


# ------------------------------------------------------------------------------
# 5. The four audit leaks rejected
# ------------------------------------------------------------------------------

def test_four_audit_leaks_rejected(matcher):
    # 1. Wimex Handelsvertreter title
    wimex = JobPosting(
        id="recruitee:wimexgruppe:2361620",
        title="selbstständiger Handelsvertreter (m/w/d) Vertrieb landwirtschaftliche Software",
        company="Wimex-Gruppe",
        location="Munich, Germany",
        url="https://wimex.recruitee.com/o/1",
        description="Vertriebsrolle",
    )
    res_wimex = matcher.evaluate(wimex)
    assert res_wimex.gate_rejected in {"german", "off-function"}
    assert res_wimex.score == 0.0

    # 2. Agile Robots New Product Introduction Engineer with German sentence
    agile = JobPosting(
        id="greenhouse:agilerobotsse:4997311101",
        title="New Product Introduction Engineer (m/f/d)",
        company="Agile Robots SE",
        location="Munich, Germany",
        url="https://boards.greenhouse.io/agilerobotsse/jobs/4997311101",
        description="<li>Verhandlungssicheres Deutsch und Englisch</li>",
    )
    res_agile = matcher.evaluate(agile)
    assert res_agile.gate_rejected in {"german", "german_required", "off-function"}
    assert res_agile.score == 0.0

    # 3. Baringa (Senior) Manager with professional proficiency in German
    baringa = JobPosting(
        id="greenhouse:baringa:4999137101",
        title="(Senior) Manager (m/w/d), Commodities & Energy Trading",
        company="Baringa",
        location="Munich, Germany",
        url="https://boards.greenhouse.io/baringa/jobs/4999137101",
        description="<li>Professional proficiency in German and English</li>",
    )
    res_baringa = matcher.evaluate(baringa)
    assert res_baringa.gate_rejected in {"german", "german_required"}
    assert res_baringa.score == 0.0

    # 4. Impossiblecloud foreign territory (UK&I) sales manager
    impossible = JobPosting(
        id="arbeitnow:remote-senior-sales-manager-cloud-solutions-uki-476136",
        title="(Senior) Sales Manager - Cloud Solutions (UK&I)",
        company="Impossiblecloud",
        location="Remote EU",
        url="https://www.arbeitnow.com/jobs/companies/impossiblecloud/remote-senior-sales-manager-cloud-solutions-uki-476136",
        source="arbeitnow",
        description="Own the UK&I revenue number for our storage product.",
    )
    res_imp = matcher.evaluate(impossible)
    assert res_imp.gate_rejected in {"location", "no-employer-link"}
    assert res_imp.score == 0.0

    # Also test if apply_url was resolved to Lever: still rejected by location
    impossible_with_link = JobPosting(
        id="arbeitnow:remote-senior-sales-manager-cloud-solutions-uki-476136",
        title="(Senior) Sales Manager - Cloud Solutions (UK&I)",
        company="Impossiblecloud",
        location="Remote EU",
        url="https://www.arbeitnow.com/jobs/companies/impossiblecloud/remote-senior-sales-manager-cloud-solutions-uki-476136",
        apply_url="https://jobs.lever.co/impossiblecloud/487c87b4-9a45-4e81-a084-b1a4f77eb774/apply",
        source="arbeitnow",
        description="Own the UK&I revenue number for our storage product.",
    )
    res_imp2 = matcher.evaluate(impossible_with_link)
    assert res_imp2.gate_rejected == "location"
    assert res_imp2.score == 0.0


# ------------------------------------------------------------------------------
# 6. "Customer Solutions Engineer, EMEA" and "Senior Customer Success Manager" accepted
# ------------------------------------------------------------------------------

def test_target_roles_still_accepted(matcher):
    j1 = JobPosting(
        id="greenhouse:target:1",
        title="Customer Solutions Engineer, EMEA",
        company="Enterprise Tech",
        location="Munich, Germany",
        url="https://boards.greenhouse.io/target/jobs/1",
        description="Work with enterprise customers on integration and APIs.",
        source="greenhouse",
    )
    res1 = matcher.evaluate(j1)
    assert res1.gate_rejected is None
    assert res1.score >= 30.0

    j2 = JobPosting(
        id="greenhouse:target:2",
        title="Senior Customer Success Manager",
        company="SaaS Corp",
        location="Munich, Germany",
        url="https://boards.greenhouse.io/target/jobs/2",
        description="Manage enterprise client relationships and adoption.",
        source="greenhouse",
    )
    res2 = matcher.evaluate(j2)
    assert res2.gate_rejected is None
    assert res2.score >= 30.0


# ------------------------------------------------------------------------------
# 7. cycle_metrics.passed equals audit candidate count
# ------------------------------------------------------------------------------

def test_cycle_metrics_passed_equals_audit_count(tmp_db):
    now_str = datetime.now(timezone.utc).isoformat()
    # Insert 2 passed jobs and 1 rejected job
    jobs = [
        JobPosting(
            id="greenhouse:acme:1",
            title="Solutions Engineer",
            company="Acme",
            location="Munich",
            url="https://boards.greenhouse.io/acme/1",
            score=45.0,
            gate_rejected=None,
            apply_kind="auto",
        ),
        JobPosting(
            id="greenhouse:acme:2",
            title="Customer Success Architect",
            company="Acme",
            location="Munich",
            url="https://boards.greenhouse.io/acme/2",
            score=50.0,
            gate_rejected=None,
            apply_kind="auto",
        ),
        JobPosting(
            id="greenhouse:acme:3",
            title="Software Developer",
            company="Acme",
            location="Munich",
            url="https://boards.greenhouse.io/acme/3",
            score=0.0,
            gate_rejected="off-function",
            apply_kind="auto",
        ),
    ]
    tmp_db.save_jobs(jobs)

    with tmp_db._get_connection() as conn:
        audit_count = conn.execute(
            "SELECT COUNT(*) FROM jobs WHERE gate_rejected IS NULL AND score > 0 AND closed_at IS NULL"
        ).fetchone()[0]

    tmp_db.record_cycle_metrics(
        started_at=now_str,
        duration_s=12.5,
        per_source_postings={"greenhouse": 3},
        per_source_errors={},
        new_rows=3,
        rejected_gates={"off-function": 1},
        passed=audit_count,
        closed=0,
        new_boards_found=0,
        linkedin_pauses=0,
        passed_total=audit_count,
        by_apply_kind={"auto": 2},
        by_source={"greenhouse": 2},
        exported=2,
    )

    recent = tmp_db.get_recent_cycle_metrics(limit=1)[0]
    assert recent["passed"] == 2
    assert recent["passed"] == audit_count
    assert recent["passed_total"] == 2


# ------------------------------------------------------------------------------
# 8. data/filters.json contains required keys
# ------------------------------------------------------------------------------

def test_filters_json_contains_required_keys(tmp_path):
    target_file = tmp_path / "filters.json"
    written_path = write_filters_json(target_file)
    assert written_path.exists()

    with open(written_path, "r", encoding="utf-8") as f:
        data = json.load(f)

    required_keys = [
        "german_title_words",
        "off_profile_title_words",
        "mandatory_german_patterns",
        "soft_words",
    ]

    for k in required_keys:
        assert k in data, f"Missing required key: {k}"
        assert isinstance(data[k], list), f"Expected list for {k}"
        assert len(data[k]) > 0, f"Expected non-empty list for {k}"
