"""
Tests for IMPROVEMENT_BRIEF_7b:
1. Never default a location to Munich (location=None, location_source='unknown'; passes only if posting text names Munich/München or Germany-remote).
2. Arbeitnow country sites: use posting's own location or infer non-German city/country from title/text; reject via location gate.
3. Teamtailor: read location from JSON-LD (jobLocation.address) or Locations block; non-German domain requires explicit Munich/Germany location.
4. Posting language gate: reject (gate 'language') postings written mostly in French, Swedish, Dutch, Spanish, Italian, or Polish.
"""

from config import ProfileConfig
from matcher import ProfileMatcher
from models import JobPosting
from scrapers.arbeitnow import infer_arbeitnow_location
from scrapers.teamtailor import parse_teamtailor_job_page


def test_arbeitnow_fr_montpellier_rejected():
    """An arbeitnow.fr 'Account Manager Montpellier' row is rejected."""
    matcher = ProfileMatcher(ProfileConfig())
    loc, loc_source = infer_arbeitnow_location(
        raw_location="",
        title="Account Manager Montpellier",
        description="<p>We are hiring an Account Manager in Montpellier, France to support our enterprise clients.</p>",
        url="https://www.arbeitnow.fr/jobs/companies/acme/account-manager-montpellier-12345",
    )
    job = JobPosting(
        id="arbeitnow:account-manager-montpellier-12345",
        source="arbeitnow",
        company="Acme France",
        title="Account Manager Montpellier",
        location=loc,
        location_source=loc_source,
        description="We are hiring an Account Manager in Montpellier, France to support our enterprise clients.",
        url="https://www.arbeitnow.fr/jobs/companies/acme/account-manager-montpellier-12345",
        apply_url="https://acme.com/careers/12345",
        apply_kind="auto",
    )
    evaluated = matcher.evaluate(job)
    assert evaluated.gate_rejected == "location"
    assert evaluated.score == 0.0


def test_vwfs_fr_teamtailor_french_posting_rejected():
    """A vwfs.fr Teamtailor French posting is rejected."""
    matcher = ProfileMatcher(ProfileConfig())
    html_text = """
    <html>
    <head>
      <title>Chargé de Clientèle Entreprises (H/F) - Volkswagen Financial Services France</title>
      <script type="application/ld+json">
      {
        "@context": "http://schema.org",
        "@type": "JobPosting",
        "title": "Chargé de Clientèle Entreprises (H/F)",
        "description": "Nous recherchons un Chargé de Clientèle pour rejoindre notre équipe commerciale à Roissy. Vous serez en charge de la gestion du portefeuille clients et du développement des ventes.",
        "jobLocation": {
          "@type": "Place",
          "address": {
            "@type": "PostalAddress",
            "addressLocality": "Roissy-en-France",
            "addressCountry": "FR"
          }
        }
      }
      </script>
    </head>
    <body></body>
    </html>
    """
    parsed = parse_teamtailor_job_page(html_text)
    assert parsed["location"] is not None
    assert "Roissy-en-France" in parsed["location"]

    # 1. Evaluated through full pipeline (rejected by location or language)
    job = JobPosting(
        id="teamtailor:vwfs:99901",
        source="teamtailor",
        ats="teamtailor",
        company="Vwfs",
        title=parsed["title"] or "Chargé de Clientèle Entreprises (H/F)",
        location=parsed["location"],
        location_source=parsed["location_source"],
        description=parsed["description"],
        url="https://carriere.vwfs.fr/jobs/99901-charge-de-clientele-entreprises-h-f",
        apply_url="https://carriere.vwfs.fr/jobs/99901-charge-de-clientele-entreprises-h-f/applications/new",
        apply_kind="auto",
    )
    evaluated = matcher.evaluate(job)
    assert evaluated.gate_rejected in ("location", "language")
    assert evaluated.score == 0.0

    # 2. Explicitly verify the language gate rejects French text even if location were Munich
    ok_lang, reason_lang = matcher.check_gate_language(job.title, job.description)
    assert not ok_lang
    assert "French" in (reason_lang or "")

    job_with_munich_loc = JobPosting(
        id="teamtailor:vwfs:99902",
        source="teamtailor",
        ats="teamtailor",
        company="Vwfs",
        title="Solutions Consultant",
        location="Munich, Germany",
        location_source="json-ld",
        description=parsed["description"],
        url="https://vwfs.teamtailor.com/jobs/99902",
        apply_url="https://vwfs.teamtailor.com/jobs/99902/applications/new",
        apply_kind="auto",
    )
    eval_lang = matcher.evaluate(job_with_munich_loc)
    assert eval_lang.gate_rejected == "language"


def test_teamtailor_com_munich_json_ld_passes():
    """A Teamtailor posting on a .com site with 'Munich, Germany' in JSON-LD passes."""
    matcher = ProfileMatcher(ProfileConfig())
    html_text = """
    <html>
    <head>
      <title>Solutions Consultant (all genders) - Acme Cloud</title>
      <script type="application/ld+json">
      {
        "@context": "http://schema.org",
        "@type": "JobPosting",
        "title": "Solutions Consultant",
        "datePosted": "2026-10-15T10:00:00Z",
        "description": "<p>We are looking for a Solutions Consultant to join our team in Munich. You will work with REST APIs, SaaS integrations, Salesforce, and enterprise customers across Europe. English is our working language.</p>",
        "jobLocation": [
          {
            "@type": "Place",
            "address": {
              "@type": "PostalAddress",
              "addressLocality": "Munich",
              "addressCountry": "Germany"
            }
          }
        ]
      }
      </script>
    </head>
    <body></body>
    </html>
    """
    parsed = parse_teamtailor_job_page(html_text)
    assert parsed["location"] == "Munich, Germany"
    assert parsed["location_source"] == "json-ld"

    job = JobPosting(
        id="teamtailor:acmecloud:1001",
        source="teamtailor",
        ats="teamtailor",
        company="Acme Cloud",
        title=parsed["title"] or "Solutions Consultant",
        location=parsed["location"],
        location_source=parsed["location_source"],
        description=parsed["description"],
        url="https://acmecloud.teamtailor.com/jobs/1001-solutions-consultant",
        apply_url="https://acmecloud.teamtailor.com/jobs/1001-solutions-consultant/applications/new",
        apply_kind="auto",
    )
    evaluated = matcher.evaluate(job)
    assert evaluated.gate_rejected is None
    assert evaluated.score > 0.0


def test_unknown_location_based_in_munich_office_passes():
    """A row with no location whose text says 'based in our Munich office' passes."""
    matcher = ProfileMatcher(ProfileConfig())
    job = JobPosting(
        id="greenhouse:acmesaas:2002",
        source="greenhouse",
        ats="greenhouse",
        company="Acme SaaS",
        title="Customer Success Manager",
        location=None,
        description=(
            "We are hiring a Customer Success Manager to help onboard enterprise clients "
            "and drive SaaS adoption. This role is based in our Munich office with a hybrid setup."
        ),
        url="https://boards.greenhouse.io/acmesaas/jobs/2002",
        apply_url="https://boards.greenhouse.io/acmesaas/jobs/2002",
        apply_kind="auto",
    )
    assert job.location is None
    assert job.location_source == "unknown"

    evaluated = matcher.evaluate(job)
    assert evaluated.gate_rejected is None
    assert evaluated.score > 0.0

    # Verify a row with no location and no mention of Munich/Germany-remote is rejected
    job_no_munich = JobPosting(
        id="greenhouse:acmesaas:2003",
        source="greenhouse",
        ats="greenhouse",
        company="Acme SaaS",
        title="Customer Success Manager",
        location=None,
        description="We are hiring a Customer Success Manager to help onboard enterprise clients.",
        url="https://boards.greenhouse.io/acmesaas/jobs/2003",
        apply_url="https://boards.greenhouse.io/acmesaas/jobs/2003",
        apply_kind="auto",
    )
    eval_no_munich = matcher.evaluate(job_no_munich)
    assert eval_no_munich.gate_rejected == "location"

