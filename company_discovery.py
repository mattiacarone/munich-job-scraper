"""
Daily company discovery (Brief 12 section 2).

For every name in the seed lists, try slug variants (name, name-with-dashes, name without legal
suffix) on Greenhouse, Ashby, Lever, Personio (xml), Workable (widget API), Recruitee, Teamtailor and
SmartRecruiters. A 404 means "wrong slug or wrong ATS", never "no jobs". Hits go to the `companies`
table (+ data/discovered_boards.json so the ATS scrapers poll them); misses go to `probed_companies`
with status 'miss' and are retried weekly instead of every cycle.

Only public JSON/XML/HTML endpoints. Seed pages are fetched only when robots.txt allows it.
"""

from __future__ import annotations

import json
import logging
import re
import threading
import time
import urllib.robotparser
from concurrent.futures import ThreadPoolExecutor, as_completed
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Callable, Iterable, Optional
from urllib.parse import urlparse

import httpx

from config import LIVE_MEMORY_DIR
from prober import generate_slug_candidates, rate_limited_get, record_discovered_slug

logger = logging.getLogger(__name__)

MISS_RETRY_DAYS = 7
DISCOVERY_INTERVAL_HOURS = 24
USER_AGENT = "Mozilla/5.0 (munichScraper company discovery; public endpoints only)"
LINKEDIN_EMPLOYERS_PATH = LIVE_MEMORY_DIR / "linkedin_employers.json"
MAX_LINKEDIN_EMPLOYERS_PER_DAY = 300

AGENCY_SKIP_SUBSTRINGS = ("recruit", "staffing", "personal", "talent", "jobs", "karriere", "career")
EMPLOYER_STRIP_SUFFIX_RE = re.compile(
    r"\b(?:gmbh|ag|se|inc|ltd|llc|bv|kg|group|holding|deutschland|germany)\b",
    re.IGNORECASE,
)


def normalize_employer_name(name: str) -> str:
    """
    Brief 14 A4: lowercase; strip GmbH, AG, SE, Inc, Ltd, LLC, BV, KG, Group, Holding,
    Deutschland, Germany and punctuation.
    """
    if not name:
        return ""
    s = name.lower()
    s = EMPLOYER_STRIP_SUFFIX_RE.sub(" ", s)
    s = re.sub(r"[^\w\s-]", " ", s)
    s = re.sub(r"[\s_-]+", " ", s).strip()
    return s


def is_agency_employer_name(name: str) -> bool:
    """Brief 14 A4: Skip any name containing recruit, staffing, personal, talent, jobs, karriere, career."""
    if not name:
        return True
    low = name.lower()
    return any(tok in low for tok in AGENCY_SKIP_SUBSTRINGS)


def load_linkedin_employer_seeds(
    db,
    path: Optional[Path] = None,
    max_employers: int = MAX_LINKEDIN_EMPLOYERS_PER_DAY,
) -> list[str]:
    """
    Brief 14 A4: Read linkedin_employers.json (read-only) as the first seed source.
    Normalize names, skip agencies and names already in probed_companies or companies,
    and return up to max_employers (default 300) normalized employer names.
    """
    emp_path = Path(path) if path else LINKEDIN_EMPLOYERS_PATH
    if not emp_path.exists():
        return []
    try:
        with open(emp_path, "r", encoding="utf-8") as f:
            data = json.load(f)
    except Exception as exc:
        logger.warning(f"[Discovery] Failed to read {emp_path}: {exc}")
        return []

    raw_list = data.get("employers", []) if isinstance(data, dict) else (data if isinstance(data, list) else [])

    known_norm: set[str] = set()
    known_compact: set[str] = set()
    if db is not None:
        with db._get_connection() as conn:
            for (cname,) in conn.execute("SELECT company_name FROM probed_companies WHERE company_name IS NOT NULL"):
                n = normalize_employer_name(cname)
                if n:
                    known_norm.add(n)
                    known_compact.add(re.sub(r"[^a-z0-9]", "", n))
                known_norm.add(cname.strip().lower())
            for dname, slug in conn.execute("SELECT display_name, slug FROM companies"):
                for val in (dname, slug):
                    if val:
                        n = normalize_employer_name(val)
                        if n:
                            known_norm.add(n)
                            known_compact.add(re.sub(r"[^a-z0-9]", "", n))
                        known_norm.add(val.strip().lower())

    seeds: list[str] = []
    for entry in raw_list:
        raw_name = entry.get("name", "") if isinstance(entry, dict) else str(entry or "")
        raw_name = raw_name.strip()
        if not raw_name or is_agency_employer_name(raw_name):
            continue
        norm = normalize_employer_name(raw_name)
        if not norm or len(norm) < 2 or is_agency_employer_name(norm):
            continue
        compact = re.sub(r"[^a-z0-9]", "", norm)
        if not compact or norm in known_norm or raw_name.lower() in known_norm or compact in known_compact:
            continue
        known_norm.add(norm)
        known_compact.add(compact)
        seeds.append(norm)
        if len(seeds) >= max_employers:
            break

    return seeds

# Curated Munich company slugs across major ATS vendors (Brief 12 original 26 slugs)
KNOWN_MUNICH_SLUGS: dict[str, list[str]] = {
    "ashby": [
        "clera", "rasa", "finto", "growthprotocol", "jua", "peec", "sensorfact", "statista", "payrails", "alephalpha",
    ],
    "greenhouse": ["superchat", "konux"],
    "lever": ["iqm"],
    "personio": [
        "appliedai", "eqs-group", "juna", "ki-group", "miles-mobility", "userlane", "climedo", "foodforecast", "tanso", "unternehmertum",
    ],
    "recruitee": ["bluebird", "kigroup"],
    "workable": ["agile-robots"],
}

# Expanded Munich scaleups across tech, space, climate, and enterprise robotics
EXPANDED_MUNICH_SLUGS: dict[str, list[str]] = {
    "ashby": [
        "robco", "the-exploration-company", "proxima-fusion", "blp-digital", "tacto", "vaeridion",
        "avelios-medical", "voize", "dryft", "reonic", "luminovo", "camunda", "kertos"
    ],
    "greenhouse": [
        "isaraerospace", "navvis", "roboyo", "airup", "helsing", "marvelfusion", "flix",
        "parloa", "blacklane", "enmacc", "numa", "celigo"
    ],
    "lever": ["finn"],
    "personio": [
        "ottonova", "twaice", "proglove", "ommax", "blickfeld", "predium", "reverion"
    ],
    "recruitee": ["formo"],
    "workable": ["scalable"],
    "smartrecruiters": ["brainlab"],
    "teamtailor": ["vialytics", "corintis"],
}

SEED_PAGES = {
    "unternehmertum": "https://www.unternehmertum.de/startups",
    "clera_munich": "https://www.getclera.com/jobs/munich",
}
# Pages that robots.txt or the live check rules out (documented in REPORT_BRIEF_12.md):
#   ycombinator.com/companies?*  -> Disallow in robots.txt; api.ycombinator.com -> "Disallow: /"
#   munich-startup.de            -> /startups-and-ecosystem lists events, no company directory in server HTML
#   tum-venture-labs.de/portfolio -> 404


# ---------------------------------------------------------------------------
# ATS probes: each returns total job count (0 = board exists but empty or not found)
# ---------------------------------------------------------------------------
def _get_json(client: httpx.Client, url: str):
    r = rate_limited_get(client, url)
    if r.status_code != 200:
        return None
    try:
        return r.json()
    except Exception:
        return None


def probe_greenhouse(client, slug) -> int:
    d = _get_json(client, f"https://boards-api.greenhouse.io/v1/boards/{slug}/jobs")
    return len(d.get("jobs", [])) if isinstance(d, dict) else 0


def probe_ashby(client, slug) -> int:
    d = _get_json(client, f"https://api.ashbyhq.com/posting-api/job-board/{slug}")
    return len(d.get("jobs", [])) if isinstance(d, dict) else 0


def probe_lever(client, slug) -> int:
    d = _get_json(client, f"https://api.lever.co/v0/postings/{slug}?mode=json")
    return len(d) if isinstance(d, list) else 0


def probe_personio(client, slug) -> int:
    for dom in ("com", "de"):
        try:
            r = rate_limited_get(client, f"https://{slug}.jobs.personio.{dom}/xml")
        except Exception:
            continue
        if r.status_code == 200 and "<position" in r.text:
            return len(re.findall(r"<position>", r.text)) or 1
    return 0


def probe_workable(client, slug) -> int:
    d = _get_json(client, f"https://apply.workable.com/api/v1/widget/accounts/{slug}")
    return len(d.get("jobs", [])) if isinstance(d, dict) else 0


def probe_recruitee(client, slug) -> int:
    d = _get_json(client, f"https://{slug}.recruitee.com/api/offers/")
    return len(d.get("offers", [])) if isinstance(d, dict) else 0


def probe_teamtailor(client, slug) -> int:
    try:
        r = rate_limited_get(client, f"https://{slug}.teamtailor.com/jobs.rss")
    except Exception:
        return 0
    if r.status_code == 200 and "<item" in r.text:
        return len(re.findall(r"<item>", r.text))
    return 0


def probe_smartrecruiters(client, slug) -> int:
    d = _get_json(client, f"https://api.smartrecruiters.com/v1/companies/{slug}/postings?limit=1")
    return int(d.get("totalFound", 0)) if isinstance(d, dict) else 0


ATS_PROBES: dict[str, Callable] = {
    "greenhouse": probe_greenhouse,
    "ashby": probe_ashby,
    "lever": probe_lever,
    "personio": probe_personio,
    "workable": probe_workable,
    "recruitee": probe_recruitee,
    "teamtailor": probe_teamtailor,
    "smartrecruiters": probe_smartrecruiters,
}


def slug_variants(name: str) -> list[str]:
    """name, name-with-dashes, name without legal suffix (+ compact form)."""
    return generate_slug_candidates(name)


def probe_name(client: httpx.Client, name: str, probes: Optional[dict] = None) -> list[tuple[str, str, int]]:
    """All (ats, slug, total_jobs) hits for a company name across the slug variants."""
    probes = probes or ATS_PROBES
    hits: list[tuple[str, str, int]] = []
    for slug in slug_variants(name):
        if len(slug) < 2:
            continue
        for ats, fn in probes.items():
            try:
                n = fn(client, slug)
            except Exception as exc:  # noqa: BLE001
                logger.debug(f"[Discovery] {ats}:{slug} probe error: {exc}")
                continue
            if n > 0:
                hits.append((ats, slug, n))
        if hits:
            break  # first variant that resolves is the right slug
    return hits


# ---------------------------------------------------------------------------
# Seeds
# ---------------------------------------------------------------------------
def robots_allows(client: httpx.Client, url: str) -> bool:
    p = urlparse(url)
    rp = urllib.robotparser.RobotFileParser()
    try:
        r = client.get(f"{p.scheme}://{p.netloc}/robots.txt", timeout=10.0)
        if r.status_code != 200:
            return True  # no robots.txt = no restriction
        rp.parse(r.text.splitlines())
    except Exception:
        return False
    return rp.can_fetch("*", url)


def parse_unternehmertum_names(html_text: str) -> list[str]:
    names = re.findall(r"<h[23][^>]*>\s*([^<]{2,60}?)\s*</h[23]>", html_text)
    return [n.replace("&amp;", "&").strip() for n in names]


def parse_clera_company_slugs(html_text: str) -> list[str]:
    """Client company slugs from getclera.com/jobs/<company>/<role> links (skips city pages and Clera itself)."""
    skip = {"munich", "berlin", "london", "new-york", "san-francisco", "clera", "remote"}
    out: list[str] = []
    for slug in re.findall(r'href="/jobs/([a-z0-9][a-z0-9-]*)/[^"#?]+"', html_text):
        if slug not in skip and slug not in out:
            out.append(slug)
    return out


def fetch_seed_names(client: httpx.Client) -> tuple[list[str], dict[str, str]]:
    """Names from the public seed pages. Returns (names, {source: status})."""
    names: list[str] = []
    status: dict[str, str] = {}
    for key, url in SEED_PAGES.items():
        try:
            if not robots_allows(client, url):
                status[key] = "blocked by robots.txt"
                continue
            r = client.get(url, timeout=20.0, follow_redirects=True)
            if r.status_code != 200:
                status[key] = f"HTTP {r.status_code}"
                continue
            if key == "unternehmertum":
                found = parse_unternehmertum_names(r.text)
            else:
                found = [s.replace("-", " ") for s in parse_clera_company_slugs(r.text)]
            names.extend(found)
            status[key] = f"{len(found)} names"
        except Exception as exc:  # noqa: BLE001
            status[key] = f"error {type(exc).__name__}"
    return names, status


def all_seed_names(
    db,
    client: Optional[httpx.Client] = None,
    linkedin_employers_path: Optional[Path] = None,
    max_linkedin_employers: int = MAX_LINKEDIN_EMPLOYERS_PER_DAY,
) -> tuple[list[str], dict[str, str]]:
    from prober import MUNICH_SEED_COMPANIES

    # Brief 14 A4: Read linkedin_employers.json as the first seed source
    employer_seeds = load_linkedin_employer_seeds(
        db, path=linkedin_employers_path, max_employers=max_linkedin_employers
    )
    names: list[str] = list(employer_seeds) + list(MUNICH_SEED_COMPANIES)
    status: dict[str, str] = {"linkedin_employers": f"{len(employer_seeds)} names"}
    own = client is None
    client = client or httpx.Client(headers={"User-Agent": USER_AGENT})
    try:
        page_names, page_status = fetch_seed_names(client)
        status.update(page_status)
        names += page_names
    finally:
        if own:
            client.close()
    with db._get_connection() as conn:  # investor portfolios / partner companies + recent job-board employers
        for row in conn.execute("SELECT display_name FROM companies WHERE source LIKE 'partner_%' OR source LIKE 'vc%'"):
            if row[0]:
                names.append(row[0])
        for row in conn.execute("SELECT DISTINCT company FROM jobs WHERE company IS NOT NULL ORDER BY created_at DESC LIMIT 200"):
            if row[0]:
                names.append(row[0])
    seen, out = set(), []
    for n in names:
        k = re.sub(r"[^a-z0-9]", "", n.lower())
        if k and k not in seen:
            seen.add(k)
            out.append(n)
    return out, status


# ---------------------------------------------------------------------------
# Persistence and orchestration
# ---------------------------------------------------------------------------
def import_known_slugs(db, slugs: Optional[dict] = None) -> int:
    """Import the 26 slugs from the brief into companies + discovered_boards.json (idempotent)."""
    slugs = slugs or KNOWN_MUNICH_SLUGS
    now = datetime.now(timezone.utc).isoformat()
    n = 0
    with db._get_connection() as conn:
        for ats, lst in slugs.items():
            for slug in lst:
                conn.execute(
                    """INSERT INTO companies (source, slug, display_name, updated_at) VALUES (?, ?, ?, ?)
                       ON CONFLICT(source, slug) DO UPDATE SET updated_at = excluded.updated_at""",
                    (ats, slug, slug.replace("-", " ").title(), now),
                )
                n += 1
    for ats, lst in slugs.items():
        for slug in lst:
            record_discovered_slug(ats, slug)
    return n


def import_expanded_slugs(db, slugs: Optional[dict] = None) -> int:
    """Import expanded Munich scaleups into companies + discovered_boards.json (idempotent)."""
    return import_known_slugs(db, slugs=slugs or EXPANDED_MUNICH_SLUGS)


def _due_names(db, names: Iterable[str], force: bool) -> list[str]:
    if force:
        return list(names)
    cutoff = (datetime.now(timezone.utc) - timedelta(days=MISS_RETRY_DAYS)).isoformat()
    with db._get_connection() as conn:
        recent = {r[0] for r in conn.execute("SELECT company_name FROM probed_companies WHERE last_probed_at > ?", (cutoff,))}
    return [n for n in names if n not in recent]


def discover_names(
    db,
    names: Iterable[str],
    client: Optional[httpx.Client] = None,
    force: bool = False,
    max_names: int = 600,
    concurrency: int = 8,
    on_progress: Optional[Callable[[], None]] = None,
    probes: Optional[dict] = None,
    employer_seed_set: Optional[set[str]] = None,
) -> dict:
    """Probe names. Returns {'probed', 'hits', 'misses', 'new_boards': [(ats, slug, n)], 'employer_names_probed', 'employer_boards_found'}."""
    todo = _due_names(db, names, force)[:max_names]
    result = {
        "probed": 0,
        "hits": 0,
        "misses": 0,
        "new_boards": [],
        "employer_names_probed": 0,
        "employer_boards_found": 0,
    }
    if not todo:
        return result
    emp_set = employer_seed_set or set()
    own = client is None
    client = client or httpx.Client(headers={"User-Agent": USER_AGENT}, timeout=8.0, follow_redirects=True)
    lock = threading.Lock()
    now = datetime.now(timezone.utc).isoformat()
    try:
        with ThreadPoolExecutor(max_workers=concurrency) as ex:
            futs = {ex.submit(probe_name, client, n, probes): n for n in todo}
            for f in as_completed(futs):
                name = futs[f]
                try:
                    hits = f.result()
                except Exception as exc:  # noqa: BLE001
                    logger.debug(f"[Discovery] {name}: {exc}")
                    continue
                is_emp = name in emp_set
                with lock:
                    result["probed"] += 1
                    if is_emp:
                        result["employer_names_probed"] += 1
                    if hits:
                        result["hits"] += 1
                        ats, slug, n = hits[0]
                        db.record_probed_company(name, ats, slug, n, "active")
                        with db._get_connection() as conn:
                            for h_ats, h_slug, _n in hits:
                                conn.execute(
                                    """INSERT INTO companies (source, slug, display_name, updated_at) VALUES (?, ?, ?, ?)
                                       ON CONFLICT(source, slug) DO UPDATE SET display_name = excluded.display_name, updated_at = excluded.updated_at""",
                                    (h_ats, h_slug, name, now),
                                )
                                record_discovered_slug(h_ats, h_slug)
                                result["new_boards"].append((h_ats, h_slug, _n))
                                if is_emp:
                                    result["employer_boards_found"] += 1
                    else:
                        result["misses"] += 1
                        db.record_probed_company(name, None, None, 0, "miss")
                if on_progress:
                    on_progress()
    finally:
        if own:
            client.close()
    return result


def discovery_is_due(db, now: Optional[datetime] = None) -> bool:
    last = db.get_metadata("last_company_discovery_at")
    if not last:
        return True
    try:
        dt = datetime.fromisoformat(last)
        dt = dt if dt.tzinfo else dt.replace(tzinfo=timezone.utc)
    except Exception:
        return True
    return ((now or datetime.now(timezone.utc)) - dt).total_seconds() >= DISCOVERY_INTERVAL_HOURS * 3600


def run_daily_discovery(
    db,
    force: bool = False,
    on_progress: Optional[Callable[[], None]] = None,
    max_names: int = 600,
    linkedin_employers_path: Optional[Path] = None,
) -> dict:
    """Daily step: import known slugs, collect seed names, probe the ones that are due."""
    if not force and not discovery_is_due(db):
        return {"skipped": True}
    t0 = time.time()
    imported = import_known_slugs(db) + import_expanded_slugs(db)
    employer_seeds = load_linkedin_employer_seeds(
        db, path=linkedin_employers_path, max_employers=MAX_LINKEDIN_EMPLOYERS_PER_DAY
    )
    names, seed_status = all_seed_names(db, linkedin_employers_path=linkedin_employers_path)
    res = discover_names(
        db,
        names,
        force=False,
        max_names=max_names,
        on_progress=on_progress,
        employer_seed_set=set(employer_seeds),
    )
    db.set_metadata("last_company_discovery_at", datetime.now(timezone.utc).isoformat())
    res.update({"imported_known": imported, "seed_names": len(names), "seed_status": seed_status, "seconds": round(time.time() - t0, 1)})
    logger.info(
        f"[Discovery] {res['probed']} names probed, {res['hits']} hits, {res['misses']} misses "
        f"(employer_probed={res['employer_names_probed']}, employer_boards_found={res['employer_boards_found']}, "
        f"retry in {MISS_RETRY_DAYS}d), seeds={seed_status}"
    )
    return res
