"""
Bundesagentur für Arbeit Scraper (Brief 11 Section 2).
Uses the public Arbeitsagentur Jobsuche API:
  Search: GET https://rest.arbeitsagentur.de/jobboerse/jobsuche-service/pc/v6/jobs
  Detail: GET https://rest.arbeitsagentur.de/jobboerse/jobsuche-service/pc/v4/jobdetails/{base64(referenznummer)}
Headers: X-API-Key: jobboerse-jobsuche, User-Agent: Jobsuche/2.9.2 (Android 14; Pixel 8 Pro)
Filters: wo=München, umkreis=30, size=50.
Keeps only postings whose text is English (language detection on title + first 500 chars).
Marked apply_kind = 'account_portal'.
"""

import base64
from datetime import datetime, timezone
import logging
import re
from typing import Optional
import httpx

from matcher import ENG_FUNCTION_WORDS, GER_FUNCTION_WORDS
from intake_checks import strip_tracking
from models import JobPosting
from scrapers.base import BaseScraper
from scrapers.family_titles import FAMILY_TITLES

logger = logging.getLogger(__name__)

BEST_CONVERTING_TITLES = [
    "Solutions Architect",
    "Technical Program Manager",
    "Solutions Engineer",
    "Customer Success Engineer",
    "Partner Manager",
    "Implementation Specialist",
    "Business Systems Analyst",
    "Technical Account Manager",
    "AI Solutions",
    "Forward Deployed",
]


class ArbeitsagenturScraper(BaseScraper):
    # 5 Nov 2026: all role-family titles (was 10), 100 per title, last 14 days, 50 km; detail calls only for unseen postings
    def __init__(self, db=None, max_per_title: int = 100, titles: Optional[list[str]] = None, max_details: int = 600):
        super().__init__(name="arbeitsagentur")
        self.db = db
        self.max_per_title = max_per_title
        self.titles = titles or list(dict.fromkeys(FAMILY_TITLES + BEST_CONVERTING_TITLES))
        self.max_details = max_details
        self.search_url = "https://rest.arbeitsagentur.de/jobboerse/jobsuche-service/pc/v6/jobs"
        self.detail_url_base = "https://rest.arbeitsagentur.de/jobboerse/jobsuche-service/pc/v4/jobdetails"
        self.headers = {
            "X-API-Key": "jobboerse-jobsuche",
            "User-Agent": "Jobsuche/2.9.2 (Android 14; Pixel 8 Pro)",
            "Accept": "application/json",
        }

    def _is_english(self, text: str) -> bool:
        """Language detection on title + excerpt: must be English, not German."""
        if not text:
            return False
        t_low = text.lower()
        if any(m in t_low for m in ["gesucht", "wir suchen", "aufgaben:", "anforderungen:", "ihr profil", "ihre aufgaben", "unser angebot", "festanstellung"]):
            return False
        words = re.findall(r"[a-zäöüß]+", t_low)
        ger_cnt = sum(1 for w in words if w in GER_FUNCTION_WORDS)
        eng_cnt = sum(1 for w in words if w in ENG_FUNCTION_WORDS)
        if ger_cnt > 3 and ger_cnt >= eng_cnt:
            return False
        return True

    def scrape(self) -> list[JobPosting]:
        logger.info(f"[Arbeitsagentur] Querying Jobsuche API across {len(self.titles)} target titles for Munich...")
        postings: list[JobPosting] = []
        seen_refs: set[str] = set()
        details_done = 0

        with httpx.Client(timeout=15.0, headers=self.headers) as client:
            for title_query in self.titles:
                params = {
                    "wo": "München",
                    "umkreis": 50,
                    "size": min(self.max_per_title, 100),
                    "was": title_query,
                    "veroeffentlichtseit": 14,
                }
                try:
                    resp = client.get(self.search_url, params=params)
                    if resp.status_code != 200:
                        logger.warning(f"[Arbeitsagentur] HTTP {resp.status_code} for query '{title_query}'")
                        continue
                    data = resp.json()
                except Exception as exc:
                    logger.warning(f"[Arbeitsagentur] Search request failed for '{title_query}': {exc}")
                    continue

                items = data.get("ergebnisliste", []) or []
                # skip postings already in the database (they were evaluated before; saves one detail call each)
                if self.db is not None and items:
                    try:
                        unseen = self.db.filter_unseen_ids([f"arbeitsagentur:{i.get('referenznummer')}" for i in items if i.get("referenznummer")])
                        items = [i for i in items if f"arbeitsagentur:{i.get('referenznummer')}" in unseen]
                    except Exception:
                        pass
                for item in items:
                    if details_done >= self.max_details:
                        break
                    refnr = item.get("referenznummer")
                    if not refnr or refnr in seen_refs:
                        continue
                    seen_refs.add(refnr)

                    job_title = item.get("stellenangebotsTitel") or title_query
                    company = item.get("firma") or "Unknown Employer"

                    # Location
                    locs = item.get("stellenlokationen", [])
                    loc_str = "Munich, Germany"
                    if locs and isinstance(locs[0], dict):
                        addr = locs[0].get("adresse", {})
                        ort = addr.get("ort") or "München"
                        plz = addr.get("plz") or ""
                        loc_str = f"{plz} {ort}, Germany".strip()

                    # Fetch detail for full description
                    b64_ref = base64.b64encode(refnr.encode("utf-8")).decode("utf-8")
                    det_url = f"{self.detail_url_base}/{b64_ref}"
                    description = ""
                    posted_at = None
                    employer_url = ""

                    details_done += 1
                    try:
                        r_det = client.get(det_url)
                        if r_det.status_code == 200:
                            det_data = r_det.json()
                            description = det_data.get("stellenangebotsBeschreibung", "")
                            # Brief 12 section 5: the BA detail names the employer's own page in externeURL
                            employer_url = strip_tracking((det_data.get("externeURL") or item.get("externeURL") or "").strip())
                            p_str = det_data.get("modifikationsTimestamp") or det_data.get("eintrittsdatum")
                            if p_str:
                                try:
                                    posted_at = datetime.fromisoformat(p_str[:19]).replace(tzinfo=timezone.utc)
                                except Exception:
                                    pass
                    except Exception as exc:
                        logger.debug(f"[Arbeitsagentur] Detail fetch failed for {refnr}: {exc}")

                    # Brief 11 Section 2: Keep only postings whose text is English (title + first 500 chars)
                    sample_text = f"{job_title}\n{description[:500]}"
                    if not self._is_english(sample_text):
                        continue

                    url = f"https://www.arbeitsagentur.de/jobsuche/jobdetail/{refnr}"
                    # Only an employer-owned link counts; otherwise the BA portal (image captcha) is the only
                    # apply path and the matcher rejects the posting with gate 'no-employer-link'.
                    apply_link = employer_url or url
                    postings.append(
                        JobPosting(
                            id=f"arbeitsagentur:{refnr}",
                            title=job_title,
                            company=company,
                            company_display=company,
                            location=loc_str,
                            location_source="source",
                            location_scope="munich",
                            url=url,
                            apply_url=apply_link,
                            apply_kind="account_portal",
                            apply_route="arbeitsagentur",
                            apply_host="rest.arbeitsagentur.de",
                            needs_account=1,
                            description=description,
                            source="arbeitsagentur",
                            posted_at=posted_at,
                        )
                    )

        logger.info(f"[Arbeitsagentur] Collected {len(postings)} English postings for Munich.")
        return postings
