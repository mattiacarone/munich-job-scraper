"""
English-First Job Boards Scrapers.
- EnglishJobsDeScraper (englishjobs.de)
- MunichStartupScraper (munich-startup.de)
Brief 14 B1: Removed GermanTechJobsScraper, EnglishJobsInGermanyScraper, WellfoundScraper.
"""

from datetime import datetime, timezone
import json
import logging
from pathlib import Path
import re
import time
import urllib.parse
from bs4 import BeautifulSoup
import httpx

from apply_classifier import classify_apply_kind
from models import JobPosting
from scrapers.base import BaseScraper

logger = logging.getLogger(__name__)
DATA_DIR = Path(__file__).resolve().parent.parent / "data"


def record_discovered_board(ats_name: str, slug: str) -> None:
    """Record verified or candidate ATS board slug in data/discovered_boards.json."""
    if not ats_name or not slug:
        return
    disc_file = DATA_DIR / "discovered_boards.json"
    DATA_DIR.mkdir(parents=True, exist_ok=True)
    try:
        data = {}
        if disc_file.exists():
            with open(disc_file, "r", encoding="utf-8") as f:
                data = json.load(f)
        current = set(data.get(ats_name, []))
        if slug not in current:
            current.add(slug)
            data[ats_name] = sorted(list(current))
            with open(disc_file, "w", encoding="utf-8") as f:
                json.dump(data, f, indent=2)
            logger.info(f"[EnglishBoards] Discovered and saved new {ats_name} slug: '{slug}'")
    except Exception as exc:
        logger.debug(f"[EnglishBoards] Could not save discovered board: {exc}")


class EnglishJobsDeScraper(BaseScraper):
    def __init__(self, db=None, max_pages: int = 4):
        super().__init__(name="englishjobsde")
        self.db = db
        self.max_pages = max_pages
        self.base_url = "https://englishjobs.de/in/munich"

    def scrape(self) -> list[JobPosting]:
        logger.info(f"[{self.name}] Fetching English-speaking Munich jobs from englishjobs.de...")
        jobs: list[JobPosting] = []
        seen_ids = set()

        try:
            with httpx.Client(headers=self.headers, timeout=20.0, follow_redirects=True) as client:
                for page in range(1, self.max_pages + 1):
                    url = f"{self.base_url}?p={page}" if page > 1 else self.base_url
                    time.sleep(1.0)
                    res = client.get(url)
                    if res.status_code != 200:
                        logger.warning(f"[{self.name}] HTTP {res.status_code} on {url}")
                        break

                    soup = BeautifulSoup(res.text, "html.parser")
                    clickouts = soup.find_all("a", href=lambda h: h and "/clickout/" in h)
                    if not clickouts:
                        break

                    for a in clickouts:
                        title = a.get_text(strip=True)
                        if not title:
                            continue

                        clickout_href = a["href"]
                        m_id = re.search(r"/clickout/([a-f0-9]+)", clickout_href)
                        jid = m_id.group(1) if m_id else str(hash(clickout_href))
                        if jid in seen_ids:
                            continue
                        seen_ids.add(jid)

                        full_apply_url = urllib.parse.urljoin("https://englishjobs.de", clickout_href)

                        container = a.find_parent("div")
                        c_text = container.get_text(" | ", strip=True) if container else ""
                        parts = [p.strip() for p in c_text.split(" | ") if p.strip()]

                        comp = "Unknown Company"
                        loc = "Munich, Germany"
                        if len(parts) >= 2 and parts[0] == title:
                            comp = parts[1]
                            if len(parts) >= 3 and any(w in parts[2].lower() for w in ("munich", "münchen", "remote", "germany", "haidhausen", "schwabing")):
                                loc = parts[2]
                                if "munich" not in loc.lower() and "münchen" not in loc.lower() and "remote" not in loc.lower():
                                    loc = f"{loc}, Munich, Germany"

                        desc_text = c_text
                        kind, host = classify_apply_kind(apply_url=full_apply_url, source=self.name)

                        job = JobPosting(
                            id=f"englishjobsde:{jid}",
                            title=title,
                            company=comp,
                            company_display=comp,
                            location=loc,
                            url=full_apply_url,
                            apply_url=full_apply_url,
                            apply_kind=kind,
                            apply_host=host,
                            description=desc_text,
                            source=self.name,
                            posted_at=datetime.now(timezone.utc),
                        )
                        jobs.append(job)

        except Exception as exc:
            logger.error(f"[{self.name}] Scrape error: {exc}")

        return jobs


class MunichStartupScraper(BaseScraper):
    def __init__(self, db=None, max_items: int = 30):
        super().__init__(name="munichstartup")
        self.db = db
        self.max_items = max_items
        self.list_url = "https://www.munich-startup.de/jobs/stellenangebote"

    def scrape(self) -> list[JobPosting]:
        logger.info(f"[{self.name}] Fetching Munich startups from munich-startup.de...")
        jobs: list[JobPosting] = []
        seen_ids = set()

        try:
            with httpx.Client(headers=self.headers, timeout=20.0, follow_redirects=True) as client:
                res = client.get(self.list_url)
                if res.status_code != 200:
                    logger.warning(f"[{self.name}] HTTP {res.status_code} on {self.list_url}")
                    return []

                soup = BeautifulSoup(res.text, "html.parser")
                anzeige_links = soup.find_all("a", href=lambda h: h and "/jobs/anzeige/" in h)

                for a in anzeige_links:
                    if len(jobs) >= self.max_items:
                        break

                    href = a["href"]
                    m = re.search(r"/anzeige/(\d+)", href)
                    if not m:
                        continue
                    jid = m.group(1)
                    if jid in seen_ids:
                        continue
                    seen_ids.add(jid)

                    detail_url = urllib.parse.urljoin("https://www.munich-startup.de", href)
                    card_text = a.get_text(" ", strip=True)

                    time.sleep(0.5)
                    try:
                        d_res = client.get(detail_url)
                        if d_res.status_code != 200:
                            continue
                        d_soup = BeautifulSoup(d_res.text, "html.parser")
                        h1 = d_soup.find("h1")
                        title = h1.get_text(strip=True) if h1 else card_text[:60]

                        comp = "Munich Startup"
                        comp_elem = d_soup.select_one(".c-job-header__company, .job-company, h2, .company")
                        if comp_elem:
                            comp = comp_elem.get_text(strip=True)
                        elif card_text:
                            m_comp = re.match(r"^([A-ZaZa-z0-9\s\.\-&]+(?:GmbH|AG|SE|UG|Inc\.?))\b", card_text, re.I)
                            if m_comp:
                                comp = m_comp.group(1).strip()

                        apply_url = None
                        for lk in d_soup.find_all("a", href=True):
                            lkh = lk["href"]
                            lkt = lk.get_text(strip=True).lower()
                            if any(k in lkt for k in ["online bewerben", "apply", "hier bewerben", "karriere"]) or (
                                lkh.startswith("http") and "munich-startup.de" not in lkh
                            ):
                                apply_url = lkh
                                break
                            elif lkh.startswith("mailto:") and not apply_url:
                                apply_url = lkh

                        desc = d_soup.get_text(" ", strip=True)
                        kind, host = classify_apply_kind(apply_url=apply_url or detail_url, source=self.name)

                        if apply_url:
                            for ats in ("greenhouse", "personio", "ashby", "lever", "workable", "recruitee", "smartrecruiters", "teamtailor"):
                                if ats in apply_url.lower():
                                    slug_cand = comp.lower().replace(" ", "-")
                                    record_discovered_board(ats, slug_cand)

                        job = JobPosting(
                            id=f"munichstartup:{jid}",
                            title=title,
                            company=comp,
                            company_display=comp,
                            location="Munich, Germany",
                            url=detail_url,
                            apply_url=apply_url or detail_url,
                            apply_kind=kind,
                            apply_host=host,
                            description=desc[:5000],
                            source=self.name,
                            is_startup=True,
                            posted_at=datetime.now(timezone.utc),
                        )
                        jobs.append(job)

                    except Exception as det_exc:
                        logger.debug(f"[{self.name}] Error fetching detail {detail_url}: {det_exc}")
                        continue

        except Exception as exc:
            logger.error(f"[{self.name}] Scrape error: {exc}")

        return jobs
