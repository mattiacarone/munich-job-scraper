"""
Teamtailor ATS Scraper (Brief 6 & Brief 7b).
Iterates slugs in data/ats_registry.json / data/discovered_boards.json using Teamtailor careers board:
https://<slug>.teamtailor.com/jobs
Reads location from the job's JSON-LD (jobLocation.address) or the Locations block on the job page.
Never defaults location to Munich.
Job ID format: teamtailor:<slug>:<id>
Apply kind: 'auto' (direct ATS forms without account).
"""

from concurrent.futures import ThreadPoolExecutor, as_completed
from datetime import datetime
import html as html_lib
import json
import logging
import re
from typing import Any, Optional
import urllib.parse
import httpx

from apply_classifier import extract_host_from_url
from config import load_ats_registry
from models import JobPosting
from scrapers.base import BaseScraper

logger = logging.getLogger(__name__)


def parse_teamtailor_job_page(html_text: str) -> dict[str, Any]:
    """
    Parse a Teamtailor job posting HTML page to extract:
    - location (from JSON-LD jobLocation.address or HTML 'Locations' block)
    - location_source ('json-ld', 'job-page', or 'unknown')
    - title
    - description
    - posted_at
    Never defaults location to Munich.
    """
    result: dict[str, Any] = {
        "location": None,
        "location_source": "unknown",
        "title": None,
        "description": "",
        "posted_at": None,
    }
    if not html_text:
        return result

    # 1. Try JSON-LD (<script type="application/ld+json">)
    ld_blocks = re.findall(
        r'<script[^>]*type=["\']application/ld\+json["\'][^>]*>(.*?)</script>',
        html_text,
         flags=re.I | re.S,
    )
    for raw_block in ld_blocks:
        try:
            data = json.loads(raw_block.strip())
        except Exception:
            continue

        candidates = []
        if isinstance(data, list):
            candidates.extend(data)
        elif isinstance(data, dict):
            if isinstance(data.get("@graph"), list):
                candidates.extend(data["@graph"])
            else:
                candidates.append(data)

        for ld in candidates:
            if not isinstance(ld, dict):
                continue
            ld_type = str(ld.get("@type") or "")
            if "JobPosting" not in ld_type and "jobLocation" not in ld:
                continue

            if ld.get("title") and not result["title"]:
                result["title"] = html_lib.unescape(str(ld["title"])).strip()

            if ld.get("description") and not result["description"]:
                desc_unescaped = html_lib.unescape(str(ld["description"]))
                result["description"] = html_lib.unescape(desc_unescaped).strip()

            if ld.get("datePosted") and not result["posted_at"]:
                try:
                    dt_str = str(ld["datePosted"]).strip()
                    result["posted_at"] = datetime.fromisoformat(dt_str.replace("Z", "+00:00"))
                except Exception:
                    pass

            job_loc = ld.get("jobLocation")
            loc_strings: list[str] = []
            if isinstance(job_loc, dict):
                job_loc = [job_loc]
            if isinstance(job_loc, list):
                for place in job_loc:
                    if isinstance(place, str) and place.strip():
                        loc_strings.append(place.strip())
                    elif isinstance(place, dict):
                        addr = place.get("address")
                        if isinstance(addr, str) and addr.strip():
                            loc_strings.append(addr.strip())
                        elif isinstance(addr, dict):
                            parts: list[str] = []
                            for k in ("addressLocality", "addressRegion", "addressCountry"):
                                v = addr.get(k)
                                if isinstance(v, dict):
                                    v = v.get("name")
                                if v and str(v).strip():
                                    sv = str(v).strip()
                                    if sv.lower() not in [p.lower() for p in parts]:
                                        parts.append(sv)
                            if parts:
                                loc_strings.append(", ".join(parts))
                        elif place.get("name") and str(place["name"]).strip():
                            loc_strings.append(str(place["name"]).strip())

            loc_type = str(ld.get("jobLocationType") or "").upper()
            is_telecommute = "TELECOMMUTE" in loc_type or "REMOTE" in loc_type

            if loc_strings:
                deduped: list[str] = []
                for s in loc_strings:
                    if s.lower() not in [d.lower() for d in deduped]:
                        deduped.append(s)
                joined = "; ".join(deduped)
                if is_telecommute and "remote" not in joined.lower():
                    joined = f"{joined} (Remote)"
                result["location"] = joined
                result["location_source"] = "json-ld"
                return result
            elif is_telecommute:
                result["location"] = "Remote"
                result["location_source"] = "json-ld"
                return result

    # 2. Fallback: look for "Locations" / "Location" / "Standort" dt/dd or section block on the job page
    loc_block_match = re.search(
        r"<dt[^>]*>\s*(?:Locations?|Standorte?|Lieu(?:x)?|Plats(?:er)?|Locatie(?:s)?)\s*</dt>\s*<dd[^>]*>(.*?)</dd>",
        html_text,
        flags=re.I | re.S,
    )
    if not loc_block_match:
        loc_block_match = re.search(
            r"(?:Locations?|Standorte?)\s*</(?:span|div|h[1-6]|dt)>\s*<(?:span|div|dd|p)[^>]*>(.*?)</(?:span|div|dd|p)>",
            html_text,
            flags=re.I | re.S,
        )
    if loc_block_match:
        raw_loc_html = loc_block_match.group(1)
        clean_loc = re.sub(r"<[^>]+>", " ", raw_loc_html)
        clean_loc = html_lib.unescape(re.sub(r"\s+", " ", clean_loc)).strip(" ,;·•-")
        if clean_loc:
            result["location"] = clean_loc
            result["location_source"] = "job-page"

    return result


class TeamtailorScraper(BaseScraper):
    def __init__(self, db=None, concurrency: int = 10, timeout: float = 20.0):
        super().__init__(name="teamtailor")
        self.db = db
        self.concurrency = concurrency
        self.timeout = timeout
        self.slugs = self._load_slugs()

    def _load_slugs(self) -> list[str]:
        registry = load_ats_registry()
        return registry.get("teamtailor", [])

    def _fetch_slug(self, client: httpx.Client, slug: str) -> list[JobPosting]:
        url = f"https://{slug}.teamtailor.com/jobs"
        jobs: list[JobPosting] = []
        try:
            res = client.get(url)
            if res.status_code != 200:
                return []

            display_name = slug.replace("-", " ").replace("_", " ").title()
            if self.db:
                self.db.save_company_display(self.name, slug, display_name)

            final_base_url = str(getattr(res, "url", None) or url)
            base_host = extract_host_from_url(final_base_url) or f"{slug}.teamtailor.com"

            html = res.text
            # Look for job links: href=".../jobs/12345-job-title"
            matches = re.findall(r'href="([^"]*/jobs/(\d+)(?:-([^"]*))?)"', html)
            seen_ids = set()

            for full_href, num_id, title_slug in matches:
                if num_id in seen_ids:
                    continue
                seen_ids.add(num_id)

                title = (title_slug or "").replace("-", " ").title()
                if not title:
                    title = f"Role {num_id}"

                job_url = full_href
                if job_url.startswith("/"):
                    job_url = urllib.parse.urljoin(final_base_url, job_url)

                loc: Optional[str] = None
                loc_source: str = "unknown"
                desc = ""
                posted_at: Optional[datetime] = None

                # Fetch the job detail page to parse JSON-LD jobLocation.address and description
                try:
                    job_res = client.get(job_url)
                    if job_res.status_code == 200:
                        parsed = parse_teamtailor_job_page(job_res.text)
                        if parsed.get("title"):
                            title = parsed["title"]
                        if parsed.get("location"):
                            loc = parsed["location"]
                            loc_source = parsed.get("location_source") or "json-ld"
                        if parsed.get("description"):
                            desc = parsed["description"]
                        if parsed.get("posted_at"):
                            posted_at = parsed["posted_at"]
                except Exception as exc:
                    logger.debug(f"[Teamtailor] Error fetching job page '{job_url}': {exc}")

                job_host = extract_host_from_url(job_url) or base_host

                jobs.append(
                    JobPosting(
                        id=f"teamtailor:{slug}:{num_id}",
                        title=title,
                        company=display_name,
                        company_display=display_name,
                        location=loc,
                        location_source=loc_source,
                        url=job_url,
                        description=desc,
                        source=self.name,
                        posted_at=posted_at,
                        apply_kind="auto",
                        apply_host=job_host,
                    )
                )

        except Exception as exc:
            logger.debug(f"[Teamtailor] Error fetching slug '{slug}': {exc}")

        return jobs

    def scrape(self) -> list[JobPosting]:
        self.slugs = self._load_slugs()
        if not self.slugs:
            logger.info("[Teamtailor] No slugs registered.")
            return []

        logger.info(f"[Teamtailor] Polling {len(self.slugs)} registered company boards...")
        all_jobs: list[JobPosting] = []

        with httpx.Client(headers=self.headers, timeout=self.timeout, follow_redirects=True) as client:
            with ThreadPoolExecutor(max_workers=self.concurrency) as executor:
                futures = {executor.submit(self._fetch_slug, client, slug): slug for slug in self.slugs}
                for f in as_completed(futures):
                    slug = futures[f]
                    try:
                        res = f.result()
                        all_jobs.extend(res)
                    except Exception as exc:
                        logger.error(f"[Teamtailor] Unexpected failure processing '{slug}': {exc}")

        logger.info(f"[Teamtailor] Ingestion complete. Retrieved {len(all_jobs)} total postings.")
        return all_jobs
