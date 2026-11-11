"""
Tests for Munich job expansion, suburb boundaries, customer-facing technical exceptions,
slug catalog expansions, and Firecrawl fallback handling.
"""
from unittest.mock import MagicMock
import pytest

from models import JobPosting
from matcher import ProfileMatcher, MUNICH_SUBURBS
from config import ProfileConfig
from company_discovery import KNOWN_MUNICH_SLUGS, EXPANDED_MUNICH_SLUGS
import intake_checks


def test_expanded_munich_suburbs():
    matcher = ProfileMatcher(ProfileConfig())
    new_suburbs = [
        "Parsdorf",
        "Manching",
        "Brunnthal",
        "Olching",
        "Puchheim",
        "Sauerlach",
        "Ebersberg",
        "Bergkirchen",
        "Maisach",
    ]
    for suburb in new_suburbs:
        assert suburb.lower() in MUNICH_SUBURBS, f"{suburb} not in MUNICH_SUBURBS"
        job = JobPosting(
            id=f"test:{suburb.lower()}",
            title="Senior Solutions Engineer",
            company="Munich ScaleUp GmbH",
            location=f"{suburb}, Germany",
            url=f"https://boards.greenhouse.io/test/{suburb.lower()}",
            description="We are seeking an experienced Solutions Engineer with APIs, customer architecture, and fluent English.",
            source="greenhouse",
        )
        evaluated = matcher.evaluate(job)
        assert evaluated.gate_rejected is None, f"Suburb {suburb} rejected with {evaluated.gate_rejected}"
        assert evaluated.location_scope == "munich", f"Expected munich scope for {suburb}, got {evaluated.location_scope}"


def test_forward_deployed_and_solutions_exceptions():
    matcher = ProfileMatcher(ProfileConfig())

    # Roles that should NOT be gated as off-function
    passing_titles = [
        "Forward Deployed AI Engineer",
        "Forward Deployed Software Engineer",
        "AI Solutions Engineer",
        "Solutions Engineer",
        "Senior Solutions Engineer",
        "Pre-Sales Solutions Consultant",
    ]
    for title in passing_titles:
        job = JobPosting(
            id=f"test:{title.replace(' ', '_').lower()}",
            title=title,
            company="AI Innovations GmbH",
            location="Munich, Germany",
            url="https://jobs.ashbyhq.com/ai/123",
            description="Lead forward deployed engineering and customer implementations for enterprise accounts. Fluent English.",
            source="ashby",
        )
        evaluated = matcher.evaluate(job)
        assert evaluated.gate_rejected != "off-function", f"Expected '{title}' to pass off-function gate, got {evaluated.gate_rejected}"

    # Pure engineering roles that should STILL be gated as off-function
    rejected_titles = [
        "Software Engineer - Backend",
        "Fullstack Software Engineer",
        "Senior AI Research Scientist",
        "Machine Learning Engineer - NLP Core",
        "Infrastructure DevOps Engineer",
    ]
    for title in rejected_titles:
        job = JobPosting(
            id=f"test:{title.replace(' ', '_').lower()}",
            title=title,
            company="AI Innovations GmbH",
            location="Munich, Germany",
            url="https://jobs.ashbyhq.com/ai/456",
            description="Write backend Python microservices and optimize SQL database queries.",
            source="ashby",
        )
        evaluated = matcher.evaluate(job)
        assert evaluated.gate_rejected == "off-function", f"Expected '{title}' to be rejected as off-function, got {evaluated.gate_rejected}"


def test_known_and_expanded_munich_slugs():
    # Verify Brief 12 baseline (26 slugs)
    assert sum(len(v) for v in KNOWN_MUNICH_SLUGS.values()) == 26
    assert "clera" in KNOWN_MUNICH_SLUGS["ashby"]
    assert "appliedai" in KNOWN_MUNICH_SLUGS["personio"]

    # Verify expanded Munich scaleups across ATS platforms
    ashby_slugs = EXPANDED_MUNICH_SLUGS.get("ashby", [])
    greenhouse_slugs = EXPANDED_MUNICH_SLUGS.get("greenhouse", [])
    personio_slugs = EXPANDED_MUNICH_SLUGS.get("personio", [])
    smartrecruiters_slugs = EXPANDED_MUNICH_SLUGS.get("smartrecruiters", [])

    assert "robco" in ashby_slugs
    assert "the-exploration-company" in ashby_slugs
    assert "proxima-fusion" in ashby_slugs
    assert "isaraerospace" in greenhouse_slugs
    assert "navvis" in greenhouse_slugs
    assert "parloa" in greenhouse_slugs
    assert "twaice" in personio_slugs
    assert "ottonova" in personio_slugs
    assert "brainlab" in smartrecruiters_slugs
