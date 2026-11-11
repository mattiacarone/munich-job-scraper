"""
Jobicy Remote Tech Scraper.
URL: https://jobicy.com/api/v2/remote-jobs?count=50&geo=germany
Job ID: jobicy:{postingId}
"""

import logging
from datetime import datetime
from typing import Optional
import httpx
from models import JobPosting
from scrapers.base import BaseScraper

logger = logging.getLogger(__name__)


class JobicyScraper(BaseScraper):
    API_URL = "https://jobicy.com/api/v2/remote-jobs"

    def __init__(self):
        super().__init__(name="jobicy")

    def scrape(self) -> list[JobPosting]:
        jobs: list[JobPosting] = []
        logger.info(f"[{self.name}] Fetching remote Germany postings from Jobicy...")

        try:
            with httpx.Client(headers=self.headers, timeout=20.0) as client:
                params = {"count": 50, "geo": "germany"}
                response = client.get(self.API_URL, params=params)
                if response.status_code != 200:
                    logger.warning(f"[{self.name}] API returned status {response.status_code}")
                    return jobs

                data = response.json()
                postings = data.get("jobs", [])

                for item in postings:
                    posting_id = str(item.get("id", "")).strip()
                    title = item.get("jobTitle", "").strip()
                    company = item.get("companyName", "").strip()
                    url = item.get("url", "").strip()
                    geo = item.get("jobGeo", "Germany (Remote)").strip()
                    description = item.get("jobDescription", "") or ""

                    if not posting_id:
                        posting_id = self.generate_job_id(url, title, company)

                    job_key = f"jobicy:{posting_id}"
                    
                    posted_at: Optional[datetime] = None
                    if item.get("pubDate"):
                        try:
                            posted_at = datetime.fromisoformat(item["pubDate"])
                        except Exception:
                            pass

                    jobs.append(
                        JobPosting(
                            id=job_key,
                            title=title,
                            company=company,
                            company_display=company,
                            location=geo,
                            url=url,
                            description=description,
                            source=self.name,
                            posted_at=posted_at,
                        )
                    )

            logger.info(f"[{self.name}] Ingested {len(jobs)} postings from Jobicy.")
        except Exception as exc:
            logger.error(f"[{self.name}] Sync error: {exc}")

        return jobs

