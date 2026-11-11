"""
Remote Job Boards Scrapers.
Scrapes Germany-eligible remote postings from:
1. Remotive (https://remotive.com/api/remote-jobs)
2. WeWorkRemotely RSS feeds
3. RemoteOK (https://remoteok.com/api)
Brief 14 B1: Removed HimalayasScraper and WorkingNomadsScraper.
"""

from datetime import datetime, timezone
import email.utils
import logging
import re
import time
import httpx

from intake_checks import find_employer_link, host_of, is_aggregator_host
from models import JobPosting
from scrapers.base import BaseScraper

logger = logging.getLogger(__name__)

EU_GERMANY_ELIGIBILITY_RE = re.compile(
    r"\b(germany|deutschland|dach|eu|europe|european\s+union|emea|worldwide|anywhere|global|all\s+countries|cet|gmt|utc)\b",
    re.I,
)

NON_EU_RESTRICTED_RE = re.compile(
    r"\b(us\s+only|usa\s+only|united\s+states\s+only|north\s+america\s+only|canada\s+only|uk\s+only|latin\s+america\s+only|latam\s+only|apac\s+only|asia\s+only|australia\s+only)\b",
    re.I,
)


def is_germany_remote_eligible(loc_text: str) -> bool:
    """Return True if loc_text explicitly allows Germany/EU or is open worldwide."""
    if not loc_text:
        return False
    t = loc_text.lower()
    if NON_EU_RESTRICTED_RE.search(t):
        if not re.search(r"\b(germany|deutschland|dach|eu|europe)\b", t):
            return False
    return bool(EU_GERMANY_ELIGIBILITY_RE.search(t))


class RemotiveScraper(BaseScraper):
    def __init__(self, db=None):
        super().__init__(name="remotive")
        self.db = db
        self.url = "https://remotive.com/api/remote-jobs"

    def scrape(self) -> list[JobPosting]:
        logger.info("[Remotive] Fetching remote jobs...")
        jobs: list[JobPosting] = []
        try:
            with httpx.Client(headers=self.headers, timeout=25.0) as client:
                res = client.get(self.url)
                if res.status_code != 200:
                    logger.warning(f"[Remotive] HTTP {res.status_code}")
                    return []
                data = res.json()
                raw_jobs = data.get("jobs", [])

            for item in raw_jobs:
                raw_loc = item.get("candidate_required_location", "") or ""
                if not is_germany_remote_eligible(raw_loc):
                    continue

                raw_id = str(item.get("id", "")).strip()
                if not raw_id:
                    continue

                title = item.get("title", "").strip()
                comp = item.get("company_name", "").strip() or "Remotive Company"
                job_url = item.get("url", "").strip()

                posted_at = None
                pub = item.get("publication_date")
                if pub:
                    try:
                        dt = datetime.fromisoformat(pub.replace("Z", "+00:00"))
                        posted_at = dt if dt.tzinfo else dt.replace(tzinfo=timezone.utc)
                    except Exception:
                        pass

                desc = item.get("description", "")
                emp_link = find_employer_link(desc)
                clean_desc = re.sub(r"<[^>]+>", " ", desc).strip()

                job = JobPosting(
                    id=f"remotive:{raw_id}",
                    title=title,
                    company=comp,
                    company_display=comp,
                    location="Remote (Germany eligible)",
                    url=job_url,
                    apply_url=emp_link,
                    description=clean_desc,
                    source=self.name,
                    posted_at=posted_at,
                    apply_kind="auto" if emp_link else "unknown",
                    apply_host=host_of(emp_link) if emp_link else "remotive.com",
                )
                jobs.append(job)

        except Exception as exc:
            logger.error(f"[Remotive] Scrape error: {exc}")

        return jobs


class WeWorkRemotelyScraper(BaseScraper):
    def __init__(self, db=None):
        super().__init__(name="weworkremotely")
        self.db = db
        self.feeds = [
            "https://weworkremotely.com/categories/remote-customer-support-jobs.rss",
            "https://weworkremotely.com/categories/remote-sales-and-marketing-jobs.rss",
            "https://weworkremotely.com/categories/remote-management-and-finance-jobs.rss",
        ]

    def scrape(self) -> list[JobPosting]:
        logger.info("[WeWorkRemotely] Fetching RSS feeds...")
        all_jobs: dict[str, JobPosting] = {}

        try:
            with httpx.Client(headers=self.headers, timeout=20.0) as client:
                for feed_url in self.feeds:
                    time.sleep(1.0)
                    res = client.get(feed_url)
                    if res.status_code != 200:
                        continue

                    items = re.findall(r"<item>(.*?)</item>", res.text, re.DOTALL)
                    for it in items:
                        reg_m = re.search(r"<region>(.*?)</region>", it)
                        region = reg_m.group(1).strip() if reg_m else ""
                        if not is_germany_remote_eligible(region):
                            continue

                        t_m = re.search(r"<title>(.*?)</title>", it)
                        l_m = re.search(r"<link>(.*?)</link>", it)
                        g_m = re.search(r"<guid[^>]*>(.*?)</guid>", it)
                        d_m = re.search(r"<pubDate>(.*?)</pubDate>", it)
                        desc_m = re.search(r"<description>(.*?)</description>", it, re.DOTALL)

                        if not (t_m and l_m):
                            continue

                        raw_title = t_m.group(1).strip()
                        link = l_m.group(1).strip()

                        company = "WWR Company"
                        title = raw_title
                        if ":" in raw_title:
                            parts = raw_title.split(":", 1)
                            company = parts[0].strip()
                            title = parts[1].strip()

                        guid_str = g_m.group(1).strip() if g_m else link
                        clean_id = guid_str.split("/remote-jobs/")[-1].strip() if "/remote-jobs/" in guid_str else self.generate_job_id(link, title, company)

                        if clean_id in all_jobs:
                            continue

                        posted_at = None
                        if d_m:
                            try:
                                dt = email.utils.parsedate_to_datetime(d_m.group(1))
                                posted_at = dt.astimezone(timezone.utc)
                            except Exception:
                                pass

                        clean_desc = ""
                        emp_link = None
                        if desc_m:
                            raw_html = desc_m.group(1)
                            emp_link = find_employer_link(raw_html)
                            clean_desc = re.sub(r"<[^>]+>", " ", raw_html).strip()

                        job = JobPosting(
                            id=f"weworkremotely:{clean_id}",
                            title=title,
                            company=company,
                            company_display=company,
                            location="Remote (Germany eligible)",
                            url=link,
                            apply_url=emp_link,
                            description=clean_desc,
                            source=self.name,
                            posted_at=posted_at,
                            apply_kind="auto" if emp_link else "unknown",
                            apply_host=host_of(emp_link) if emp_link else "weworkremotely.com",
                        )
                        all_jobs[clean_id] = job

        except Exception as exc:
            logger.error(f"[WeWorkRemotely] Scrape error: {exc}")

        return list(all_jobs.values())


class RemoteOkScraper(BaseScraper):
    def __init__(self, db=None):
        super().__init__(name="remoteok")
        self.db = db
        self.url = "https://remoteok.com/api"

    def scrape(self) -> list[JobPosting]:
        logger.info("[RemoteOK] Fetching remote jobs from API...")
        jobs: list[JobPosting] = []

        try:
            with httpx.Client(headers=self.headers, timeout=25.0) as client:
                res = client.get(self.url)
                if res.status_code != 200:
                    logger.warning(f"[RemoteOK] HTTP {res.status_code}")
                    return []
                raw_data = res.json()

            items = raw_data[1:] if len(raw_data) > 1 else []

            for item in items:
                loc_str = item.get("location", "") or ""
                if loc_str and not is_germany_remote_eligible(loc_str):
                    continue

                raw_id = str(item.get("id", "")).strip()
                if not raw_id:
                    continue

                title = item.get("position", "").strip()
                comp = item.get("company", "").strip() or "RemoteOK Employer"
                job_url = item.get("url", "").strip() or f"https://remoteok.com/remote-jobs/{raw_id}"
                apply_url = item.get("apply_url", "").strip() or job_url

                posted_at = None
                pub = item.get("date")
                if pub:
                    try:
                        dt = datetime.fromisoformat(pub.replace("Z", "+00:00"))
                        posted_at = dt if dt.tzinfo else dt.replace(tzinfo=timezone.utc)
                    except Exception:
                        pass

                desc = item.get("description", "")
                clean_desc = re.sub(r"<[^>]+>", " ", desc).strip()
                if apply_url and is_aggregator_host(host_of(apply_url)):
                    emp = find_employer_link(desc)
                    if emp:
                        apply_url = emp

                job = JobPosting(
                    id=f"remoteok:{raw_id}",
                    title=title,
                    company=comp,
                    company_display=comp,
                    location="Remote (Germany eligible)",
                    url=job_url,
                    apply_url=apply_url,
                    description=clean_desc,
                    source=self.name,
                    posted_at=posted_at,
                    apply_kind="auto",
                    apply_host="remoteok.com",
                )
                jobs.append(job)

        except Exception as exc:
            logger.error(f"[RemoteOK] Scrape error: {exc}")

        return jobs
