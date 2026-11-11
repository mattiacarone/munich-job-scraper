"""
Recruitee ATS Scraper.
Iterates recruitee slugs from data/ats_registry.json using ThreadPoolExecutor.
Brief 13 Section 2 optimizations:
- Skip parsing/storing descriptions for postings that fail preliminary location or role-rules
- Per-board cadence: boards with 0 location passes in 30d polled daily (24h), active polled every cycle
URL: https://{slug}.recruitee.com/api/offers/
Job ID: recruitee:{slug}:{postingId}
"""

import logging
from concurrent.futures import ThreadPoolExecutor, as_completed
from datetime import datetime
from typing import Optional
import httpx
from config import load_ats_registry
from models import JobPosting
from scrapers.base import BaseScraper

logger = logging.getLogger(__name__)


class RecruiteeScraper(BaseScraper):
    def __init__(self, db=None, matcher=None, concurrency: int = 12, timeout: float = 25.0):
        super().__init__(name="recruitee")
        self.db = db
        self.matcher = matcher
        self.concurrency = concurrency
        self.timeout = timeout
        self.slugs = self._load_slugs()
        self.last_postings_listed = 0
        self.last_details_fetched = 0

    def _load_slugs(self) -> list[str]:
        registry = load_ats_registry()
        return registry.get("recruitee", [])

    def _fetch_slug(self, client: httpx.Client, slug: str) -> tuple[list[JobPosting], int]:
        url = f"https://{slug}.recruitee.com/api/offers/"
        jobs: list[JobPosting] = []
        listed_count = 0
        try:
            res = client.get(url)
            if res.status_code != 200:
                return [], 0
            data = res.json()
            offers = data.get("offers", [])
            listed_count = len(offers)

            display_name = None
            if offers and offers[0].get("company_name"):
                display_name = offers[0].get("company_name")
            if not display_name:
                display_name = slug.replace("-", " ").replace("_", " ").title()

            if self.db:
                self.db.save_company_display(self.name, slug, display_name)

            for item in offers:
                posting_id = str(item.get("id", "")).strip()
                if not posting_id:
                    continue

                title = item.get("title", "").strip()
                city = item.get("city", "") or ""
                country = item.get("country", "") or ""
                loc_parts = [p for p in [city, country] if p]
                location = ", ".join(loc_parts)

                remote = item.get("remote", False)
                if remote and "remote" not in location.lower():
                    location = f"{location} (Remote)" if location else "Remote"

                url_val = item.get("careers_url") or f"https://{slug}.recruitee.com/o/{posting_id}"

                # Brief 13 Section 2 & Brief 14 A2: Preliminary check
                passes_preliminary = True
                if self.matcher:
                    ok_loc, _ = self.matcher.check_gate_location(location, title, "", url_val)
                    if not ok_loc:
                        if hasattr(self.matcher, "should_fetch_for_vague_location") and self.matcher.should_fetch_for_vague_location(location, title):
                            passes_preliminary = True
                        else:
                            passes_preliminary = False
                    elif self.matcher.role_rules:
                        ok_role, _ = self.matcher.check_gate_role_rules(title)
                        if not ok_role:
                            passes_preliminary = False

                desc = (item.get("description", "") or "") if passes_preliminary else ""

                posted_at: Optional[datetime] = None
                if item.get("created_at"):
                    try:
                        posted_at = datetime.fromisoformat(item["created_at"].replace("Z", "+00:00"))
                    except Exception:
                        pass

                job_key = f"recruitee:{slug}:{posting_id}"

                jobs.append(
                    JobPosting(
                        id=job_key,
                        title=title,
                        company=display_name,
                        company_display=display_name,
                        location=location or None,
                        url=url_val,
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
