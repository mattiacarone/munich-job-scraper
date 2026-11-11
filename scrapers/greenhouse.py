"""
Greenhouse ATS Scraper.
Iterates slugs in data/ats_registry.json using Greenhouse public boards API.
Brief 13 Section 2 & Brief 14 Section A1/A2 optimizations:
- List without content=true
- Apply location & role-rules preliminary check
- Vague locations with fitting titles fetch description (up to 150/cycle cap, cached by job id)
- Fetch /v1/boards/{slug}/jobs/{id} content only for new/updated passing or vague-location IDs
- Per-board cadence: boards with 0 location passes in 30d polled daily (24h), active polled every cycle.
- Preloads known postings and company displays in bulk so worker threads never contend on SQLite.
Job ID format: greenhouse:{slug}:{postingId}
"""

import logging
import threading
from concurrent.futures import ThreadPoolExecutor, as_completed
from datetime import datetime
from typing import Optional
import httpx
from config import load_ats_registry
from models import JobPosting
from scrapers.base import BaseScraper

logger = logging.getLogger(__name__)


class GreenhouseScraper(BaseScraper):
    def __init__(self, db=None, matcher=None, concurrency: int = 20, timeout: float = 20.0):
        super().__init__(name="greenhouse")
        self.db = db
        self.matcher = matcher
        self.concurrency = concurrency
        self.timeout = timeout
        self.slugs = self._load_slugs()
        self.last_postings_listed = 0
        self.last_details_fetched = 0
        self.last_boards_polled = 0
        self._display_cache: Optional[dict[str, str]] = None
        self._new_displays: dict[str, str] = {}
        self._known_postings: Optional[dict[str, tuple[str, str]]] = None
        self._lock = threading.Lock()

    def _load_slugs(self) -> list[str]:
        registry = load_ats_registry()
        return registry.get("greenhouse", [])

    def _get_company_display(self, client: httpx.Client, slug: str) -> str:
        if self._display_cache is not None:
            cached = self._display_cache.get(slug)
            if cached:
                return cached
        elif self.db:
            cached = self.db.get_company_display(self.name, slug)
            if cached:
                return cached

        display_name = None
        try:
            board_url = f"https://boards-api.greenhouse.io/v1/boards/{slug}"
            res = client.get(board_url)
            if res.status_code == 200:
                display_name = res.json().get("name")
        except Exception:
            pass

        if not display_name:
            display_name = slug.replace("-", " ").replace("_", " ").title()

        if self._display_cache is not None:
            with self._lock:
                self._display_cache[slug] = display_name
                self._new_displays[slug] = display_name
        elif self.db:
            self.db.save_company_display(self.name, slug, display_name)

        return display_name

    def _fetch_slug(self, client: httpx.Client, slug: str, batch_mode: bool = False) -> tuple[list[JobPosting], int, int]:
        url = f"https://boards-api.greenhouse.io/v1/boards/{slug}/jobs"
        jobs: list[JobPosting] = []
        listed_count = 0
        details_count = 0

        try:
            res = client.get(url)
            if res.status_code != 200:
                if not batch_mode and self.db and hasattr(self.db, "record_board_poll"):
                    self.db.record_board_poll(self.name, slug)
                return [], 0, 0
            data = res.json()
            raw_jobs = data.get("jobs", [])
            listed_count = len(raw_jobs)
            display_name = self._get_company_display(client, slug)

            if self._known_postings is not None:
                known_postings = self._known_postings
            else:
                known_postings = {}
                if self.db:
                    try:
                        with self.db._get_connection() as conn:
                            cursor = conn.execute(
                                "SELECT id, posted_at, description FROM jobs WHERE id >= ? AND id < ?",
                                (f"greenhouse:{slug}:", f"greenhouse:{slug};"),
                            )
                            for r in cursor.fetchall():
                                known_postings[r["id"]] = (r["posted_at"] or "", r["description"] or "")
                    except Exception:
                        pass

            for item in raw_jobs:
                posting_id = str(item.get("id", "")).strip()
                if not posting_id:
                    continue

                title = item.get("title", "").strip()
                loc_data = item.get("location", {})
                loc_name = loc_data.get("name", "").strip() if isinstance(loc_data, dict) else str(loc_data).strip()
                abs_url = item.get("absolute_url", "").strip()

                posted_at: Optional[datetime] = None
                updated_at_str = item.get("updated_at") or ""
                if updated_at_str:
                    try:
                        posted_at = datetime.fromisoformat(updated_at_str.replace("Z", "+00:00"))
                    except Exception:
                        pass

                job_key = f"greenhouse:{slug}:{posting_id}"

                # Preliminary check on location and role-rules (plus Brief 14 A2 vague location check)
                passes_preliminary = True
                is_vague_candidate = False
                if self.matcher:
                    ok_loc, _ = self.matcher.check_gate_location(loc_name, title, "", abs_url)
                    if not ok_loc:
                        passes_preliminary = False
                        if hasattr(self.matcher, "should_fetch_for_vague_location") and self.matcher.should_fetch_for_vague_location(loc_name, title):
                            is_vague_candidate = True
                    elif self.matcher.role_rules:
                        ok_role, _ = self.matcher.check_gate_role_rules(title)
                        if not ok_role:
                            passes_preliminary = False
                        else:
                            ok_neg, _ = self.matcher.check_gate_negative_keywords(title)
                            if not ok_neg:
                                passes_preliminary = False

                content = ""
                if passes_preliminary or is_vague_candidate:
                    # Check if already in DB with same updated_at and non-empty description
                    known_info = known_postings.get(job_key)
                    if known_info and known_info[1] and len(known_info[1]) > 50:
                        known_posted_at = known_info[0]
                        if not updated_at_str or (updated_at_str[:19] in known_posted_at or known_posted_at[:19] in updated_at_str):
                            content = known_info[1]

                    if not content and is_vague_candidate and self.matcher:
                        cached_vague = self.matcher.vague_description_cache.get(job_key)
                        if cached_vague:
                            content = cached_vague

                    if not content:
                        can_fetch = True
                        if is_vague_candidate and self.matcher and hasattr(self.matcher, "try_claim_vague_fetch"):
                            can_fetch = self.matcher.try_claim_vague_fetch(job_key)
                        if can_fetch:
                            try:
                                detail_url = f"https://boards-api.greenhouse.io/v1/boards/{slug}/jobs/{posting_id}"
                                d_res = client.get(detail_url)
                                if d_res.status_code == 200:
                                    content = d_res.json().get("content", "") or ""
                                    details_count += 1
                                    if is_vague_candidate and self.matcher and content:
                                        with self.matcher._vague_lock:
                                            self.matcher.vague_description_cache[job_key] = content
                            except Exception:
                                content = ""

                jobs.append(
                    JobPosting(
                        id=job_key,
                        title=title,
                        company=display_name,
                        company_display=display_name,
                        location=loc_name or None,
                        url=abs_url,
                        apply_url=abs_url,
                        ats="greenhouse",
                        ats_slug=slug,
                        description=content,
                        source=self.name,
                        posted_at=posted_at,
                    )
                )

            if not batch_mode and self.db and hasattr(self.db, "record_board_poll"):
                self.db.record_board_poll(self.name, slug)

        except Exception as exc:
            logger.debug(f"[{self.name}] Error fetching slug {slug}: {exc}")

        return jobs, listed_count, details_count

    def scrape(self) -> list[JobPosting]:
        all_slugs = self._load_slugs()

        # Brief 13 Section 2 & Brief 14 Section A1: Per-board cadence
        if self.db and hasattr(self.db, "filter_slugs_by_cadence"):
            self.slugs, active_cnt, daily_cnt = self.db.filter_slugs_by_cadence(self.name, all_slugs)
            logger.info(
                f"[{self.name}] Board cadence: {active_cnt} active (every cycle), {daily_cnt} inactive (daily). Polling {len(self.slugs)} boards this cycle."
            )
        else:
            self.slugs = all_slugs
        self.last_boards_polled = len(self.slugs)

        # Preload known postings and company displays in bulk (0.1s vs 53s of per-slug SQLite queries)
        self._known_postings = {}
        self._display_cache = {}
        self._new_displays = {}
        if self.db:
            try:
                if hasattr(self.db, "get_company_displays_for_source"):
                    self._display_cache = self.db.get_company_displays_for_source(self.name)
                with self.db._get_connection() as conn:
                    rows = conn.execute(
                        "SELECT id, posted_at, description FROM jobs WHERE id >= 'greenhouse:' AND id < 'greenhouse;' AND description IS NOT NULL AND length(description) > 50"
                    ).fetchall()
                    for r in rows:
                        self._known_postings[r["id"]] = (r["posted_at"] or "", r["description"] or "")
            except Exception as exc:
                logger.debug(f"[{self.name}] Preload error: {exc}")

        logger.info(
            f"[{self.name}] Scraping {len(self.slugs)} slugs (concurrency={self.concurrency}, timeout={self.timeout}s)..."
        )
        all_jobs: list[JobPosting] = []
        ok_count = 0
        failed_count = 0
        total_listed = 0
        total_details = 0

        limits = httpx.Limits(max_connections=self.concurrency + 10, max_keepalive_connections=self.concurrency)
        with httpx.Client(headers=self.headers, timeout=self.timeout, limits=limits) as client:
            with ThreadPoolExecutor(max_workers=self.concurrency) as executor:
                futures = {executor.submit(self._fetch_slug, client, slug, True): slug for slug in self.slugs}
                for f in as_completed(futures):
                    try:
                        jobs, listed, details = f.result()
                        ok_count += 1
                        total_listed += listed
                        total_details += details
                        all_jobs.extend(jobs)
                    except Exception:
                        failed_count += 1

        if self.db:
            try:
                if self._new_displays and hasattr(self.db, "save_company_displays_batch"):
                    self.db.save_company_displays_batch(self.name, self._new_displays)
                if hasattr(self.db, "record_board_polls_batch"):
                    self.db.record_board_polls_batch(self.name, self.slugs)
            except Exception as exc:
                logger.debug(f"[{self.name}] Batch save error: {exc}")

        self._known_postings = None
        self._display_cache = None
        self.last_postings_listed = total_listed
        self.last_details_fetched = total_details

        logger.info(
            f"[{self.name}] Finished: {len(all_jobs)} harvested (boards polled: {len(self.slugs)}, listed: {total_listed}, details fetched: {total_details}, OK: {ok_count}, Failed: {failed_count})"
        )
        return all_jobs
