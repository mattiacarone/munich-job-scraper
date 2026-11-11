"""
Apply Kind & Host Classifier for munichScraper (Brief 5).
Classifies how each job posting can be applied to:
- 'auto' : direct ATS forms without login or captcha (Greenhouse, Lever, Personio, Workable, SmartRecruiters, Teamtailor, Join).
- 'captcha' : ATS with frequent captchas (Recruitee, BambooHR, Ashby, or pages with hcaptcha/h-captcha in HTML).
- 'account' : enterprise ATS requiring account creation / login (Workday, SuccessFactors/sapsf, careersportal, amazon.jobs, jobs.sap.com, Oracle HCM, Taleo, iCIMS, Phenom, Eightfold).
- 'easy_apply' : LinkedIn jobs with no off-site link.
- 'unknown' : otherwise.
"""

import re
import urllib.parse
from typing import Optional, Tuple


# Regexes for host identification
ACCOUNT_HOST_PATTERNS = [
    re.compile(r"(?:^|\.)(?:wd\d+\.)?myworkdayjobs\.com$", re.I),
    re.compile(r"(?:^|\.)(?:successfactors\.(?:com|eu)|sapsf\.com)$", re.I),
    re.compile(r"^jobs\.sap\.com$", re.I),
    re.compile(r"(?:^|\.)careersportal\.[a-z0-9_-]+$", re.I),
    re.compile(r"^amazon\.jobs$", re.I),
    re.compile(r"(?:^|\.)oraclecloud\.com$", re.I),
    re.compile(r"(?:^|\.)taleo\.net$", re.I),
    re.compile(r"(?:^|\.)icims\.com$", re.I),
    re.compile(r"(?:^|\.)phenom(?:people)?\.com$", re.I),
    re.compile(r"(?:^|\.)eightfold\.ai$", re.I),
    re.compile(r"(?:^|\.)stepstone\.(?:de|com)$", re.I),
    re.compile(r"(?:^|\.)xing\.com$", re.I),
]

AUTO_HOST_PATTERNS = [
    re.compile(r"(?:^|\.)(?:job-boards(?:\.eu)?|boards)?\.greenhouse\.io$", re.I),
    re.compile(r"(?:^|\.)(?:jobs\.)?lever\.co$", re.I),
    re.compile(r"(?:^|\.)(?:jobs\.)?personio\.(?:de|com)$", re.I),
    re.compile(r"(?:^|\.)(?:apply\.)?workable\.com$", re.I),
    re.compile(r"(?:^|\.)(?:jobs\.)?smartrecruiters\.com$", re.I),
    re.compile(r"(?:^|\.)teamtailor\.com$", re.I),
    re.compile(r"(?:^|\.)join\.com$", re.I),
    re.compile(r"(?:^|\.)softgarden\.(?:io|de)$", re.I),
    re.compile(r"(?:^|\.)breezy\.hr$", re.I),
    re.compile(r"(?:^|\.)pinpointhq\.com$", re.I),
    re.compile(r"(?:^|\.)factorialhr\.com$", re.I),
]

CAPTCHA_HOST_PATTERNS = [
    re.compile(r"(?:^|\.)recruitee\.com$", re.I),
    re.compile(r"(?:^|\.)bamboohr\.com$", re.I),
    re.compile(r"(?:^|\.)(?:jobs\.)?ashbyhq\.com$", re.I),
]


def extract_host_from_url(url: Optional[str]) -> Optional[str]:
    """Extract lowercase netloc/hostname from URL string."""
    if not url:
        return None
    try:
        parsed = urllib.parse.urlparse(url.strip())
        host = (parsed.netloc or "").strip().lower()
        # Strip port if present
        if ":" in host:
            host = host.split(":")[0]
        return host or None
    except Exception:
        return None


def strip_utm_params(url: str) -> str:
    """Strip utm_*, ref, and tracking parameters from a URL."""
    if not url:
        return ""
    try:
        parsed = urllib.parse.urlparse(url.strip())
        qs = urllib.parse.parse_qs(parsed.query, keep_blank_values=True)
        # Keep non-tracking params
        clean_qs = {
            k: v for k, v in qs.items()
            if not (k.lower().startswith("utm_") or k.lower() in {"ref", "referrer", "source"})
        }
        clean_query = urllib.parse.urlencode(clean_qs, doseq=True)
        return urllib.parse.urlunparse((
            parsed.scheme,
            parsed.netloc,
            parsed.path,
            parsed.params,
            clean_query,
            parsed.fragment,
        ))
    except Exception:
        return url


def classify_apply_kind(
    apply_url: Optional[str] = None,
    url: Optional[str] = None,
    ats: Optional[str] = None,
    source: Optional[str] = None,
    html_text: Optional[str] = None,
) -> Tuple[str, Optional[str]]:
    """
    Classify posting into ('auto', 'captcha', 'account', 'easy_apply', 'unknown') and determine apply_host.
    """
    # 1. Determine target URL & host
    effective_url = apply_url or url
    host = extract_host_from_url(effective_url)

    # 2. Check if HTML contains explicit captcha markers
    if html_text:
        if re.search(r"\b(?:hcaptcha|h-captcha)\b", html_text, re.I):
            return "captcha", host

    # Check calibrated route overrides from real application runs (Brief 10 Section 5)
    try:
        from route_calibrator import load_route_calibrations
        calibs = load_route_calibrations()
        effective_route = (ats or "").lower().strip()
        if not effective_route and host:
            if "ashbyhq.com" in host:
                effective_route = "ashby"
            elif "greenhouse.io" in host:
                effective_route = "greenhouse"
            elif "lever.co" in host:
                effective_route = "lever"
            elif "personio" in host:
                effective_route = "personio"
        if effective_route in calibs and calibs[effective_route] == "auto":
            return "auto", host
    except Exception:
        pass

    # 3. Check account hosts (highest gate priority)
    if host:
        for pat in ACCOUNT_HOST_PATTERNS:
            if pat.search(host):
                return "account", host

    # Also check ATS name if given as workday/successfactors/taleo/etc.
    ats_norm = (ats or "").lower().strip()
    if ats_norm in {"workday", "successfactors", "sapsf", "taleo", "icims", "phenom", "eightfold", "oracle_hcm"}:
        return "account", host or ats_norm

    # 4. Check captcha hosts
    if host:
        for pat in CAPTCHA_HOST_PATTERNS:
            if pat.search(host):
                return "captcha", host

    if ats_norm in {"recruitee", "ashby", "bamboohr"}:
        return "captcha", host or ats_norm

    # 5. Check JOIN (Brief 7 - needs signed-in session)
    src_norm = (source or "").lower().strip()
    if ats_norm == "join" or src_norm == "join" or (host and "join.com" in host):
        if not apply_url or (host and "join.com" in host):
            return "join-account", host or "join.com"

    # 6. Check auto hosts
    if host:
        for pat in AUTO_HOST_PATTERNS:
            if pat.search(host):
                return "auto", host

    if ats_norm in {
        "greenhouse", "lever", "personio", "workable", "smartrecruiters",
        "teamtailor", "softgarden", "breezy", "pinpoint", "factorial",
    }:
        return "auto", host or ats_norm

    # 6. Check LinkedIn easy apply
    src_norm = (source or "").lower().strip()
    if src_norm == "linkedin" or (host and "linkedin.com" in host):
        if not apply_url or (host and "linkedin.com" in host):
            return "easy_apply", "linkedin.com"

    return "unknown", host


AGGREGATOR_HOSTS = {
    "arbeitnow.com", "arbeitnow.fr", "arbeitnow.nl", "arbeitnow.es", "arbeitnow.it",
    "arbeitnow.co.uk", "jobicy.com", "stepstone.de", "stepstone.com", "xing.com",
    "germantechjobs.de", "englishjobsingermany.com", "wellfound.com", "remotive.com",
    "himalayas.app", "weworkremotely.com", "workingnomads.com", "remoteok.com",
    "euremotejobs.com", "getro.com", "consider.com",
}


def classify_apply_route(
    apply_url: Optional[str] = None,
    url: Optional[str] = None,
    ats: Optional[str] = None,
    source: Optional[str] = None,
    html_text: Optional[str] = None,
) -> Tuple[str, int]:
    """
    Classify posting into Brief 9 Section 3 apply_route and needs_account (0 or 1).
    Allowed routes:
      greenhouse, ashby, lever, personio, smartrecruiters, teamtailor, recruitee,
      workable, linkedin_easy, amazon_jobs, workday, successfactors, phenom,
      join, taleo, icims, email, company_site, unknown.
    Rows routed to workday, successfactors, phenom, join, taleo or icims get needs_account = 1.
    """
    effective_url = (apply_url or url or "").strip()
    if effective_url.lower().startswith("mailto:"):
        return "email", 0
    if html_text and not apply_url and re.search(r"mailto:[a-z0-9._%+-]+@[a-z0-9.-]+", html_text, re.I):
        return "email", 0

    host = extract_host_from_url(effective_url) or ""
    ats_norm = (ats or "").lower().strip()
    src_norm = (source or "").lower().strip()

    # Direct ATS routes (needs_account = 0)
    if ats_norm == "greenhouse" or "greenhouse.io" in host:
        return "greenhouse", 0
    if ats_norm == "ashby" or "ashbyhq.com" in host:
        return "ashby", 0
    if ats_norm == "lever" or "lever.co" in host:
        return "lever", 0
    if ats_norm == "personio" or "personio." in host:
        return "personio", 0
    if ats_norm == "smartrecruiters" or "smartrecruiters.com" in host:
        return "smartrecruiters", 0
    if ats_norm == "teamtailor" or "teamtailor.com" in host:
        return "teamtailor", 0
    if ats_norm == "recruitee" or "recruitee.com" in host:
        return "recruitee", 0
    if ats_norm == "workable" or "workable.com" in host:
        return "workable", 0

    # Account-required routes (needs_account = 1, parked for Mattia, not gated)
    if ats_norm == "workday" or "myworkdayjobs.com" in host or "workday.com" in host:
        return "workday", 1
    if ats_norm in {"successfactors", "sapsf"} or "successfactors." in host or "sapsf.com" in host or host == "jobs.sap.com":
        return "successfactors", 1
    if ats_norm == "phenom" or "phenompeople.com" in host or "phenom.com" in host:
        return "phenom", 1
    if ats_norm == "join" or src_norm == "join" or "join.com" in host:
        return "join", 1
    if ats_norm == "taleo" or "taleo.net" in host:
        return "taleo", 1
    if ats_norm == "icims" or "icims.com" in host:
        return "icims", 1

    # Amazon jobs
    if host == "amazon.jobs" or host.endswith(".amazon.jobs"):
        return "amazon_jobs", 0

    # LinkedIn easy apply
    if src_norm == "linkedin" or "linkedin.com" in host:
        if not apply_url or "linkedin.com" in (extract_host_from_url(apply_url) or ""):
            return "linkedin_easy", 0

    # Other ATS / company site vs aggregator
    if ats_norm in {"softgarden", "bamboohr", "breezy", "pinpoint", "factorial", "comeet"}:
        return "company_site", 0

    if host:
        is_agg = any(host == agg or host.endswith("." + agg) for agg in AGGREGATOR_HOSTS)
        if not is_agg:
            return "company_site", 0

    return "unknown", 0


def detect_ats_from_url(url: str) -> Tuple[Optional[str], Optional[str]]:
    """Detect known ATS provider and company slug from an apply URL."""
    if not url:
        return None, None
    u = url.lower()

    m = re.search(r"greenhouse\.io/(?:v1/boards/)?([a-z0-9_-]+)", u)
    if m and m.group(1) not in {"embed", "jobs"}:
        return "greenhouse", m.group(1)

    m = re.search(r"ashbyhq\.com/([a-z0-9_-]+)", u)
    if m:
        return "ashby", m.group(1)

    m = re.search(r"lever\.co/([a-z0-9_-]+)", u)
    if m:
        return "lever", m.group(1)

    m = re.search(r"([a-z0-9_-]+)(?:\.jobs)?\.personio\.(?:de|com)", u)
    if m:
        return "personio", m.group(1)

    m = re.search(r"workable\.com/(?:j/|api/v1/widget/accounts/)?([a-z0-9_-]+)", u)
    if m:
        return "workable", m.group(1)

    m = re.search(r"([a-z0-9_-]+)\.recruitee\.com", u)
    if m:
        return "recruitee", m.group(1)

    m = re.search(r"smartrecruiters\.com/(?:companies/)?([a-z0-9_-]+)", u)
    if m:
        return "smartrecruiters", m.group(1)

    m = re.search(r"([a-z0-9_-]+)\.teamtailor\.com", u)
    if m:
        return "teamtailor", m.group(1)

    return None, None


def record_discovered_board(ats: str, slug: str) -> None:
    """Save newly discovered ATS board to data/discovered_boards.json."""
    from prober import record_discovered_slug
    record_discovered_slug(ats, slug)

