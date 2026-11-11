"""
Personio ATS Scraper.
Iterates personio slugs from data/ats_registry.json using ThreadPoolExecutor.
Brief 13 Section 2 optimizations:
- Skip parsing/storing descriptions for postings that fail preliminary location or role-rules
- Per-board cadence: boards with 0 location passes in 30d polled daily (24h), active polled every cycle
URL: https://{slug}.jobs.personio.de/xml (fallback https://{slug}.jobs.personio.com/xml)
Job ID: personio:{slug}:{postingId}
"""

import logging
import xml.etree.ElementTree as ET
from concurrent.futures import ThreadPoolExecutor, as_completed
from datetime import datetime
from typing import Optional
import httpx
from config import load_ats_registry
from models import JobPosting
from scrapers.base import BaseScraper

logger = logging.getLogger(__name__)


class PersonioScraper(BaseScraper):
    def __init__(self, db=None, matcher=None, concurrency: int = 12, timeout: float = 25.0):
        super().__init__(name="personio")
        self.db = db
        self.matcher = matcher
        self.concurrency = concurrency
        self.timeout = timeout
        self.slugs = self._load_slugs()
        self.last_postings_listed = 0
        self.last_details_fetched = 0

    def _load_slugs(self) -> list[str]:
        registry = load_ats_registry()
        return registry.get("personio", [])

    def _fetch_slug(self, client: httpx.Client, slug: str) -> tuple[list[JobPosting], int]:
        url_de = f"https://{slug}.jobs.personio.de/xml"
        url_com = f"https://{slug}.jobs.personio.com/xml"
        jobs: list[JobPosting] = []
        listed_count = 0

        resp_text = None
        used_domain = "de"
        try:
            r = client.get(url_de)
            if r.status_code == 200:
                resp_text = r.text
            else:
                r2 = client.get(url_com)
                if r2.status_code == 200:
                    resp_text = r2.text
                    used_domain = "com"
        except Exception:
            try:
                r2 = client.get(url_com)
                if r2.status_code == 200:
                    resp_text = r2.text
                    used_domain = "com"
            except Exception:
                return [], 0

        if not resp_text:
            return [], 0

        try:
            root = ET.fromstring(resp_text)
            positions = root.findall(".//position")
            listed_count = len(positions)

            display_name = None
            company_elem = root.find(".//company") or root.find(".//employer")
            if company_elem is not None and company_elem.text:
                display_name = company_elem.text.strip()
            if not display_name:
                display_name = slug.replace("-", " ").replace("_", " ").title()

            if self.db:
                self.db.save_company_display(self.name, slug, display_name)

            for pos in positions:
                id_elem = pos.find("id")
                name_elem = pos.find("name")
                if id_elem is None or not id_elem.text or name_elem is None or not name_elem.text:
                    continue

                posting_id = id_elem.text.strip()
                title = name_elem.text.strip()

                office_elem = pos.find("office")
                location = office_elem.text.strip() if (office_elem is not None and office_elem.text and office_elem.text.strip()) else None

                add_office = pos.find("additionalOffices")
                if add_office is not None and add_office.text and add_office.text.strip():
                    location = f"{location}; {add_office.text.strip()}" if location else add_office.text.strip()

                created_elem = pos.find("createdAt")
                posted_at: Optional[datetime] = None
                if created_elem is not None and created_elem.text:
                    try:
                        posted_at = datetime.fromisoformat(created_elem.text.strip().replace("Z", "+00:00"))
                    except Exception:
                        pass

                job_url = f"https://{slug}.jobs.personio.{used_domain}/job/{posting_id}"

                # Brief 13 Section 2 & Brief 14 A2: Preliminary check
                passes_preliminary = True
                if self.matcher:
                    ok_loc, _ = self.matcher.check_gate_location(location, title, "", job_url)
                    if not ok_loc:
                        if hasattr(self.matcher, "should_fetch_for_vague_location") and self.matcher.should_fetch_for_vague_location(location, title):
                            passes_preliminary = True
                        else:
                            passes_preliminary = False
                    elif self.matcher.role_rules:
                        ok_role, _ = self.matcher.check_gate_role_rules(title)
                        if not ok_role:
                            passes_preliminary = False

                full_desc = ""
                if passes_preliminary:
                    desc_parts = []
                    job_desc_parent = pos.find("jobDescriptions")
                    if job_desc_parent is not None:
                        for jd in job_desc_parent.findall("jobDescription"):
                            jd_name = jd.find("name")
                            jd_val = jd.find("value")
                            h = jd_name.text.strip() if (jd_name is not None and jd_name.text) else ""
                            v = jd_val.text.strip() if (jd_val is not None and jd_val.text) else ""
                            if h or v:
                                desc_parts.append(f"{h}\n{v}")
                    full_desc = "\n\n".join(desc_parts)

                job_key = f"personio:{slug}:{posting_id}"

                jobs.append(
                    JobPosting(
                        id=job_key,
                        title=title,
                        company=display_name,
                        company_display=display_name,
                        location=location,
                        url=job_url,
                        description=full_desc,
                        source=self.name,
                        posted_at=posted_at,
                    )
                )

            if self.db and hasattr(self.db, "record_board_poll"):
                self.db.record_board_poll(self.name, slug)

        except Exception as exc:
            logger.debug(f"[{self.name}] Error parsing XML for slug {slug}: {exc}")

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
