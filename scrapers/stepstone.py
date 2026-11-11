"""
StepStone Job Board Scraper.
Scrapes English-friendly roles in Munich from StepStone.de:
https://www.stepstone.de/jobs/<slugified-query>/in-muenchen?radius=30&action=facet_selected%3blanguage%3ben&page=<1..3>

Rotates queries from linkedin_queries.txt (max 40 per cycle, resumes where it left off).
Parses job cards from server-rendered HTML / embedded JSON state (__PRELOADED_STATE__).
Relative dates ('vor 2 Tagen', 'vor 3 Stunden', 'gestern', 'heute') converted to datetime.
Job ID format: stepstone:<id>

Detail fetching for cheap-gate candidates (max 80 per cycle):
- Checks apply target: internal StepStone login application -> apply_kind = 'account' (rejected by Gate 8)
- External employer ATS link -> store in apply_url and classify.
Rate limit: max 1 req / 2s. Backoff on 429/403. Stop source for cycle after 3 consecutive errors.
"""

from datetime import datetime, timedelta, timezone
import json
import logging
import re
import time
from typing import Optional
import urllib.parse
import httpx

from apply_classifier import classify_apply_kind, extract_host_from_url
from config import get_linkedin_queries_path
from models import JobPosting
from scrapers.base import BaseScraper

logger = logging.getLogger(__name__)


def parse_relative_date(date_str: str) -> Optional[datetime]:
    """Parse StepStone date strings into UTC datetime."""
    if not date_str:
        return None
    s = str(date_str).strip().lower()
    now = datetime.now(timezone.utc)

    # ISO formats
    if re.match(r"^\d{4}-\d{2}-\d{2}", s):
        try:
            dt = datetime.fromisoformat(s.replace("Z", "+00:00"))
            return dt if dt.tzinfo else dt.replace(tzinfo=timezone.utc)
        except Exception:
            pass

    if "heute" in s or "just now" in s or "gerade" in s:
        return now
    if "gestern" in s or "yesterday" in s:
        return now - timedelta(days=1)

    m_days = re.search(r"vor\s+(\d+)\s+tag", s)
    if m_days:
        return now - timedelta(days=int(m_days.group(1)))

    m_hours = re.search(r"vor\s+(\d+)\s+stunde", s)
    if m_hours:
        return now - timedelta(hours=int(m_hours.group(1)))

    m_mins = re.search(r"vor\s+(\d+)\s+minute", s)
    if m_mins:
        return now - timedelta(minutes=int(m_mins.group(1)))

    m_weeks = re.search(r"vor\s+(\d+)\s+woche", s)
    if m_weeks:
        return now - timedelta(days=int(m_weeks.group(1)) * 7)

    return None


class StepStoneScraper(BaseScraper):
    def __init__(
        self,
        db=None,
        matcher=None,
        max_queries: int = 40,
        max_detail_fetches: int = 80,
        delay_seconds: float = 2.0,
    ):
        super().__init__(name="stepstone")
        self.db = db
        self.matcher = matcher
        self.max_queries = max_queries
        self.max_detail_fetches = max_detail_fetches
        self.delay_seconds = delay_seconds
        self.headers = {
            "User-Agent": (
                "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
                "AppleWebKit/537.36 (KHTML, like Gecko) "
                "Chrome/125.0.0.0 Safari/537.36"
            ),
            "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,application/json,*/*;q=0.8",
            "Accept-Language": "en-US,en;q=0.9,de;q=0.8",
            "DNT": "1",
            "Connection": "keep-alive",
            "Sec-Ch-Ua": '"Google Chrome";v="125", "Chromium";v="125", "Not.A/Brand";v="24"',
            "Sec-Ch-Ua-Mobile": "?0",
            "Sec-Ch-Ua-Platform": '"Windows"',
        }

    def _load_queries(self) -> list[str]:
        path = get_linkedin_queries_path()
        if not path.exists():
            return ["Customer Success", "Integration", "Solutions Architect", "Technical Consultant"]
        lines = [line.strip() for line in path.read_text(encoding="utf-8").splitlines() if line.strip()]
        return lines or ["Customer Success", "Integration"]

    def _slugify(self, text: str) -> str:
        s = text.lower()
        s = re.sub(r"[^a-z0-9]+", "-", s).strip("-")
        return s or "jobs"

    def scrape(self) -> list[JobPosting]:
        queries = self._load_queries()
        if not queries:
            return []

        # Rotate query window
        start_idx = 0
        if self.db:
            try:
                saved = self.db.get_metadata("last_stepstone_query_index")
                if saved is not None:
                    start_idx = int(saved) % len(queries)
            except Exception:
                start_idx = 0

        selected_queries = []
        for i in range(min(self.max_queries, len(queries))):
            idx = (start_idx + i) % len(queries)
            selected_queries.append(queries[idx])
        next_idx = (start_idx + len(selected_queries)) % len(queries)
        if self.db:
            self.db.set_metadata("last_stepstone_query_index", str(next_idx))

        logger.info(
            f"[StepStone] Starting scrape cycle for {len(selected_queries)} queries (index {start_idx}..{next_idx})."
        )

        all_jobs: dict[str, JobPosting] = {}
        consecutive_errors = 0

        with httpx.Client(headers=self.headers, timeout=20.0, follow_redirects=True) as client:
            for q_idx, query in enumerate(selected_queries):
                if consecutive_errors >= 3:
                    logger.warning("[StepStone] 3 consecutive errors hit. Stopping StepStone source for this cycle.")
                    break

                q_slug = self._slugify(query)
                for page in range(1, 4):  # pages 1..3
                    url = (
                        f"https://www.stepstone.de/jobs/{q_slug}/in-muenchen"
                        f"?radius=30&action=facet_selected%3blanguage%3ben&page={page}"
                    )
                    time.sleep(self.delay_seconds)
                    try:
                        res = client.get(url)
                        if res.status_code in (429, 403):
                            consecutive_errors += 1
                            logger.warning(
                                f"[StepStone] HTTP {res.status_code} for query '{query}' page {page}. Backing off 5s."
                            )
                            time.sleep(5.0)
                            continue

                        if res.status_code != 200:
                            consecutive_errors += 1
                            logger.warning(f"[StepStone] HTTP {res.status_code} on {url}")
                            continue

                        consecutive_errors = 0
                        page_jobs = self._parse_search_page(res.text)
                        if not page_jobs:
                            # No more jobs for this query
                            break

                        for j in page_jobs:
                            if j.id not in all_jobs:
                                all_jobs[j.id] = j

                    except Exception as exc:
                        consecutive_errors += 1
                        logger.error(f"[StepStone] Error fetching {url}: {exc}")
                        if consecutive_errors >= 3:
                            break

            # Detail fetch for cheap-gate candidate postings (max 80 per cycle)
            candidates_to_enrich = []
            for j in all_jobs.values():
                if self._passes_cheap_gates(j):
                    candidates_to_enrich.append(j)

            enrich_limit = min(self.max_detail_fetches, len(candidates_to_enrich))
            logger.info(
                f"[StepStone] Discovered {len(all_jobs)} jobs; {len(candidates_to_enrich)} pass cheap gates. "
                f"Enriching up to {enrich_limit} detail pages..."
            )

            detail_errors = 0
            for j in candidates_to_enrich[:enrich_limit]:
                if detail_errors >= 3:
                    logger.warning("[StepStone] 3 consecutive detail errors. Stopping detail enrichment.")
                    break
                time.sleep(self.delay_seconds)
                try:
                    res = client.get(j.url)
                    if res.status_code in (429, 403):
                        detail_errors += 1
                        time.sleep(5.0)
                        continue
                    if res.status_code != 200:
                        detail_errors += 1
                        continue

                    detail_errors = 0
                    self._parse_detail_page(j, res.text)
                except Exception as exc:
                    detail_errors += 1
                    logger.debug(f"[StepStone] Failed to enrich detail for {j.id}: {exc}")

        return list(all_jobs.values())

    def _parse_search_page(self, html: str) -> list[JobPosting]:
        jobs: list[JobPosting] = []

        # 1. Attempt extracting structured state
        match = re.search(
            r'window\.__PRELOADED_STATE__\["app-unifiedResultlist"\]\s*=\s*(\{.*?\});\s*(?:window\.|</script>)',
            html,
            re.DOTALL,
        )
        if match:
            try:
                data = json.loads(match.group(1))
                items = data.get("searchResults", {}).get("items", [])
                for it in items:
                    raw_id = it.get("id")
                    if not raw_id:
                        continue
                    posting_id = str(raw_id)
                    title = it.get("title", "").strip()
                    comp = it.get("companyName", "").strip() or "Unknown Company"
                    loc = it.get("location", "").strip() or None
                    rel_url = it.get("url", "")
                    job_url = "https://www.stepstone.de" + rel_url if rel_url.startswith("/") else rel_url

                    date_posted = it.get("datePosted") or it.get("periodPostedDate")
                    posted_at = parse_relative_date(date_posted)

                    snippet = it.get("textSnippet", "")
                    # Clean html tags in snippet
                    clean_snippet = re.sub(r"<[^>]+>", " ", snippet).strip()

                    job = JobPosting(
                        id=f"stepstone:{posting_id}",
                        title=title,
                        company=comp,
                        company_display=comp,
                        location=loc,
                        url=job_url,
                        description=clean_snippet,
                        source=self.name,
                        posted_at=posted_at,
                        apply_kind="account",  # default until detail page clarifies external link
                        apply_host="stepstone.de",
                    )
                    jobs.append(job)
                if jobs:
                    return jobs
            except Exception as e:
                logger.debug(f"[StepStone] Error parsing preloaded state: {e}")

        # 2. Fallback to server-rendered HTML card parsing
        link_matches = re.findall(r'href="(/stellenangebote--([^"]+)--(\d+)-inline\.html)"', html)
        for rel_path, slug, raw_id in link_matches:
            posting_id = str(raw_id)
            title = slug.replace("-", " ").title()
            job_url = f"https://www.stepstone.de{rel_path}"
            jobs.append(
                JobPosting(
                    id=f"stepstone:{posting_id}",
                    title=title,
                    company="StepStone Company",
                    company_display="StepStone Company",
                    location=None,
                    url=job_url,
                    description="",
                    source=self.name,
                    apply_kind="account",
                    apply_host="stepstone.de",
                )
            )

        return jobs

    def _parse_detail_page(self, job: JobPosting, html: str) -> None:
        """Enrich description and classify apply target from StepStone detail HTML."""
        # Check LD+JSON for full description
        ld_jsons = re.findall(r'<script[^>]*type="application/ld\+json"[^>]*>(.*?)</script>', html, re.DOTALL)
        for lj in ld_jsons:
            if '"JobPosting"' in lj:
                try:
                    ld_data = json.loads(lj)
                    desc = ld_data.get("description", "")
                    if desc:
                        job.description = re.sub(r"<[^>]+>", " ", desc).strip()
                    if ld_data.get("hiringOrganization", {}).get("name"):
                        job.company = ld_data["hiringOrganization"]["name"]
                        job.company_display = job.company
                    if ld_data.get("jobLocation", {}).get("address", {}).get("addressLocality"):
                        job.location = ld_data["jobLocation"]["address"]["addressLocality"]
                except Exception:
                    pass

        # Look for external apply URL
        # External employer links appear in buttons or specific JSON tags
        external_url = None

        # Look for explicit external apply links
        match_apply = re.search(
            r'"(?:applyUrl|externalUrl|directApplyUrl|externalListingUrl)":\s*"([^"]+)"', html
        )
        if match_apply:
            candidate_url = match_apply.group(1).replace("\\u002F", "/").replace("\\/", "/")
            if "stepstone.de" not in candidate_url.lower() and "stepstone.com" not in candidate_url.lower():
                external_url = candidate_url

        if not external_url:
            # Check for links with target="_blank" pointing to employer ATS
            links = re.findall(r'href="([^"]+)"[^>]*data-genesis-element="apply-button"', html, re.I)
            for lk in links:
                if "stepstone.de" not in lk.lower() and "stepstone.com" not in lk.lower():
                    external_url = lk
                    break

        # Check if the company has a known ATS board in our registry
        if not external_url and self.db:
            try:
                from intake_checks import resolve_employer_link_via_registry
                res_url = resolve_employer_link_via_registry(job, self.db)
                if res_url:
                    external_url = res_url if isinstance(res_url, str) else job.apply_url
            except Exception:
                pass

        if external_url:
            job.apply_url = external_url
            kind, host = classify_apply_kind(apply_url=external_url, ats=job.ats, source=self.name)
            job.apply_kind = kind
            job.apply_host = host
        else:
            # Internal StepStone application -> account-only
            job.apply_url = None
            job.apply_kind = "account"
            job.apply_host = "stepstone.de"

    def _passes_cheap_gates(self, job: JobPosting) -> bool:
        if not self.matcher:
            return True
        ok_loc, _ = self.matcher.check_gate_location(job.location, job.title)
        ok_sen, _ = self.matcher.check_gate_seniority(job.title)
        ok_age, _ = self.matcher.check_gate_age(job.posted_at)
        ok_skip, _ = self.matcher.check_gate_skip_list(job.company)
        ok_off, _ = self.matcher.check_gate_off_function(job.title)
        ok_ger, _ = self.matcher.check_gate_german(job.title, "")
        return ok_loc and ok_sen and ok_age and ok_skip and ok_off and ok_ger
