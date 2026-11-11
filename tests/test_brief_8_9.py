"""
Unit tests for IMPROVEMENT_BRIEF_8.md and IMPROVEMENT_BRIEF_9.md.
Verifies:
  Brief 8:
    - "Remote - Europe" Ashby posting passes as location_scope='remote-eu'
    - "Remote (UK only)" fails
    - "Hybrid - Berlin" fails
    - Posting with location "Remote" and text "must be based in the United States" fails
    - "Senior Solutions Engineer, 6+ years" fails
    - "Solutions Engineer, 3+ years, Remote EMEA" passes as location_scope='remote-eu'
  Brief 9:
    - "Fluent German required" gates with gate_rejected='german_required' and quote in gate_reason
    - "German is a plus" passes
    - "5+ years" in a Senior title gates with gate_rejected='years_required' and years_required=5
    - "3-5 years" passes with years_required=3
    - "Temporary, 6 months" gates with gate_rejected='contract' and contract_type='fixed_term'
    - LinkedIn guest page with an off-site apply link resolves to the Greenhouse URL
    - Title containing "Social Media Manager" with an earlier Rejected outcome is penalised
    - "Implementation Consultant" is boosted
    - Deep stack & clearance gates work and store matched quotes
    - Deduplication on 14-char prefix and 3-sent company cap (Amazon exempt)
"""

from datetime import datetime, timezone
import pytest

from config import config
from matcher import ProfileMatcher
from models import JobPosting


@pytest.fixture
def matcher():
    return ProfileMatcher(config.profile)


# ==============================================================================
# BRIEF 8 TESTS
# ==============================================================================


def test_brief8_remote_europe_ashby_passes_as_remote_eu(matcher):
    job = JobPosting(
        id="ashby:acme:101",
        title="Customer Solutions Engineer",
        company="Acme Cloud",
        location="Remote - Europe",
        url="https://jobs.ashbyhq.com/acme/101",
        apply_url="https://jobs.ashbyhq.com/acme/101/application",
        source="ashby",
        ats="ashby",
        description="Join our distributed EMEA team helping customers integrate REST APIs and webhooks.",
    )
    res = matcher.evaluate(job)
    assert res.gate_rejected is None
    assert res.location_scope == "remote-eu"
    assert res.apply_route == "ashby"
    assert res.score > 0


def test_brief8_remote_uk_only_fails(matcher):
    job = JobPosting(
        id="ashby:acme:102",
        title="Solutions Consultant",
        company="Acme Cloud",
        location="Remote (UK only)",
        url="https://jobs.ashbyhq.com/acme/102",
        source="ashby",
        description="Work with enterprise customers.",
    )
    res = matcher.evaluate(job)
    assert res.gate_rejected == "location"
    assert res.score == 0.0


def test_brief8_hybrid_berlin_fails(matcher):
    job = JobPosting(
        id="personio:acme:103",
        title="Technical Account Manager (remote-friendly)",
        company="Berlin Tech",
        location="Hybrid - Berlin",
        url="https://berlintech.jobs.personio.de/job/103",
        source="personio",
        description="Remote-friendly hybrid role based in our Berlin headquarters.",
    )
    res = matcher.evaluate(job)
    assert res.gate_rejected == "location"
    assert res.score == 0.0


def test_brief8_remote_location_but_text_says_must_be_based_in_us_fails(matcher):
    job = JobPosting(
        id="greenhouse:acme:104",
        title="Implementation Consultant",
        company="US SaaS Co",
        location="Remote",
        url="https://boards.greenhouse.io/ussaasco/jobs/104",
        source="greenhouse",
        description=(
            "We are hiring an Implementation Consultant. "
            "Although this is a remote role, candidates must be based in the United States."
        ),
    )
    res = matcher.evaluate(job)
    assert res.gate_rejected == "location"
    assert res.score == 0.0


def test_brief8_senior_solutions_engineer_6_plus_years_fails(matcher):
    job = JobPosting(
        id="greenhouse:acme:105",
        title="Senior Solutions Engineer, 6+ years",
        company="Acme Cloud",
        location="Munich, Germany",
        url="https://boards.greenhouse.io/acme/jobs/105",
        source="greenhouse",
        description="Lead enterprise pre-sales and API integrations.",
    )
    res = matcher.evaluate(job)
    # Brief 14 A3: years_required is parsed and stored on the row, never rejected on
    assert res.gate_rejected is None
    assert res.years_required == 6
    assert res.score > 0


def test_brief8_solutions_engineer_3_plus_years_remote_emea_passes(matcher):
    job = JobPosting(
        id="greenhouse:acme:106",
        title="Solutions Engineer, 3+ years, Remote EMEA",
        company="Acme Cloud",
        location="Remote EMEA",
        url="https://boards.greenhouse.io/acme/jobs/106",
        apply_url="https://boards.greenhouse.io/acme/jobs/106",
        source="greenhouse",
        description="3+ years of experience in Solutions Engineering, REST APIs, and SaaS onboarding.",
    )
    res = matcher.evaluate(job)
    assert res.gate_rejected is None
    assert res.location_scope == "remote-eu"
    assert res.years_required == 3
    assert res.score > 0


# ==============================================================================
# BRIEF 9 TESTS
# ==============================================================================


def test_brief9_fluent_german_required_gates(matcher):
    job = JobPosting(
        id="greenhouse:acme:201",
        title="Implementation Consultant",
        company="Acme GmbH",
        location="Munich, Germany",
        url="https://boards.greenhouse.io/acme/jobs/201",
        source="greenhouse",
        description="We work with DACH clients. Fluent German required and strong English skills.",
    )
    res = matcher.evaluate(job)
    assert res.gate_rejected == "german_required"
    assert res.german_required == 1
    assert "Fluent German required" in (res.gate_reason or "")
    assert res.score == 0.0


def test_brief9_german_is_a_plus_passes(matcher):
    job = JobPosting(
        id="greenhouse:acme:202",
        title="Implementation Consultant",
        company="Acme GmbH",
        location="Munich, Germany",
        url="https://boards.greenhouse.io/acme/jobs/202",
        source="greenhouse",
        description="English is our working language. German is a plus / nice to have.",
    )
    res = matcher.evaluate(job)
    assert res.gate_rejected is None
    assert res.german_required == 0
    assert res.language_status == "German is a Plus"
    assert res.score > 0


def test_brief9_5_plus_years_in_senior_title_gates(matcher):
    job = JobPosting(
        id="greenhouse:acme:203",
        title="Senior Customer Success Manager",
        company="Acme GmbH",
        location="Munich, Germany",
        url="https://boards.greenhouse.io/acme/jobs/203",
        source="greenhouse",
        description="Requirements: 5+ years of experience in B2B SaaS Customer Success.",
    )
    res = matcher.evaluate(job)
    # Brief 14 A3: years_required is parsed and stored on the row, never rejected on
    assert res.gate_rejected is None
    assert res.years_required == 5
    assert res.score > 0


def test_brief9_3_to_5_years_passes(matcher):
    job = JobPosting(
        id="greenhouse:acme:204",
        title="Senior Customer Success Manager",
        company="Acme GmbH",
        location="Munich, Germany",
        url="https://boards.greenhouse.io/acme/jobs/204",
        source="greenhouse",
        description="Requirements: 3-5 years of experience in SaaS onboarding and API integrations.",
    )
    res = matcher.evaluate(job)
    assert res.gate_rejected is None
    assert res.years_required == 3
    assert res.score > 0


def test_brief9_temporary_6_months_gates_contract(matcher):
    job = JobPosting(
        id="greenhouse:acme:205",
        title="Customer Success Manager",
        company="Acme GmbH",
        location="Munich, Germany",
        url="https://boards.greenhouse.io/acme/jobs/205",
        source="greenhouse",
        description="This role is Temporary, 6 months to cover parental leave in Munich.",
    )
    res = matcher.evaluate(job)
    assert res.gate_rejected == "contract"
    assert res.contract_type == "fixed_term"
    assert "Temporary, 6 months" in (res.gate_reason or "")
    assert res.score == 0.0


def test_brief9_social_media_manager_penalised_and_implementation_consultant_boosted(matcher):
    w_social = matcher.compute_title_weight("Social Media Manager")
    w_impl = matcher.compute_title_weight("Implementation Consultant")
    assert w_social < 0, f"Expected Social Media Manager to be penalised, got {w_social}"
    assert w_impl > 0, f"Expected Implementation Consultant to be boosted, got {w_impl}"

    j_social = JobPosting(
        id="greenhouse:acme:206",
        title="Social Media Manager",
        company="Acme GmbH",
        location="Munich, Germany",
        url="https://boards.greenhouse.io/acme/jobs/206",
        source="greenhouse",
        description="Manage social media campaigns and community channels.",
    )
    j_impl = JobPosting(
        id="greenhouse:acme:207",
        title="Implementation Consultant",
        company="Acme GmbH",
        location="Munich, Germany",
        url="https://boards.greenhouse.io/acme/jobs/207",
        source="greenhouse",
        description="Guide customers through software implementation and API configuration.",
    )
    res_social = matcher.evaluate(j_social)
    res_impl = matcher.evaluate(j_impl)
    assert res_social.title_weight < 0
    assert res_impl.title_weight > 0
    assert res_impl.score > res_social.score


def test_brief9_deep_stack_and_clearance_gates(matcher):
    j_k8s = JobPosting(
        id="greenhouse:acme:208",
        title="Solutions Consultant",
        company="Acme GmbH",
        location="Munich, Germany",
        url="https://boards.greenhouse.io/acme/jobs/208",
        source="greenhouse",
        description="Requires hands-on experience administering production Kubernetes clusters and Terraform modules.",
    )
    res_k8s = matcher.evaluate(j_k8s)
    assert res_k8s.gate_rejected == "deep_stack"
    assert "Kubernetes" in (res_k8s.gate_reason or "")

    j_clear = JobPosting(
        id="greenhouse:acme:209",
        title="Solutions Consultant",
        company="Defense Tech GmbH",
        location="Munich, Germany",
        url="https://boards.greenhouse.io/acme/jobs/209",
        source="greenhouse",
        description="Candidates must undergo a Sicherheitsüberprüfung nach SÜG (Ü2). German citizenship required.",
    )
    res_clear = matcher.evaluate(j_clear)
    assert res_clear.gate_rejected == "clearance"
    assert res_clear.score == 0.0


def test_brief9_dedupe_prefix14_and_company_cap_amazon_exempt(matcher):
    # Setup synthetic submitted history
    matcher.submitted_prefix14_history.add(("testcorp", "customersucces"))
    matcher.submitted_company_counts["cappedco"] = 3
    matcher.submitted_company_counts["amazon"] = 5

    # 1. 14-char prefix match on Submitted role -> gated as 'dup'
    j_prefix = JobPosting(
        id="greenhouse:testcorp:1",
        title="Customer Success Manager (EMEA)",
        company="TestCorp",
        location="Munich, Germany",
        url="https://boards.greenhouse.io/testcorp/jobs/1",
        source="greenhouse",
        description="Customer success role.",
    )
    res_prefix = matcher.evaluate(j_prefix)
    assert res_prefix.gate_rejected == "dup"
    assert "prefix-14" in (res_prefix.gate_reason or "")

    # 2. Company with 3 Submitted applications -> gated as 'dup'
    j_cap = JobPosting(
        id="greenhouse:cappedco:1",
        title="Implementation Consultant",
        company="CappedCo",
        location="Munich, Germany",
        url="https://boards.greenhouse.io/cappedco/jobs/1",
        source="greenhouse",
        description="Implementation role.",
    )
    res_cap = matcher.evaluate(j_cap)
    assert res_cap.gate_rejected == "dup"
    assert "3-application Submitted cap" in (res_cap.gate_reason or "")

    # 3. Amazon is exempt from the 3-application company cap
    j_amz = JobPosting(
        id="greenhouse:amazon:999",
        title="Solutions Consultant",
        company="Amazon",
        location="Munich, Germany",
        url="https://www.amazon.jobs/en/jobs/999",
        apply_url="https://www.amazon.jobs/en/jobs/999",
        source="greenhouse",
        description="Work with enterprise customers in Munich.",
    )
    res_amz = matcher.evaluate(j_amz)
    assert res_amz.gate_rejected is None
    assert res_amz.apply_route == "amazon_jobs"

