"""
Liveness Verification Engine for munichScraper.
Performs fast, authoritative HTTP & ATS checks on candidate job postings to verify
whether positions are still live or have been closed / expired / filled.

Rules:
- Authoritative ATS APIs checked when available (Greenhouse, Lever, etc.).
- Direct apply_url or url checked with redirect following.
- Status 404, 410 -> Closed.
- Redirection to expired/career root/closed URL -> Closed.
- Page text containing 'no longer accepting applications', 'position closed', etc. -> Closed.
- Status 403/429/Cloudflare -> Inconclusive, role kept open to avoid false negatives.
- Automatically marks closed postings in SQLite (closed_at = NOW()).
"""

import logging
import re
import urllib.parse
from concurrent.futures import ThreadPoolExecutor, as_completed
from datetime import datetime, timezone
from typing import Optional, Tuple
import httpx
from database import Database

logger = logging.getLogger("LivenessEngine")

CLOSED_URL_PATTERNS = re.compile(
    r"(?:/jobs-closed|/expired|/not-found|/job-not-found|/careers/?$|/jobs/?$|\bclosed=true\b)",
    re.I,
)

CLOSED_CONTENT_MARKERS = [
    re.compile(r"this\s+job\s+(?:is\s+)?no\s+longer\s+(?:available|accepting\s+applications|active)", re.I),
    re.compile(r"this\s+position\s+(?:has\s+been|is)\s+(?:closed|filled)", re.I),
    re.compile(r"no\s+longer\s+accepting\s+applications", re.I),
    re.compile(r"this\s+posting\s+has\s+expired", re.I),
    re.compile(r"this\s+vacancy\s+is\s+closed", re.I),
    re.compile(r"dieses\s+stellenangebot\s+ist\s+nicht\s+mehr\s+verf[üu]gbar", re.I),
    re.compile(r"diese\s+stelle\s+ist\s+bereits\s+besetzt", re.I),
    re.compile(r"die\s+ausschreibung\s+wurde\s+beendet", re.I),
]


from typing import Optional, Tuple, Union


def check_single_job_liveness(
    client: httpx.Client,
    job: Union[dict, any],
) -> Tuple[str, bool, str]:
    """
    Check if a candidate job is still active.
    Returns (job_id, is_live, reason).
    """
    if isinstance(job, dict):
        jid = job.get("id", "")
        source = job.get("source") or ""
        apply_url = job.get("apply_url")
        url = job.get("url") or ""
        ats = job.get("ats")
        ats_slug = job.get("ats_slug")
    else:
        jid = getattr(job, "id", "")
        source = getattr(job, "source", "") or ""
        apply_url = getattr(job, "apply_url", None)
        url = getattr(job, "url", "") or ""
        ats = getattr(job, "ats", None)
        ats_slug = getattr(job, "ats_slug", None)

    # 1. Authoritative ATS API checks
    if source == "greenhouse" or ats == "greenhouse":
        # Extract posting id if present
        m = re.search(r":(\d+)$", jid) or re.search(r"/jobs/(\d+)", apply_url or url)
        slug = ats_slug
        if not slug and ":" in jid:
            parts = jid.split(":")
            if len(parts) == 3:
                slug = parts[1]
        if slug and m:
            api_url = f"https://boards-api.greenhouse.io/v1/boards/{slug}/jobs/{m.group(1)}"
            try:
                res = client.get(api_url)
                if res.status_code == 404:
                    return jid, False, "Greenhouse API 404 (Job removed from board)"
                if res.status_code == 200:
                    return jid, True, "Active on Greenhouse API"
            except Exception:
                pass

    if source == "lever" or ats == "lever":
        m = re.search(r":([a-f0-9-]+)$", jid) or re.search(r"/([a-f0-9-]{36})", apply_url or url)
        slug = ats_slug
        if not slug and ":" in jid:
            parts = jid.split(":")
            if len(parts) == 3:
                slug = parts[1]
        if slug and m:
            api_url = f"https://api.lever.co/v0/postings/{slug}/{m.group(1)}"
            try:
                res = client.get(api_url)
                if res.status_code == 404:
                    return jid, False, "Lever API 404 (Posting closed)"
                if res.status_code == 200:
                    return jid, True, "Active on Lever API"
            except Exception:
                pass

    # 2. General URL HTTP check
    target_url = apply_url if (apply_url and "linkedin.com" not in apply_url) else url
    if not target_url:
        return jid, True, "No URL to check"

    try:
        res = client.get(target_url)

        # Definite closed codes
        if res.status_code in {404, 410}:
            return jid, False, f"HTTP {res.status_code} Not Found"

        # Rate limits or Cloudflare challenge -> inconclusive, keep active
        if res.status_code in {401, 403, 429}:
            return jid, True, f"HTTP {res.status_code} (Inconclusive/Protected, kept active)"

        if res.status_code == 200:
            final_url = str(res.url)

            # Check if redirected to generic career landing page
            orig_path = urllib.parse.urlparse(target_url).path.strip("/")
            final_path = urllib.parse.urlparse(final_url).path.strip("/")

            if CLOSED_URL_PATTERNS.search(final_url):
                return jid, False, f"Redirected to expired/closed URL: {final_url}"

            # If original had a job path but was redirected to root career page
            if orig_path and not final_path and ("careers" in target_url or "jobs" in target_url):
                return jid, False, f"Redirected to domain root: {final_url}"

            # Check page body content for closed vacancy markers
            text_preview = res.text[:20000]
            for pat in CLOSED_CONTENT_MARKERS:
                if pat.search(text_preview):
                    return jid, False, f"Page contains closed notice: '{pat.pattern}'"

            return jid, True, "HTTP 200 OK (Content active)"

        return jid, True, f"HTTP {res.status_code} (Kept active)"

    except httpx.TimeoutException:
        # Timeout -> inconclusive, keep alive
        return jid, True, "Request Timeout (Kept active)"
    except Exception as exc:
        return jid, True, f"Error: {exc} (Kept active)"

def verify_job_liveness(job: Union[dict, any], client: Optional[httpx.Client] = None) -> Tuple[bool, str]:
    """
    Direct verification of a single job.
    Returns (is_live, reason).
    """
    if client is not None:
        _, is_live, reason = check_single_job_liveness(client, job)
        return is_live, reason

    headers = {
        "User-Agent": (
            "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
            "(KHTML, like Gecko) Chrome/128.0.0.0 Safari/537.36"
        ),
        "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8",
        "Accept-Language": "en-US,en;q=0.9,de;q=0.8",
    }
    with httpx.Client(headers=headers, timeout=8.0, follow_redirects=True) as new_client:
        _, is_live, reason = check_single_job_liveness(new_client, job)
        return is_live, reason


class LivenessEngine:
    def __init__(self, db: Database, concurrency: int = 15):
        self.db = db
        self.concurrency = concurrency

    def check_candidates(self, limit: Optional[int] = None) -> dict:
        """
        Verify all candidate jobs in SQLite that are currently considered open.
        Marks closed postings in the database.
        """
        with self.db._get_connection() as conn:
            query = """
                SELECT id, title, company, source, url, apply_url, ats, ats_slug
                FROM jobs
                WHERE gate_rejected IS NULL
                  AND score > 0
                  AND closed_at IS NULL
                ORDER BY score DESC, created_at DESC
            """
            if limit:
                query += f" LIMIT {int(limit)}"
            rows = conn.execute(query).fetchall()

        candidates = [dict(r) for r in rows]
        if not candidates:
            logger.info("[Liveness] No candidate jobs to check.")
            return {"total": 0, "live": 0, "closed": 0, "closed_jobs": []}

        logger.info(f"[Liveness] Checking liveness of {len(candidates)} candidate jobs (concurrency={self.concurrency})...")

        headers = {
            "User-Agent": (
                "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
                "(KHTML, like Gecko) Chrome/128.0.0.0 Safari/537.36"
            ),
            "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8",
            "Accept-Language": "en-US,en;q=0.9,de;q=0.8",
        }

        live_count = 0
        closed_count = 0
        closed_records = []

        now_iso = datetime.now(timezone.utc).isoformat()

        with httpx.Client(headers=headers, timeout=10.0, follow_redirects=True) as client:
            with ThreadPoolExecutor(max_workers=self.concurrency) as executor:
                futures = {
                    executor.submit(check_single_job_liveness, client, job): job
                    for job in candidates
                }
                for f in as_completed(futures):
                    job_meta = futures[f]
                    try:
                        jid, is_live, reason = f.result()
                        if not is_live:
                            closed_count += 1
                            closed_records.append((jid, job_meta.get("title"), job_meta.get("company"), reason))
                            logger.info(f"[Liveness] Marking CLOSED: {jid} | {job_meta.get('title')} ({reason})")
                            with self.db._get_connection() as conn:
                                conn.execute("UPDATE jobs SET closed_at = ? WHERE id = ?", (now_iso, jid))
                        else:
                            live_count += 1
                    except Exception as exc:
                        live_count += 1
                        logger.debug(f"[Liveness] Error checking job: {exc}")

        logger.info(f"[Liveness] Finished check: {live_count} live, {closed_count} closed out of {len(candidates)}.")
        return {
            "total": len(candidates),
            "live": live_count,
            "closed": closed_count,
            "closed_jobs": closed_records,
        }
