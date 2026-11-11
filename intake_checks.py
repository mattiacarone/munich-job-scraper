"""
Intake checks (Brief 12 sections 5, 6, 7).

Pure, side-effect-free helpers used by the matcher and the pipeline:
  - employer-link rules for aggregator sources (no apply link on the board's own domain)
  - extra hard blocks on German / seniority / contract wording (English and German)
  - Clera role-family + location filter
  - description fetching at intake (public pages only)
  - normalized company+title+location dedupe key and per-company row caps
"""

from __future__ import annotations

import html
import logging
import re
from typing import Iterable, Optional, Tuple
from urllib.parse import urlparse

logger = logging.getLogger(__name__)

MIN_DESCRIPTION_CHARS = 300

# ---------------------------------------------------------------------------
# Section 5: aggregators and employer links
# ---------------------------------------------------------------------------
AGGREGATOR_SOURCES = {
    "weworkremotely", "remoteok", "arbeitsagentur", "englishjobsde", "arbeitnow",
    "germantechjobs", "jobicy", "himalayas", "workingnomads", "remotive",
}

# Hosts that are job boards / feed exporters themselves (never an employer apply page).
AGGREGATOR_DOMAINS = (
    "weworkremotely.com", "remoteok.com", "remoteok.io", "arbeitsagentur.de",
    "englishjobs.de", "englishjobsingermany.com", "arbeitnow.com", "germantechjobs.de", "jobicy.com",
    "himalayas.app", "workingnomads.com", "remotive.com", "remotive.io",
    "wellfound.com", "angel.co", "munich-startup.de",
    # job boards that the Bundesagentur / XING feeds forward to
    "jobexport.de", "kalaydo.de", "empfehlungsbund.de", "yourfirm.de", "stellenanzeigen.de",
    "jobware.de", "stepstone.de", "xing.com", "linkedin.com", "indeed.com",
    "heyjobs.co", "rocken.jobs", "jobs.arbeitsagentur.de", "europa.eu",
    # 7 Nov 2026: never an employer page either. After Firecrawl was removed (brief 14) the href fallback in find_employer_link
    # took the boards' own badges: jobicy -> apps.apple.com (16 rows), remoteok -> producthunt.com (3 rows), all sent to tailoring.
    "apps.apple.com", "itunes.apple.com", "play.google.com", "chrome.google.com", "chromewebstore.google.com", "producthunt.com",
    "facebook.com", "instagram.com", "twitter.com", "x.com", "youtube.com", "youtu.be", "tiktok.com", "reddit.com", "medium.com",
    "substack.com", "t.me", "telegram.me", "wa.me", "whatsapp.com", "discord.gg", "discord.com", "bit.ly", "tinyurl.com", "lnkd.in",
    "buff.ly", "ow.ly", "t.co", "goo.gl", "glassdoor.com", "glassdoor.de", "kununu.com", "crunchbase.com", "trustpilot.com",
)

EMPLOYER_ATS_URL_RE = re.compile(
    r"https?://(?:"
    r"(?:boards|job-boards)(?:\.eu)?\.greenhouse\.io/[^\s\"'<>)]+"
    r"|jobs\.ashbyhq\.com/[^\s\"'<>)]+"
    r"|jobs\.(?:eu\.)?lever\.co/[^\s\"'<>)]+"
    r"|[a-z0-9_-]+\.jobs\.personio\.(?:de|com)/[^\s\"'<>)]*"
    r"|apply\.workable\.com/[^\s\"'<>)]+"
    r"|[a-z0-9_-]+\.recruitee\.com/o/[^\s\"'<>)]+"
    r"|(?:jobs|careers)\.smartrecruiters\.com/[^\s\"'<>)]+"
    r"|[a-z0-9_-]+\.teamtailor\.com/jobs/[^\s\"'<>)]+"
    r"|join\.com/companies/[^\s\"'<>)]+"
    r"|[a-z0-9_-]+\.myworkdayjobs\.com/[^\s\"'<>)]+"
    r"|[a-z0-9_-]+\.bamboohr\.com/[^\s\"'<>)]+"
    r"|[a-z0-9_-]+\.breezy\.hr/p/[^\s\"'<>)]+"
    r")",
    re.I,
)

EMAIL_RE = re.compile(r"\b[A-Za-z0-9._%+-]+@[A-Za-z0-9.-]+\.[A-Za-z]{2,}\b")

# Image captcha / login walls on the BA portal and board redirects
BLOCKED_APPLY_HINTS = re.compile(
    r"(dargestellte\s+zeichen|captcha|please\s+log\s*in\s+to\s+apply|login\s+to\s+apply|anmelden\s+um\s+sich\s+zu\s+bewerben)",
    re.I,
)


def host_of(url: Optional[str]) -> str:
    if not url:
        return ""
    u = url.strip()
    if u.lower().startswith("mailto:"):
        return "mailto"
    if "://" not in u:
        u = "https://" + u
    try:
        return (urlparse(u).hostname or "").lower().lstrip(".")
    except Exception:
        return ""


def is_aggregator_host(host: str) -> bool:
    h = (host or "").lower()
    return any(h == d or h.endswith("." + d) for d in AGGREGATOR_DOMAINS)


def strip_tracking(url: str) -> str:
    """Remove utm_* parameters so the stored employer link is clean."""
    try:
        p = urlparse(url)
        if not p.query:
            return url
        kept = [q for q in p.query.split("&") if not q.lower().startswith(("utm_", "ref=", "source="))]
        return p._replace(query="&".join(kept)).geturl()
    except Exception:
        return url


def find_employer_link(text: str) -> Optional[str]:
    """First employer-ATS url or e-mail address (as mailto:) found in free text/HTML."""
    if not text:
        return None
    t = html.unescape(text)
    m = EMPLOYER_ATS_URL_RE.search(t)
    if m:
        return strip_tracking(m.group(0).rstrip(".,;"))
    for href in re.findall(r"href=[\"'](https?://[^\"']+)[\"']", t, flags=re.I):
        h = host_of(href)
        if h and not is_aggregator_host(h) and re.search(r"(apply|career|karriere|jobs?|bewerb)", href, re.I):
            return strip_tracking(href)
    em = EMAIL_RE.search(t)
    if em and not em.group(0).lower().endswith(("@example.com", "@sentry.io")):
        return "mailto:" + em.group(0)
    return None


def check_employer_link(source: str, apply_url: Optional[str], description: str = "") -> Tuple[bool, Optional[str]]:
    """
    Brief 12 section 5. For aggregator sources the posting needs the employer's own apply url
    (or an e-mail address). An apply_url on an aggregator's own domain never passes, for any source.
    Returns (ok, reason).
    """
    au = (apply_url or "").strip()
    host = host_of(au)
    src = (source or "").lower()

    if au and host and host != "mailto" and is_aggregator_host(host):
        if src in AGGREGATOR_SOURCES or src in {"stepstone", "xing", "linkedin"}:
            return False, f"apply_url is on the aggregator's own domain ({host})"
        # company-ATS rows may legitimately point at nothing aggregator-like; still never pass it
        return False, f"apply_url is on an aggregator domain ({host})"

    if src in AGGREGATOR_SOURCES:
        if not au or not host:
            return False, f"{src} posting has no employer apply link (login wall, captcha or e-mail without address)"
        if description and BLOCKED_APPLY_HINTS.search(description[:3000]) and host == "mailto":
            return False, "apply path blocked by captcha / login"
    return True, None


# ---------------------------------------------------------------------------
# Section 6: hard blocks on the full description (English + German wording)
# ---------------------------------------------------------------------------
_PLUS = re.compile(
    r"(is\s+a\s+plus|nice\s+to\s+have|a\s+plus|advantageous|beneficial|von\s+vorteil|wünschenswert|"
    r"wuenschenswert|ist\s+ein\s+plus|desirable|bonus|optional|preferred|willkommen)",
    re.I,
)
_GERMAN_REQUIRED = [
    re.compile(r"\bfluent\s+(?:in\s+)?german\b", re.I),
    re.compile(r"\bfluency\s+in\s+german\b", re.I),
    re.compile(r"\bgerman\s*\(?\s*(?:level\s*)?(?:c1|c2|native)\b", re.I),
    re.compile(r"\b(?:c1|c2)\s*(?:level\s*)?(?:in\s+)?german\b", re.I),
    re.compile(r"\bnative\s+german\b", re.I),
    re.compile(r"\bverhandlungssicher(?:e[rnms]?)?\b", re.I),
    re.compile(r"\bfließend(?:e[rnms]?)?\s+deutsch\w*\b", re.I),
    re.compile(r"\bfliessend(?:e[rnms]?)?\s+deutsch\w*\b", re.I),
    re.compile(r"\bsehr\s+gute[n]?\s+deutschkenntnisse\b", re.I),
    re.compile(r"\bdeutsch(?:kenntnisse)?\s*\(?\s*(?:c1|c2)\b", re.I),
    re.compile(r"\bdeutsch\s+(?:auf\s+)?(?:muttersprach\w+|niveau\s+c)", re.I),
]
_GERMAN_TITLE = re.compile(r"\bgerman[- ]speaking\b|\bdeutschsprachig\w*\b", re.I)

_SENIOR_YEARS = re.compile(
    r"(?<![\d\-–—])(?:(?:5|6|7|8|9|10|12|15)\s*\+?|(?:5|6|7|8|10)\s*[-–]\s*\d{1,2})\s*(?:\+\s*)?(?:years?|yrs?|jahre?n?)\b",
    re.I,
)
_LOW_RANGE_TO_5 = re.compile(
    r"\b(?:[1-4]\s*(?:[-–—]|to|bis)\s*5|up\s+to\s+5|bis\s+zu\s+5)\s*(?:years?|yrs?|jahre?n?)\b",
    re.I,
)
_EXPERIENCE_NEAR = re.compile(r"(experience|erfahrung|berufserfahrung|track\s+record|background)", re.I)
_FOUNDER_TITLE = re.compile(
    r"\b(founder|co-?founder|ex-?founder|founding\s+(?:engineer|member)|cto|chief\s+technology\s+officer|chief\s+\w+\s+officer)\b",
    re.I,
)

_CONTRACT = [
    re.compile(r"\bfixed[- ]term\b", re.I),
    re.compile(r"\bbefristet\w*\b", re.I),
    re.compile(r"\bmaternity\s+(?:leave\s+)?cover\b", re.I),
    re.compile(r"\bparental\s+leave\s+cover\b", re.I),
    re.compile(r"\belternzeitvertretung\b", re.I),
    re.compile(r"\btemporary\s+(?:contract|position|role|employment|assignment)\b", re.I),
    re.compile(r"\bcontract\s+type\s*:\s*temporary\b", re.I),
    re.compile(r"\bpart[- ]time\b", re.I),
    re.compile(r"\bteilzeit\b", re.I),
    re.compile(r"\bwerkstudent(?:in)?\b", re.I),
    re.compile(r"\bworking\s+student\b", re.I),
    re.compile(r"\bpraktikum\b", re.I),
    re.compile(r"\bpraktikant(?:in)?\b", re.I),
]
# "12/18/24 months" only counts as a contract term when the sentence talks about the contract itself
_CONTRACT_MONTHS = re.compile(r"\b(?:12|18|24)\s*[- ]?\s*(?:months?|monate?)\b", re.I)
_CONTRACT_WORD = re.compile(
    r"\b(contract|vertrag|term|laufzeit|duration|limited|temporary|position|role|employment|assignment|stelle|anstellung|dauer)\b",
    re.I,
)
_MONTHS_NOT_CONTRACT = re.compile(r"\b(?:experience|erfahrung|notice|kündigung|probation|probezeit|after|within|first|innerhalb|nach)\b", re.I)
_CONTRACT_OK_CONTEXT = re.compile(
    r"(full[- ]time\s+or\s+part[- ]time|part[- ]time\s+or\s+full[- ]time|voll-?\s*oder\s*teilzeit|"
    r"teilzeit\s+(?:oder|möglich|moeglich)|part[- ]time\s+(?:possible|available|options?|also)|"
    r"(?:also|auch)\s+(?:in\s+)?(?:teilzeit|part[- ]time)|permanent|unbefristet)",
    re.I,
)


def _clean(text: str) -> str:
    t = html.unescape(html.unescape(text or ""))
    t = re.sub(r"<\s*(?:br|p|li|tr|div|h[1-6])(?:\s+[^>]*)?>", "\n", t, flags=re.I)
    return re.sub(r"<[^>]+>", " ", t)


def _sentences(text: str) -> Iterable[str]:
    for s in re.split(r"[\.\n\r•\?!;]+", text):
        s = re.sub(r"\s+", " ", s).strip()
        if s:
            yield s


def check_hard_blocks(title: str, description: str) -> Tuple[Optional[str], Optional[str]]:
    """
    Returns (gate_code, matched_quote) or (None, None).
    gate codes: german_required, contract.
    Brief 14 A3: a years ask lowers the score, it never blocks.
    A German-is-a-plus sentence never blocks.
    """
    title = title or ""
    if _GERMAN_TITLE.search(title):
        return "german_required", title.strip()[:160]

    text = _clean(description)
    for s in _sentences(text):
        if not _PLUS.search(s):
            for pat in _GERMAN_REQUIRED:
                if pat.search(s):
                    return "german_required", s[:160]

    for pat in _CONTRACT:
        m = pat.search(title)
        if m:
            return "contract", title.strip()[:160]
    for s in _sentences(text):
        if _CONTRACT_OK_CONTEXT.search(s):
            continue
        if re.search(r"\btemporary\s+(?:accommodation|housing|relocation|files|storage|workaround)\b", s, re.I):
            continue
        for pat in _CONTRACT:
            if pat.search(s):
                return "contract", s[:160]
        if _CONTRACT_MONTHS.search(s) and _CONTRACT_WORD.search(s) and not _MONTHS_NOT_CONTRACT.search(s):
            return "contract", s[:160]
    return None, None


# ---------------------------------------------------------------------------
# Section 7: Clera filter, dedupe key, row caps, description fetch
# ---------------------------------------------------------------------------
ROLE_FAMILY_RE = re.compile(
    r"\b(integration|implementation|consult\w*|solutions?\s+(?:engineer|architect|consultant|analyst)|"
    r"forward[- ]deployed|customer\s+success|technical\s+account|support\s+engineer|"
    r"(?:technical\s+)?(?:project|program(?:me)?)\s+manager|product\s+(?:owner|manager|operations)|"
    r"revops|revenue\s+operations|crm|gtm\s+(?:systems|engineer)|go[- ]to[- ]market|"
    r"marketing\s+operations|business\s+systems|sales\s+operations|operations\s+(?:manager|analyst)|"
    r"enablement|partner\s+(?:manager|solutions)|ai\s+(?:solutions|deployment))\b",
    re.I,
)
_MUNICH_OR_EU_REMOTE = re.compile(
    r"\b(munich|münchen|muenchen|garching|unterföhring|ismaning|remote.{0,20}(?:europe|eu|emea|germany|cet)|"
    r"(?:europe|eu|emea|germany).{0,12}remote|worldwide)\b",
    re.I,
)


def is_clera(job_id: str, company: str = "") -> bool:
    return (job_id or "").lower().startswith("ashby:clera:")


def check_clera(title: str, location: Optional[str]) -> Tuple[bool, Optional[str]]:
    """Clera is a recruiting marketplace: keep role-family titles in Munich / remote EU only."""
    if not ROLE_FAMILY_RE.search(title or ""):
        return False, f"Clera posting outside the exported role families ({title})"
    if not _MUNICH_OR_EU_REMOTE.search(location or ""):
        return False, f"Clera posting is neither Munich nor remote EU ({location})"
    return True, None


def normalize_part(text: Optional[str]) -> str:
    t = (text or "").lower()
    t = re.sub(r"[\(\[\{][^\)\]\}]*[\)\]\}]", "", t)
    t = re.sub(r"\b(m/w/d|f/m/d|w/m/d|m/f/x|all\s+genders|gn|gmbh|ag|se|inc|ltd|limited)\b", "", t, flags=re.I)
    return re.sub(r"[^a-z0-9]", "", t)


def triple_key(company: str, title: str, location: Optional[str]) -> str:
    """Normalized company + title + location (brief 12 section 7)."""
    return f"{normalize_part(company)}|{normalize_part(title)}|{normalize_part(location)}"


# per-company cap on rows created per cycle for known duplicate-flooders
COMPANY_ROW_CAPS = {
    "prolificacademic": 5,
    "prolificacademicltd": 5,
    "truetalentsolutionspartners": 5,
    "truetalentsolutions": 5,
    "jobgether": 5,
}
DEFAULT_COMPANY_ROW_CAP = 400


def dedupe_and_cap(jobs: list, existing_triples: Optional[set] = None, existing_company_counts: Optional[dict] = None) -> Tuple[list, dict]:
    """
    Drop jobs whose triple (company+title+location) already appeared (in this batch or in
    existing_triples) and cap rows per company. Returns (kept, stats).
    """
    existing_triples = existing_triples or set()
    counts = dict(existing_company_counts or {})
    seen = set()
    kept = []
    stats = {"duplicate_triple": 0, "company_cap": 0}
    for j in jobs:
        key = triple_key(j.company, j.title, j.location)
        if key in seen or key in existing_triples:
            stats["duplicate_triple"] += 1
            continue
        comp = normalize_part(j.company)
        cap = COMPANY_ROW_CAPS.get(comp, DEFAULT_COMPANY_ROW_CAP)
        if counts.get(comp, 0) >= cap:
            stats["company_cap"] += 1
            continue
        counts[comp] = counts.get(comp, 0) + 1
        seen.add(key)
        kept.append(j)
    return kept, stats


def html_to_text(raw: str) -> str:
    t = re.sub(r"(?is)<(script|style|nav|footer|header)[^>]*>.*?</\1>", " ", raw or "")
    t = _clean(t)
    return re.sub(r"[ \t\r\f\v]+", " ", t).strip()


def fetch_description(client, url: str, max_chars: int = 20000) -> str:
    """Public page text for a posting (no login, no captcha handling). Empty string on any failure."""
    if not url or not url.lower().startswith("http"):
        return ""
    try:
        r = client.get(url, timeout=15.0, follow_redirects=True)
        if r.status_code == 200:
            ctype = r.headers.get("content-type", "")
            if "html" in ctype or "text" in ctype:
                if not (BLOCKED_APPLY_HINTS.search(r.text[:20000]) and len(r.text) < 6000):
                    text = html_to_text(r.text)
                    if len(text) >= MIN_DESCRIPTION_CHARS:
                        return text[:max_chars]
    except Exception as exc:  # noqa: BLE001
        logger.debug(f"[Intake] description fetch failed for {url}: {exc}")

    return ""


def _list_ats_board_postings(ats: str, slug: str, client=None, db=None) -> list[Tuple[str, str]]:
    """
    Brief 14 A5: Return [(title, apply_url), ...] for a board (ats, slug).
    Checks DB jobs first and queries the public ATS endpoint if client is provided.
    """
    postings: list[Tuple[str, str]] = []
    seen_urls: set[str] = set()

    if db is not None:
        try:
            prefix_lo = f"{ats}:{slug}:"
            prefix_hi = f"{ats}:{slug};"
            with db._get_connection() as conn:
                for row_title, row_url, row_apply in conn.execute(
                    "SELECT title, url, apply_url FROM jobs WHERE id >= ? AND id < ? AND closed_at IS NULL",
                    (prefix_lo, prefix_hi),
                ):
                    target_url = (row_apply or row_url or "").strip()
                    if row_title and target_url and not is_aggregator_host(host_of(target_url)):
                        if target_url not in seen_urls:
                            seen_urls.add(target_url)
                            postings.append((row_title, target_url))
        except Exception as exc:
            logger.debug(f"[RegistryResolver] DB lookup error for {ats}:{slug}: {exc}")

    if client is None:
        return postings

    try:
        if ats == "greenhouse":
            r = client.get(f"https://boards-api.greenhouse.io/v1/boards/{slug}/jobs", timeout=10.0)
            if r.status_code == 200:
                for item in r.json().get("jobs", []):
                    t = (item.get("title") or "").strip()
                    u = (item.get("absolute_url") or "").strip()
                    if t and u and u not in seen_urls:
                        seen_urls.add(u)
                        postings.append((t, u))
        elif ats == "ashby":
            r = client.get(f"https://api.ashbyhq.com/posting-api/job-board/{slug}", timeout=10.0)
            if r.status_code == 200:
                for item in r.json().get("jobs", []):
                    pid = str(item.get("id", "")).strip()
                    t = (item.get("title") or "").strip()
                    u = (item.get("jobUrl") or (f"https://jobs.ashbyhq.com/{slug}/{pid}" if pid else "")).strip()
                    if t and u and u not in seen_urls:
                        seen_urls.add(u)
                        postings.append((t, u))
        elif ats == "lever":
            r = client.get(f"https://api.lever.co/v0/postings/{slug}?mode=json", timeout=10.0)
            if r.status_code == 200 and isinstance(r.json(), list):
                for item in r.json():
                    pid = str(item.get("id", "")).strip()
                    t = (item.get("text") or "").strip()
                    u = (item.get("hostedUrl") or (f"https://jobs.lever.co/{slug}/{pid}" if pid else "")).strip()
                    if t and u and u not in seen_urls:
                        seen_urls.add(u)
                        postings.append((t, u))
        elif ats == "workable":
            r = client.get(f"https://apply.workable.com/api/v1/widget/accounts/{slug}", timeout=10.0)
            if r.status_code == 200:
                for item in r.json().get("jobs", []):
                    sc = str(item.get("shortcode", "")).strip()
                    t = (item.get("title") or "").strip()
                    u = (item.get("url") or (f"https://apply.workable.com/{slug}/j/{sc}/" if sc else "")).strip()
                    if t and u and u not in seen_urls:
                        seen_urls.add(u)
                        postings.append((t, u))
        elif ats == "recruitee":
            r = client.get(f"https://{slug}.recruitee.com/api/offers/", timeout=10.0)
            if r.status_code == 200:
                for item in r.json().get("offers", []):
                    pid = str(item.get("id", "")).strip()
                    t = (item.get("title") or "").strip()
                    u = (item.get("careers_url") or (f"https://{slug}.recruitee.com/o/{pid}" if pid else "")).strip()
                    if t and u and u not in seen_urls:
                        seen_urls.add(u)
                        postings.append((t, u))
        elif ats == "smartrecruiters":
            r = client.get(f"https://api.smartrecruiters.com/v1/companies/{slug}/postings?limit=100", timeout=10.0)
            if r.status_code == 200:
                for item in r.json().get("content", []):
                    pid = str(item.get("id", "")).strip()
                    t = (item.get("name") or "").strip()
                    u = f"https://jobs.smartrecruiters.com/{slug}/{pid}" if pid else ""
                    if t and u and u not in seen_urls:
                        seen_urls.add(u)
                        postings.append((t, u))
        elif ats == "personio":
            import xml.etree.ElementTree as ET
            for dom in ("de", "com"):
                r = client.get(f"https://{slug}.jobs.personio.{dom}/xml", timeout=10.0)
                if r.status_code == 200 and "<position" in r.text:
                    root = ET.fromstring(r.text)
                    for pos in root.findall(".//position"):
                        id_el = pos.find("id")
                        name_el = pos.find("name")
                        if id_el is not None and id_el.text and name_el is not None and name_el.text:
                            pid = id_el.text.strip()
                            t = name_el.text.strip()
                            u = f"https://{slug}.jobs.personio.{dom}/job/{pid}"
                            if u not in seen_urls:
                                seen_urls.add(u)
                                postings.append((t, u))
                    break
        elif ats == "teamtailor":
            import xml.etree.ElementTree as ET
            r = client.get(f"https://{slug}.teamtailor.com/jobs.rss", timeout=10.0)
            if r.status_code == 200 and "<item" in r.text:
                root = ET.fromstring(r.text)
                for item in root.findall(".//item"):
                    t_el = item.find("title")
                    l_el = item.find("link")
                    if t_el is not None and t_el.text and l_el is not None and l_el.text:
                        t = t_el.text.strip()
                        u = l_el.text.strip()
                        if u not in seen_urls:
                            seen_urls.add(u)
                            postings.append((t, u))
    except Exception as exc:
        logger.debug(f"[RegistryResolver] HTTP listing failed for {ats}:{slug}: {exc}")

    return postings


def resolve_employer_link_via_registry(
    job,
    db,
    client=None,
    board_cache: Optional[dict] = None,
    matcher=None,
) -> Optional[str]:
    """
    Brief 14 A5: Resolve missing employer apply_url via the `companies` table registry (0 Firecrawl credits).
    1. Normalize job.company and look it up in `companies`.
    2. If that company has a board in `companies`, list the board (cached per cycle) and look for a posting
       whose normalized title matches `job.title`.
    3. If found, set `job.apply_url` to that ATS posting URL (`apply_url_source = 'registry'`) and re-run
       `matcher.evaluate(job)` if matcher is provided.
    """
    if db is None or not getattr(job, "company", None) or not getattr(job, "title", None):
        return None

    norm_company = normalize_part(job.company)
    if not norm_company or len(norm_company) < 2:
        return None

    norm_title = normalize_part(job.title)
    if not norm_title:
        return None

    matching_boards: list[Tuple[str, str]] = []
    with db._get_connection() as conn:
        for src, slug, dname in conn.execute("SELECT source, slug, display_name FROM companies"):
            if not src or not slug:
                continue
            if normalize_part(dname) == norm_company or normalize_part(slug) == norm_company:
                matching_boards.append((src, slug))

    if not matching_boards:
        return None

    cache = board_cache if board_cache is not None else {}
    for ats, slug in matching_boards:
        key = (ats, slug)
        if key not in cache:
            cache[key] = _list_ats_board_postings(ats, slug, client=client, db=db)
        postings = cache[key]
        # 7 Nov 2026: an exact title match wins. A near match counts only when the titles are almost the same length (90%+)
        # and exactly one posting fits: the old "one title contains the other" rule linked "Product Manager - Security" to
        # Canonical's generic "Product Manager" posting and "Product Manager" to "Junior Product Manager".
        matched_url = None
        near = []
        for p_title, p_url in postings:
            p_norm = normalize_part(p_title)
            if not p_norm:
                continue
            if p_norm == norm_title:
                matched_url = p_url
                break
            short, long_ = sorted((p_norm, norm_title), key=len)
            if len(short) >= 10 and short in long_ and len(short) / len(long_) >= 0.9:
                near.append(p_url)
        if not matched_url and len(set(near)) == 1:
            matched_url = near[0]
        if matched_url:
            from apply_classifier import classify_apply_kind, extract_host_from_url
            job.apply_url = matched_url
            job.apply_url_source = "registry"
            job.apply_url_status = "resolved"
            kind, host = classify_apply_kind(apply_url=matched_url, url=job.url, ats=ats, source=job.source, html_text=job.description)
            job.apply_kind = kind
            job.apply_host = host or extract_host_from_url(matched_url)
            if matcher is not None:
                matcher.evaluate(job)
            return matched_url

    return None
