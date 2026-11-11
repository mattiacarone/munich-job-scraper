"""
Arbeitnow API Scraper with Employer Apply Link Redirect Resolution (Brief 5).
Follows all pages until links.next is null.
URL: https://www.arbeitnow.com/api/job-board-api
Job ID: arbeitnow:{slug}

For rows passing cheap preliminary gates:
- GET <job url>/apply with follow_redirects=True (15s timeout, 3 concurrent).
- Store final URL (stripped of utm_*, ref) in apply_url.
- Detect ATS, ats_slug, apply_kind.
- If final URL remains on arbeitnow.*, reject with gate_rejected = 'no-employer-link'.
- Cache resolved apply URLs in database table resolved_apply_urls.
- Record new ATS boards in data/discovered_boards.json.
"""

import logging
import re
from concurrent.futures import ThreadPoolExecutor, as_completed
from datetime import datetime
from typing import Optional, Tuple
import httpx
from apply_classifier import classify_apply_kind, detect_ats_from_url, extract_host_from_url, record_discovered_board, strip_utm_params
from models import JobPosting
from scrapers.base import BaseScraper

logger = logging.getLogger(__name__)

ARBEITNOW_FOREIGN_PLACES_PAT = re.compile(
    r"\b("
    r"Montpellier|Paris|Lyon|Marseille|Toulouse|Nice|Nantes|Strasbourg|Bordeaux|Lille|Rennes|Grenoble|"
    r"Amsterdam|Rotterdam|Utrecht|Eindhoven|The\s+Hague|Den\s+Haag|"
    r"Madrid|Barcelona|Valencia|Seville|Sevilla|Bilbao|Malaga|"
    r"Milan|Milano|Rome|Roma|Turin|Torino|Bologna|Florence|Firenze|Naples|"
    r"London|Manchester|Edinburgh|Birmingham|Bristol|Cambridge|Oxford|Glasgow|Leeds|Belfast|"
    r"Dublin|Cork|Stockholm|Gothenburg|Göteborg|Malmö|Uppsala|Oslo|Copenhagen|København|Helsinki|Espoo|"
    r"Warsaw|Warszawa|Krakow|Kraków|Wroclaw|Wrocław|Gdansk|Gdańsk|Poznan|Poznań|Prague|Praha|Brno|"
    r"Vienna|Wien|Linz|Graz|Salzburg|Zurich|Zürich|Geneva|Genève|Basel|Bern|Lausanne|"
    r"Brussels|Bruxelles|Antwerp|Ghent|Lisbon|Lisboa|Porto|Athens|Bucharest|Budapest|"
    r"Berlin|Hamburg|Frankfurt|Stuttgart|Cologne|Köln|Düsseldorf|Leipzig|Dresden|Hannover|Nuremberg|Nürnberg|Finsterwalde|"
    r"France|Spain|España|Italy|Italia|Netherlands|Nederland|BeNeLux|Belgium|Belgique|Sweden|Sverige|"
    r"Norway|Norge|Denmark|Danmark|Finland|Poland|Polska|United\s+Kingdom|UK|U\.K\.|Ireland|"
    r"Switzerland|Schweiz|Suisse|Austria|Österreich|Portugal|Nordics|Baltics|Iberia"
    r")\b",
    re.I,
)


def infer_arbeitnow_location(
    raw_location: Optional[str],
    title: str = "",
    description: str = "",
    url: str = "",
) -> Tuple[Optional[str], str]:
    """
    Determine location and location_source for an Arbeitnow job without ever defaulting to Munich.
    For arbeitnow country sites (.fr, .nl, .es, .it, .co.uk, .ch, etc.) or missing locations,
    if the title or text names a non-German city or country (and not Munich), set that as location.
    """
    loc = (raw_location or "").strip() or None
    host = (extract_host_from_url(url) or "").lower()
    is_country_site = bool(
        host
        and "arbeitnow" in host
        and not host.endswith("arbeitnow.com")
        and not host.endswith("arbeitnow.de")
    )

    title_has_munich = bool(re.search(r"\b(munich|münchen)\b", title or "", re.I))
    loc_has_munich = bool(loc and re.search(r"\b(munich|münchen)\b", loc, re.I))

    # 1. Check title for explicit non-German city / country
    m_title = ARBEITNOW_FOREIGN_PLACES_PAT.search(title or "")
    if m_title and not title_has_munich and not loc_has_munich:
        return m_title.group(1), "text"

    # 2. For country sites or missing location, check description for non-German city / country
    if is_country_site or not loc:
        desc_clean = re.sub(r"<[^>]+>", " ", description or "")
        desc_has_munich = bool(re.search(r"\b(munich|münchen)\b", desc_clean, re.I))
        if not loc_has_munich and not title_has_munich and not desc_has_munich:
            m_desc = ARBEITNOW_FOREIGN_PLACES_PAT.search(desc_clean)
            if m_desc:
                return m_desc.group(1), "text"

    if loc:
        return loc, "source"
    return None, "unknown"


def resolve_arbeitnow_job(job: JobPosting, client: httpx.Client, db=None) -> JobPosting:
    """
    Resolve the real employer apply URL for an Arbeitnow job:
    GET <job url>/apply with redirects followed.
    """
    if not job.url:
        job.gate_rejected = "no-employer-link"
        job.score = 0.0
        return job

    target_url = job.url.rstrip("/")
    if not target_url.endswith("/apply"):
        target_url = f"{target_url}/apply"

    # 1. Check database cache
    if db:
        cached = db.get_resolved_apply_url(target_url)
        if cached:
            cached_final = cached.get("final_url", "")
            cached_host = extract_host_from_url(cached_final)
            if not cached_final or (cached_host and "arbeitnow" in cached_host):
                job.gate_rejected = "no-employer-link"
                job.score = 0.0
                job.apply_url = None
                job.ats = None
                job.ats_slug = None
                job.apply_kind = "unknown"
                return job

            job.apply_url = cached_final
            job.ats = cached.get("ats")
            job.ats_slug = cached.get("ats_slug")
            job.apply_kind = cached.get("apply_kind") or "unknown"
            job.apply_host = cached_host
            return job

    # 2. Perform HTTP request following redirects
    try:
        res = client.get(target_url)
        final_raw = str(res.url)
        final_clean = strip_utm_params(final_raw)
        host = extract_host_from_url(final_clean)

        # If redirected back to arbeitnow or remained on arbeitnow
        if not host or "arbeitnow" in host:
            job.gate_rejected = "no-employer-link"
            job.score = 0.0
            job.apply_url = None
            job.ats = None
            job.ats_slug = None
            job.apply_kind = "unknown"
            if db:
                db.save_resolved_apply_url(
                    url=target_url,
                    final_url=final_raw,
                    ats=None,
                    ats_slug=None,
                    apply_kind="unknown",
                )
            return job

        # External employer apply URL resolved!
        ats, ats_slug = detect_ats_from_url(final_clean)
        kind, apply_host = classify_apply_kind(apply_url=final_clean, ats=ats)

        job.apply_url = final_clean
        job.ats = ats
        job.ats_slug = ats_slug
        job.apply_kind = kind
        job.apply_host = apply_host or host

        if ats and ats_slug:
            record_discovered_board(ats, ats_slug)

        if db:
            db.save_resolved_apply_url(
                url=target_url,
                final_url=final_clean,
                ats=ats,
                ats_slug=ats_slug,
                apply_kind=kind,
            )

        logger.info(f"[arbeitnow] Resolved {job.id} -> {final_clean} ({kind}, ats={ats})")
        return job

    except Exception as exc:
        logger.warning(f"[arbeitnow] Failed to resolve apply URL for {job.id}: {exc}")
        job.gate_rejected = "no-employer-link"
        job.score = 0.0
        job.apply_url = None
        return job


class ArbeitnowScraper(BaseScraper):
    API_URL = "https://www.arbeitnow.com/api/job-board-api"

    def __init__(self, db=None, matcher=None, max_pages: int = 15, concurrency: int = 3):
        super().__init__(name="arbeitnow")
        self.db = db
        self.matcher = matcher
        self.max_pages = max_pages
        self.concurrency = concurrency

    def resolve_candidates(self, candidates: list[JobPosting]) -> list[JobPosting]:
        """Resolve employer apply links concurrently for a batch of candidate postings."""
        if not candidates:
            return []

        resolved = []
        with httpx.Client(
            headers=self.headers,
            timeout=15.0,
            follow_redirects=True,
        ) as client:
            with ThreadPoolExecutor(max_workers=self.concurrency) as executor:
                future_to_job = {
                    executor.submit(resolve_arbeitnow_job, j, client, self.db): j
                    for j in candidates
                }
                for f in as_completed(future_to_job):
                    try:
                        resolved_job = f.result()
                        resolved.append(resolved_job)
                    except Exception as exc:
                        logger.debug(f"[{self.name}] Resolution error: {exc}")

        return resolved

    def scrape(self) -> list[JobPosting]:
        jobs: list[JobPosting] = []
        logger.info(f"[{self.name}] Initiating sync from Arbeitnow...")
        next_url = self.API_URL
        page_num = 0

        with httpx.Client(headers=self.headers, timeout=20.0) as client:
            while next_url and page_num < self.max_pages:
                page_num += 1
                try:
                    res = client.get(next_url)
                    if res.status_code != 200:
                        logger.warning(f"[{self.name}] Page {page_num} returned status {res.status_code}")
                        break

                    data = res.json()
                    items = data.get("data", [])
                    if not items:
                        break

                    for item in items:
                        slug = str(item.get("slug", "")).strip()
                        if not slug:
                            continue

                        title = item.get("title", "").strip()
                        company = item.get("company_name", "").strip()
                        url = item.get("url", "").strip()
                        raw_location = item.get("location", "").strip()
                        is_remote = item.get("remote", False)

                        if is_remote and raw_location and "remote" not in raw_location.lower():
                            raw_location = f"{raw_location} (Remote)"
                        elif is_remote and not raw_location:
                            raw_location = "Remote"

                        desc = item.get("description", "") or ""
                        loc_val, loc_src = infer_arbeitnow_location(raw_location, title, desc, url)
                        job_key = f"arbeitnow:{slug}"

                        posted_at: Optional[datetime] = None
                        if item.get("created_at"):
                            try:
                                posted_at = datetime.fromtimestamp(int(item["created_at"]))
                            except Exception:
                                pass

                        jobs.append(
                            JobPosting(
                                id=job_key,
                                title=title,
                                company=company,
                                company_display=company,
                                location=loc_val,
                                location_source=loc_src,
                                url=url,
                                description=desc,
                                source=self.name,
                                posted_at=posted_at,
                            )
                        )

                    links = data.get("links", {})
                    next_url = links.get("next")
                except Exception as exc:
                    logger.error(f"[{self.name}] Page {page_num} error: {exc}")
                    break

        logger.info(f"[{self.name}] Ingested {len(jobs)} postings across {page_num} pages.")

        # Resolve employer apply URL for rows passing cheap preliminary gates
        candidates_to_resolve = []
        for j in jobs:
            if self.matcher:
                ok_loc, _ = self.matcher.check_gate_location(j.location, j.title, j.description, j.url, j.location_source)
                ok_sen, _ = self.matcher.check_gate_seniority(j.title)
                ok_age, _ = self.matcher.check_gate_age(j.posted_at)
                ok_skip, _ = self.matcher.check_gate_skip_list(j.company)
                ok_off, _ = self.matcher.check_gate_off_function(j.title)
                ok_ger_tit, _ = self.matcher.check_gate_german(j.title, "")
                if ok_loc and ok_sen and ok_age and ok_skip and ok_off and ok_ger_tit:
                    candidates_to_resolve.append(j)
                else:
                    # Mark unpromising Arbeitnow postings without hitting network
                    j.apply_kind = "unknown"
            else:
                candidates_to_resolve.append(j)

        if candidates_to_resolve:
            logger.info(
                f"[{self.name}] Resolving employer apply URLs for {len(candidates_to_resolve)} cheap-gate candidates (concurrency={self.concurrency})..."
            )
            self.resolve_candidates(candidates_to_resolve)

        return jobs
