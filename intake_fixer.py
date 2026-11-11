"""
Intake fixer (Brief 12 sections 5 and 7).

After the matcher rejects a posting with `no-description` or `no-employer-link`, try once to repair it
from public pages (the posting's own page; no login, no captcha handling), then the caller
re-evaluates. A per-cycle budget keeps slow sites from stalling the cycle.
"""

from __future__ import annotations

import logging
from typing import Callable, Optional

import httpx

from apply_classifier import classify_apply_kind, extract_host_from_url
from intake_checks import (
    AGGREGATOR_SOURCES,
    MIN_DESCRIPTION_CHARS,
    find_employer_link,
    fetch_description,
    host_of,
    html_to_text,
    is_aggregator_host,
    resolve_employer_link_via_registry,
)

logger = logging.getLogger(__name__)

FIXABLE_GATES = {"no-description", "no-employer-link"}


class IntakeFixer:
    def __init__(self, max_fetches: int = 150, client: Optional[httpx.Client] = None, db=None):
        self.max_fetches = max_fetches
        self.fetches = 0
        self.fixed_description = 0
        self.fixed_link = 0
        self.failed = 0
        self.db = db
        self.board_cache: dict = {}
        self._own = client is None
        self.client = client or httpx.Client(
            headers={"User-Agent": "Mozilla/5.0 (munichScraper intake; public pages only)", "Accept": "text/html,*/*"},
            timeout=15.0,
            follow_redirects=True,
        )

    def close(self) -> None:
        if self._own:
            self.client.close()

    def _budget(self) -> bool:
        if self.fetches >= self.max_fetches:
            return False
        self.fetches += 1
        return True

    def _raw_page(self, url: str) -> str:
        try:
            r = self.client.get(url)
            return r.text if r.status_code == 200 else ""
        except Exception:
            return ""

    def fix(self, job) -> bool:
        """Mutates job in place. Returns True when something was repaired (caller re-evaluates)."""
        changed = False
        gate = job.gate_rejected

        if gate == "no-description":
            if self._budget():
                text = fetch_description(self.client, job.url)
                if len(text) < MIN_DESCRIPTION_CHARS and job.apply_url and job.apply_url != job.url:
                    text = fetch_description(self.client, job.apply_url) if self._budget() else text
                if len(text) >= MIN_DESCRIPTION_CHARS:
                    job.description = text
                    job.description_source = "fetch"
                    self.fixed_description += 1
                    changed = True
                else:
                    self.failed += 1

        if gate == "no-employer-link" and (job.source or "").lower() in AGGREGATOR_SOURCES:
            # 7 Nov 2026: the employer's own board in the registry is the most reliable link, so try it first; links found in the page
            # come second (they can be the board's own app or social badges, see AGGREGATOR_DOMAINS).
            if self.db is not None and resolve_employer_link_via_registry(job, self.db, client=self.client, board_cache=self.board_cache):
                kind, host = classify_apply_kind(apply_url=job.apply_url, url=job.url, ats=job.ats, source=job.source, html_text=job.description)
                job.apply_kind = kind
                job.apply_host = host or extract_host_from_url(job.apply_url)
                job.apply_url_source = "registry"
                job.apply_url_status = "resolved"
                self.fixed_link += 1
                return True
            link = find_employer_link(job.description or "")
            raw = ""
            if not link and self._budget():
                raw = self._raw_page(job.url)
                link = find_employer_link(raw)
                if raw and len(job.description or "") < MIN_DESCRIPTION_CHARS:
                    txt = html_to_text(raw)[:20000]
                    if len(txt) >= MIN_DESCRIPTION_CHARS:
                        job.description = txt
                        job.description_source = "fetch"
                        self.fixed_description += 1
            if link and not is_aggregator_host(host_of(link)):
                job.apply_url = link
                kind, host = classify_apply_kind(apply_url=link, url=job.url, ats=job.ats, source=job.source, html_text=job.description)
                job.apply_kind = kind
                job.apply_host = host or extract_host_from_url(link)
                job.apply_url_source = "resolved"
                job.apply_url_status = "resolved"
                self.fixed_link += 1
                changed = True
            elif self.db is not None and resolve_employer_link_via_registry(job, self.db, client=self.client, board_cache=self.board_cache):
                kind, host = classify_apply_kind(apply_url=job.apply_url, url=job.url, ats=job.ats, source=job.source, html_text=job.description)
                job.apply_kind = kind
                job.apply_host = host or extract_host_from_url(job.apply_url)
                job.apply_url_source = "registry"
                job.apply_url_status = "resolved"
                self.fixed_link += 1
                changed = True
            else:
                job.apply_url_status = "none"
                self.failed += 1
        return changed
