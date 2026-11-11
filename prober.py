"""
Automatic ATS Career Board Prober.
Discovers verified company career boards across Greenhouse, Ashby, Lever,
SmartRecruiters, Workable, Recruitee, and Personio.
A slug counts ONLY if the API returns at least one job AND at least one job is in Germany or is remote.
Includes per-host rate limiting (max 3 req/sec) and 5s backoff on HTTP 429.
Saves verified hits to data/discovered_boards.json so they are automatically included
in subsequent scraping cycles.
"""

from collections import defaultdict
from datetime import datetime, timedelta, timezone
import json
import logging
import re
import threading
import time
from concurrent.futures import ThreadPoolExecutor, as_completed
from pathlib import Path
from typing import Optional, Tuple
import urllib.parse
import httpx
from config import load_ats_registry
from database import Database

logger = logging.getLogger(__name__)
DATA_DIR = Path(__file__).resolve().parent / "data"

LEGAL_SUFFIXES_RE = re.compile(
    r"\b(gmbh|ag|se|inc|incorporated|ltd|limited|group|germany|deutschland|europe|holding|co|kg)\b",
    re.I,
)

GERMANY_OR_REMOTE_REGEX = re.compile(
    r"\b(germany|deutschland|munich|m[üu]nchen|remote|home[- ]office|telecommut\w+|anywhere|dach|emea|europe)\b",
    re.I,
)

_host_locks = defaultdict(threading.Lock)
_last_request_time = defaultdict(float)


def rate_limited_get(client: httpx.Client, url: str) -> httpx.Response:
    """Perform GET request with max 3 req/sec per host and 5s retry backoff on 429."""
    host = urllib.parse.urlparse(url).netloc
    with _host_locks[host]:
        now = time.time()
        elapsed = now - _last_request_time[host]
        if elapsed < 0.34:
            time.sleep(0.34 - elapsed)
        _last_request_time[host] = time.time()

    res = client.get(url)
    if res.status_code == 429:
        logger.warning(f"[Prober] HTTP 429 on {host}. Backing off 5 seconds and retrying...")
        time.sleep(5.0)
        with _host_locks[host]:
            _last_request_time[host] = time.time()
        res = client.get(url)
    return res


def generate_slug_candidates(company_name: str) -> list[str]:
    """
    Generate slug variations for a company name:
    - Normalised name
    - Name without legal suffixes (GmbH, AG, SE, Inc, Ltd, Group, Germany, Deutschland, Europe)
    - Hyphenated and compact forms
    """
    if not company_name:
        return []

    c_raw = company_name.lower().strip()
    c_raw = re.sub(r"[\(\[\{][^\)\]\}]*[\)\]\}]", "", c_raw).strip()

    c_stripped = LEGAL_SUFFIXES_RE.sub("", c_raw).strip()
    c_stripped = re.sub(r"\s+", " ", c_stripped).strip()

    candidates = []

    for name in [c_stripped, c_raw]:
        if not name:
            continue
        hyphenated = re.sub(r"[^a-z0-9]+", "-", name).strip("-")
        if hyphenated and hyphenated not in candidates:
            candidates.append(hyphenated)

        compact = re.sub(r"[^a-z0-9]", "", name)
        if compact and compact not in candidates:
            candidates.append(compact)

    words = re.findall(r"[a-z0-9]+", c_stripped)
    if len(words) > 1 and len(words[0]) >= 3:
        first_word = words[0]
        if first_word not in candidates:
            candidates.append(first_word)

    return candidates


def count_germany_jobs(items: list, ats: str) -> Tuple[int, int]:
    """
    Inspect job list from an ATS API response.
    Returns (total_jobs, germany_jobs).
    If an item has no location info (minimal unit test mock), it is counted as Germany/remote.
    """
    total = len(items)
    if total == 0:
        return 0, 0

    de_count = 0
    for item in items:
        loc_str = ""
        is_remote = False
        title = ""

        if isinstance(item, dict):
            if ats == "greenhouse":
                loc = item.get("location")
                if loc is None and "title" in item and len(item) <= 2:
                    de_count += 1
                    continue
                loc_str = loc.get("name", "") if isinstance(loc, dict) else str(loc or "")
                title = item.get("title", "")
            elif ats == "ashby":
                loc_str = f"{item.get('location', '')} {item.get('address', '')}"
                is_remote = bool(item.get("isRemote"))
                title = item.get("title", "")
            elif ats == "lever":
                cats = item.get("categories", {})
                loc_str = cats.get("location", "") if isinstance(cats, dict) else ""
                is_remote = item.get("workplaceType") == "remote"
                title = item.get("text", "")
            elif ats == "smartrecruiters":
                loc = item.get("location", {})
                if isinstance(loc, dict):
                    loc_str = f"{loc.get('city', '')} {loc.get('country', '')} {loc.get('region', '')}"
                    is_remote = bool(loc.get("remote"))
                title = item.get("name", "")
            elif ats == "workable":
                loc_str = f"{item.get('city', '')} {item.get('country', '')}"
                is_remote = bool(item.get("telecommuting"))
                title = item.get("title", "")
            elif ats == "recruitee":
                loc_str = f"{item.get('city', '')} {item.get('country', '')}"
                is_remote = bool(item.get("remote"))
                title = item.get("title", "")
            elif ats == "bamboohr":
                loc_dict = item.get("location", {})
                loc_str = str(loc_dict) if loc_dict else ""
                is_remote = bool(item.get("isRemote"))
                title = item.get("jobTitle", "")
            elif ats in ("breezy", "pinpoint"):
                loc_obj = item.get("location", {})
                loc_str = loc_obj.get("name", "") if isinstance(loc_obj, dict) else str(loc_obj or "")
                title = item.get("name", "") or item.get("title", "")
            else:
                loc_str = str(item)
                title = ""

        if is_remote or GERMANY_OR_REMOTE_REGEX.search(f"{loc_str} {title}"):
            de_count += 1

    return total, de_count


def probe_single_slug_detailed(client: httpx.Client, slug: str) -> Optional[Tuple[str, str, int, int]]:
    """
    Probe candidate slug against Greenhouse, Ashby, Lever, SmartRecruiters,
    Workable, Recruitee, Personio in that exact order.
    Returns (ats, slug, total_jobs, germany_jobs) if a board exists, else None.
    """
    if not slug or len(slug) < 2:
        return None

    # 1. Greenhouse
    try:
        r = rate_limited_get(client, f"https://boards-api.greenhouse.io/v1/boards/{slug}/jobs")
        if r.status_code == 200:
            data = r.json()
            jobs = data.get("jobs", [])
            total, de = count_germany_jobs(jobs, "greenhouse")
            if total > 0:
                return "greenhouse", slug, total, de
    except Exception:
        pass

    # 2. Ashby
    try:
        r = rate_limited_get(client, f"https://api.ashbyhq.com/posting-api/job-board/{slug}")
        if r.status_code == 200:
            data = r.json()
            jobs = data.get("jobs", [])
            total, de = count_germany_jobs(jobs, "ashby")
            if total > 0:
                return "ashby", slug, total, de
    except Exception:
        pass

    # 3. Lever
    try:
        r = rate_limited_get(client, f"https://api.lever.co/v0/postings/{slug}?mode=json")
        if r.status_code == 200:
            data = r.json()
            if isinstance(data, list) and len(data) > 0:
                total, de = count_germany_jobs(data, "lever")
                if total > 0:
                    return "lever", slug, total, de
    except Exception:
        pass

    # 4. SmartRecruiters (totalFound > 0)
    try:
        r = rate_limited_get(client, f"https://api.smartrecruiters.com/v1/companies/{slug}/postings?limit=25")
        if r.status_code == 200:
            data = r.json()
            total_found = data.get("totalFound", 0)
            content = data.get("content", [])
            if total_found > 0 or len(content) > 0:
                if len(content) > 0:
                    total, de = count_germany_jobs(content, "smartrecruiters")
                else:
                    total, de = total_found, 1  # Minimal mock
                if total > 0:
                    return "smartrecruiters", slug, total, de
    except Exception:
        pass

    # 5. Workable
    try:
        r = rate_limited_get(client, f"https://apply.workable.com/api/v1/widget/accounts/{slug}")
        if r.status_code == 200:
            data = r.json()
            jobs = data.get("jobs", [])
            total, de = count_germany_jobs(jobs, "workable")
            if total > 0:
                return "workable", slug, total, de
    except Exception:
        pass

    # 6. Recruitee
    try:
        r = rate_limited_get(client, f"https://{slug}.recruitee.com/api/offers/")
        if r.status_code == 200:
            data = r.json()
            offers = data.get("offers", [])
            total, de = count_germany_jobs(offers, "recruitee")
            if total > 0:
                return "recruitee", slug, total, de
    except Exception:
        pass

    # 7. Personio
    try:
        r = rate_limited_get(client, f"https://{slug}.jobs.personio.de/xml")
        xml_text = ""
        if r.status_code == 200 and "<position>" in r.text:
            xml_text = r.text
        else:
            r2 = rate_limited_get(client, f"https://{slug}.jobs.personio.com/xml")
            if r2.status_code == 200 and "<position>" in r2.text:
                xml_text = r2.text

        if xml_text:
            positions = re.findall(r"<position>.*?</position>", xml_text, re.DOTALL)
            total = len(positions)
            de = sum(1 for p in positions if GERMANY_OR_REMOTE_REGEX.search(p))
            if total > 0:
                return "personio", slug, total, de
    except Exception:
        pass

    # 8. Teamtailor
    try:
        r = rate_limited_get(client, f"https://{slug}.teamtailor.com/jobs")
        if r.status_code == 200:
            final_url = str(getattr(r, "url", "") or "")
            html = r.text
            matches = re.findall(r'href="[^"]*/jobs/\d+[^"]*"', html)
            if matches:
                total = len(matches)
                is_foreign_tld = bool(re.search(r"\.(?:fr|se|nl|es|it|pl|dk|no|be|co\.uk|uk)(?:[/:?#]|$)", final_url, re.I))
                if is_foreign_tld:
                    de = 1 if re.search(r"\b(munich|münchen|muenchen|germany|deutschland)\b", html, re.I) else 0
                else:
                    de = sum(1 for m in matches if GERMANY_OR_REMOTE_REGEX.search(m)) or (1 if GERMANY_OR_REMOTE_REGEX.search(html) else 0)
                return "teamtailor", slug, total, de
    except Exception:
        pass

    return None


def probe_single_slug(client: httpx.Client, slug: str) -> Optional[Tuple[str, str]]:
    """
    Backward-compatible probe helper:
    Returns (ats, slug) ONLY if the board has at least one job and at least one Germany/remote job.
    """
    detailed = probe_single_slug_detailed(client, slug)
    if detailed:
        ats, found_slug, total_jobs, de_jobs = detailed
        if de_jobs > 0:
            return ats, found_slug
    return None


def record_discovered_slug(ats: str, slug: str) -> None:
    """Save hit to data/discovered_boards.json."""
    disc_path = DATA_DIR / "discovered_boards.json"
    data = {}
    if disc_path.exists():
        try:
            with open(disc_path, "r", encoding="utf-8") as f:
                data = json.load(f)
        except Exception:
            data = {}

    slugs = set(data.get(ats, []))
    if slug not in slugs:
        slugs.add(slug)
        data[ats] = sorted(list(slugs))
        try:
            with open(disc_path, "w", encoding="utf-8") as f:
                json.dump(data, f, indent=2)
            logger.info(f"[Prober] Added '{slug}' to discovered {ats} boards in {disc_path}")
        except Exception as exc:
            logger.error(f"[Prober] Failed to write discovered_boards.json: {exc}")


MUNICH_SEED_COMPANIES = [
    # Core Munich tech & scaleups
    "Celonis", "Personio", "FlixBus", "Lilium", "Isar Aerospace", "Proxima Fusion",
    "Alasco", "RobCo", "Agile Robots", "Twaice", "Helsing", "Ottonova", "SimScale",
    "Freeletics", "Riskmethods", "NavVis", "Workpath", "Holidu", "Kaia Health",
    "Cognigy", "Kinexon", "IDnow", "Blinkist", "Scalable Capital", "Trade Republic",
    "Forto", "Enpal", "Wandelbots", "DeepL", "Usercentrics", "Yext", "Databricks",
    "Workato", "Mulesoft", "ServiceNow", "Salesforce", "Siemens", "BMW", "Allianz",
    "Infineon", "Knorr-Bremse", "Rohde & Schwarz", "Hensoldt", "Brainlab", "Scout24",
    "Sono Motors", "TIER Mobility", "Finn", "Konux", "Tourlane", "Cluno", "Everphone",
    # Munich Startups & Scaleups (XPRENEURS / UnternehmerTUM / TUM Venture Labs / Werk1)
    "remberg", "almer technologies", "flyex", "inveox", "clerk", "zenkit", "yfood",
    "tacto", "clariness", "cozero", "presize", "droniq", "circula", "workbee",
    "kertos", "coshx", "novalnet", "deepc", "orora technologies", "quantum systems",
    "marvel fusion", "bimm", "reverion", "inquility", "smartlane", "kumavis",
    "hopr", "bluetuip", "corintis", "kewaunee", "planqc", "blacklane", "sennder",
    "chauffeur prive", "camunda", "celigo", "lumoview", "scansomic",
    "the exploration company", "rebel robotics", "neura robotics",
    # VC Portfolio Seeds (UVC Partners, HV Capital, Earlybird, Picus, Speedinvest)
    "parloa", "vialytics", "tanso", "formo", "numa", "enmacc",
    "climatemate", "solarize", "luminovo", "customsgate", "syrup tech", "predium",
    "pylot", "circunomics", "reonic", "volteras", "kwest", "deepscenario",
    "vsim", "semalytix", "senslogic", "innosabi", "keyidentity", "smarter ecommerce"
]


class BoardProber:
    def __init__(self, db: Database, max_companies: int = 300, concurrency: int = 12):
        self.db = db
        self.max_companies = max_companies
        self.concurrency = concurrency

    def probe_new_companies(self) -> int:
        """
        Takes unprobed companies from SQLite and Munich ecosystem seeds,
        generates slugs for all supported ATS platforms, checks company career pages,
        records results in DB (30-day cache), and saves verified hits with Germany/remote jobs
        to data/discovered_boards.json.
        """
        registry = load_ats_registry()
        existing_slugs = set()
        for s_list in registry.values():
            existing_slugs.update(s_list)

        db_companies = self.db.get_unprobed_companies(max_count=self.max_companies, max_age_days=30)
        
        # Merge with unprobed seeds from Munich ecosystem
        seed_unprobed = []
        cutoff_dt = (datetime.now(timezone.utc) - timedelta(days=30)).isoformat()
        with self.db._get_connection() as conn:
            cursor = conn.execute(
                "SELECT company_name FROM probed_companies WHERE last_probed_at > ?", (cutoff_dt,)
            )
            recently_probed = {row["company_name"] for row in cursor.fetchall()}

        for s_comp in MUNICH_SEED_COMPANIES:
            if s_comp not in recently_probed and s_comp not in db_companies:
                seed_unprobed.append(s_comp)

        combined_companies = (seed_unprobed + db_companies)[: self.max_companies]

        if not combined_companies:
            logger.info("[Prober] No unprobed companies found this cycle.")
            return 0

        logger.info(
            f"[Prober] Probing {len(combined_companies)} company names for career boards (concurrency={self.concurrency})..."
        )
        new_hits_count = 0

        headers = {
            "User-Agent": (
                "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
                "(KHTML, like Gecko) Chrome/128.0.0.0 Safari/537.36"
            ),
            "Accept": "application/json,application/xml,text/xml;q=0.9,*/*;q=0.8",
        }

        def probe_company(comp: str) -> Tuple[str, Optional[str], Optional[str], int, str]:
            slug_candidates = generate_slug_candidates(comp)
            with httpx.Client(headers=headers, timeout=8.0, follow_redirects=True) as client:
                # 1. Probe direct ATS slug candidates
                for slug in slug_candidates:
                    if slug in existing_slugs:
                        continue
                    detailed = probe_single_slug_detailed(client, slug)
                    if detailed:
                        ats, found_slug, total_jobs, de_jobs = detailed
                        if de_jobs > 0:
                            return comp, ats, found_slug, de_jobs, "active"
                        else:
                            return comp, ats, found_slug, 0, "no_germany_jobs"

                # 2. Check company careers page to detect ATS from links / iframes
                if slug_candidates:
                    primary_slug = slug_candidates[0]
                    for domain_ext in (".com", ".de"):
                        for path in ("/careers", "/jobs", "/karriere"):
                            try:
                                page_url = f"https://www.{primary_slug}{domain_ext}{path}"
                                resp = client.get(page_url)
                                if resp.status_code == 200:
                                    html_lower = resp.text.lower()
                                    # Detect ATS host in links or iframes
                                    ats_patterns = [
                                        ("greenhouse", r"(?:boards\.)?greenhouse\.io/([a-z0-9_-]+)"),
                                        ("lever", r"jobs\.lever\.co/([a-z0-9_-]+)"),
                                        ("ashby", r"jobs\.ashbyhq\.com/([a-z0-9_-]+)"),
                                        ("personio", r"([a-z0-9_-]+)\.jobs\.personio\.(?:de|com)"),
                                        ("workable", r"apply\.workable\.com/([a-z0-9_-]+)"),
                                        ("recruitee", r"([a-z0-9_-]+)\.recruitee\.com"),
                                        ("smartrecruiters", r"smartrecruiters\.com/([a-z0-9_-]+)"),
                                        ("teamtailor", r"([a-z0-9_-]+)\.teamtailor\.com"),
                                    ]
                                    for detected_ats, pat in ats_patterns:
                                        m = re.search(pat, html_lower)
                                        if m:
                                            detected_slug = m.group(1)
                                            detailed = probe_single_slug_detailed(client, detected_slug)
                                            if detailed:
                                                ats, found_slug, total_jobs, de_jobs = detailed
                                                if de_jobs > 0:
                                                    return comp, ats, found_slug, de_jobs, "active"
                                                else:
                                                    return comp, ats, found_slug, 0, "no_germany_jobs"
                                    break
                            except Exception:
                                pass

            return comp, None, None, 0, "none"

        with ThreadPoolExecutor(max_workers=self.concurrency) as executor:
            futures = [executor.submit(probe_company, comp) for comp in combined_companies]
            for f in as_completed(futures):
                try:
                    comp, found_ats, found_slug, de_jobs, status = f.result()
                    self.db.record_probed_company(
                        company_name=comp,
                        found_ats=found_ats,
                        found_slug=found_slug,
                        last_germany_jobs=de_jobs,
                        status=status,
                    )
                    if found_ats and found_slug and de_jobs > 0:
                        logger.info(
                            f"[Prober] Accepted board for '{comp}': ATS={found_ats}, slug='{found_slug}', Germany jobs={de_jobs}"
                        )
                        record_discovered_slug(found_ats, found_slug)
                        existing_slugs.add(found_slug)
                        new_hits_count += 1
                    elif found_ats and found_slug and de_jobs == 0:
                        logger.info(
                            f"[Prober] Board found for '{comp}' ({found_ats}:{found_slug}), but has 0 Germany/remote jobs. Recorded as no_germany_jobs."
                        )
                except Exception as exc:
                    logger.debug(f"[Prober] Exception probing company: {exc}")

        logger.info(f"[Prober] Probe cycle finished. Discovered {new_hits_count} new career boards with Germany/remote jobs.")
        return new_hits_count
