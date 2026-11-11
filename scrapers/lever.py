"""
Lever ATS Scraper.
Iterates lever slugs from data/ats_registry.json using ThreadPoolExecutor.
Brief 13 Section 2 optimizations:
- Skip parsing/storing descriptions for postings that fail preliminary location or role-rules
- Per-board cadence: boards with 0 location passes in 30d polled daily (24h), active polled every cycle
URL: https://api.lever.co/v0/postings/{slug}?mode=json
Job ID: lever:{slug}:{postingId}
"""

import logging
from concurrent.futures import ThreadPoolExecutor, as_completed
from datetime import datetime, timezone
from typing import Optional
import httpx
from config import load_ats_registry
from models import JobPosting
from scrapers.base import BaseScraper

logger = logging.getLogger(__name__)


class LeverScraper(BaseScraper):
    def __init__(self, db=None, matcher=None, concurrency: int = 12, timeout: float = 25.0):
        super().__init__(name="lever")
        self.db = db
        self.matcher = matcher
        self.concurrency = concurrency
        self.timeout = timeout
        self.slugs = self._load_slugs()
        self.last_postings_listed = 0
        self.last_details_fetched = 0

    def _load_slugs(self) -> list[str]:
        registry = load_ats_registry()
        return registry.get("lever", [])

    def _fetch_slug(self, client: httpx.Client, slug: str) -> tuple[list[JobPosting], int]:
        url = f"https://api.lever.co/v0/postings/{slug}?mode=json"
        jobs: list[JobPosting] = []
        listed_count = 0
        try:
            res = client.get(url)
            if res.status_code == 404:
                if self.db and hasattr(self.db, "record_board_poll"):
                    self.db.record_board_poll(self.name, slug)
                return [], 0
            if res.status_code != 200:
                return [], 0
            raw_postings = res.json()
            if not isinstance(raw_postings, list):
                return [], 0
            listed_count = len(raw_postings)

            display_name = slug.replace("-", " ").replace("_", " ").title()
            if self.db:
                self.db.save_company_display(self.name, slug, display_name)

            for item in raw_postings:
                posting_id = str(item.get("id", "")).strip()
                if not posting_id:
                    continue

                title = item.get("text", "").strip()
                categories = item.get("categories", {}) or {}
                location = categories.get("location", "") or ""
                all_locs = categories.get("allLocations", []) or []
                if all_locs:
                    location = "; ".join(all_locs)

                workplace = item.get("workplaceType", "") or ""
                if workplace.lower() == "remote" and "remote" not in location.lower():
                    location = f"{location} (Remote)" if location else "Remote"

                created_at_ms = item.get("createdAt")
                posted_at: Optional[datetime] = None
                if created_at_ms:
                    try:
                        posted_at = datetime.fromtimestamp(created_at_ms / 1000.0, tz=timezone.utc)
                    except Exception:
                        pass

                hosted_url = item.get("hostedUrl") or f"https://jobs.lever.co/{slug}/{posting_id}"

                # Brief 13 Section 2 & Brief 14 A2: Preliminary check
                passes_preliminary = True
                if self.matcher:
                    ok_loc, _ = self.matcher.check_gate_location(location, title, "", hosted_url)
                    if not ok_loc:
                        if hasattr(self.matcher, "should_fetch_for_vague_location") and self.matcher.should_fetch_for_vague_location(location, title):
                            passes_preliminary = True
                        else:
                            passes_preliminary = False
                    elif self.matcher.role_rules:
                        ok_role, _ = self.matcher.check_gate_role_rules(title)
                        if not ok_role:
                            passes_preliminary = False

                desc = ""
                if passes_preliminary:
                    desc = item.get("descriptionPlain", "") or ""
                    lists = item.get("lists", []) or []
                    for lst in lists:
                        lst_text = lst.get("text", "")
                        content = lst.get("content", "")
                        if lst_text or content:
                            desc += f"\n\n{lst_text}\n{content}"

                job_key = f"lever:{slug}:{posting_id}"

                jobs.append(
                    JobPosting(
                        id=job_key,
                        title=title,
                        company=display_name,
                        company_display=display_name,
                        location=location or None,
                        url=hosted_url,
                        description=desc,
                        source=self.name,
                        posted_at=posted_at,
                    )
                )

            if self.db and hasattr(self.db, "record_board_poll"):
                self.db.record_board_poll(self.name, slug)

        except Exception as exc:
            logger.debug(f"[{self.name}] Error fetching slug {slug}: {exc}")

        return jobs, listed_count

    def scrape(self) -> list[JobPosting]:
        all_slugs = self._load_slugs()

        # Brief 13 Section 2: Per-board cadence
        if self.db and hasattr(self.db, "filter_slugs_by_cadence"):
            self.slugs, active_cnt, daily_cnt = self.db.filter_slugs_by_cadence(self.name, all_slugs)
            logger.info(
                f"[{self.name}] Board cadence: {active_cnt} active (every cycle), {daily_cnt} inactive (daily). {len(self.slugs)} due this cycle."
            )
        else:
            self.slugs = all_slugs

        logger.info(
            f"[{self.name}] Scraping {len(self.slugs)} slugs (concurrency={self.concurrency}, timeout={self.timeout}s)..."
        )
        all_jobs: list[JobPosting] = []
        ok_count = 0
        failed_count = 0
        total_listed = 0

        with httpx.Client(headers=self.headers, timeout=self.timeout) as client:
            with ThreadPoolExecutor(max_workers=self.concurrency) as executor:
                futures = {executor.submit(self._fetch_slug, client, slug): slug for slug in self.slugs}
                for f in as_completed(futures):
                    slug = futures[f]
                    try:
                        jobs, listed = f.result()
                        ok_count += 1
                        total_listed += listed
                        all_jobs.extend(jobs)
                    except Exception:
                        failed_count += 1

        self.last_postings_listed = total_listed
        self.last_details_fetched = 0

        logger.info(
            f"[{self.name}] Finished: {len(all_jobs)} harvested (listed: {total_listed}, OK: {ok_count}, Failed: {failed_count})"
        )
        return all_jobs
