"""
Main Orchestrator for munichScraper.
Always-on discovery layer for Mattia Carone's Munich job search.
Polls ATS registry, aggregators, and LinkedIn, enforces gates, scores qualified postings,
deduplicates cross-source, alerts on strong fresh matches, exports candidates, probes new company
boards, records metrics, and provides health auditing.
"""

import argparse
import csv
import json
import logging
import sys
import time

if hasattr(sys.stdout, "reconfigure"):
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")
if hasattr(sys.stderr, "reconfigure"):
    sys.stderr.reconfigure(encoding="utf-8", errors="replace")
from collections import defaultdict
from datetime import datetime, timezone
from logging.handlers import RotatingFileHandler
from pathlib import Path
from typing import Optional
from apscheduler.schedulers.blocking import BlockingScheduler
from apscheduler.triggers.interval import IntervalTrigger

from config import config
from database import Database
from matcher import ProfileMatcher, MUNICH_SUBURBS
from notifier import Notifier
from prober import BoardProber
from scrapers.arbeitnow import ArbeitnowScraper
from scrapers.ashby import AshbyScraper
from scrapers.base import BaseScraper
from scrapers.english_boards import (
    EnglishJobsDeScraper,
    MunichStartupScraper,
)
from scrapers.greenhouse import GreenhouseScraper
from scrapers.jobicy import JobicyScraper
from scrapers.lever import LeverScraper
from scrapers.personio import PersonioScraper
from scrapers.recruitee import RecruiteeScraper
from scrapers.remote_boards import (
    RemotiveScraper,
    WeWorkRemotelyScraper,
    RemoteOkScraper,
)
from scrapers.arbeitsagentur import ArbeitsagenturScraper
from ecosystem_partners import seed_ecosystem_partners
from company_discovery import import_known_slugs, import_expanded_slugs, run_daily_discovery
from intake_checks import MIN_DESCRIPTION_CHARS
from intake_fixer import FIXABLE_GATES, IntakeFixer
from supervisor import (
    CYCLE_PAUSE_SECONDS,
    SOURCE_CYCLE_MODULO,
    SOURCE_MIN_INTERVAL_HOURS,
    mark_source_run,
    run_forever,
    source_is_due,
    touch_heartbeat,
    write_pid,
)
from scrapers.smartrecruiters import SmartRecruitersScraper
from scrapers.stepstone import StepStoneScraper
from scrapers.teamtailor import TeamtailorScraper
from scrapers.workable import WorkableScraper
from liveness import LivenessEngine
from web_app import run_server

DATA_DIR = Path(__file__).resolve().parent / "data"

# Brief 14 A1: Never skip greenhouse or ashby; raise slow-source threshold to 240s for all other sources
SLOW_SOURCE_EXEMPT = {"greenhouse", "ashby"}
SLOW_SOURCE_THRESHOLD_SECONDS = 240.0


def should_skip_slow_source(slow_state: dict, source_name: str) -> bool:
    """Return True if source_name should be skipped this cycle due to slow-source backoff."""
    if source_name in SLOW_SOURCE_EXEMPT:
        slow_state[source_name] = {"consecutive_slow": 0, "skip_remaining": 0}
        return False
    info = slow_state.get(source_name, {"consecutive_slow": 0, "skip_remaining": 0})
    if info.get("skip_remaining", 0) > 0:
        info["skip_remaining"] -= 1
        slow_state[source_name] = info
        return True
    return False


def record_source_duration(slow_state: dict, source_name: str, dur: float) -> None:
    """Update slow_state after a source completes in `dur` seconds."""
    if source_name in SLOW_SOURCE_EXEMPT:
        slow_state[source_name] = {"consecutive_slow": 0, "skip_remaining": 0}
        return
    info = slow_state.get(source_name, {"consecutive_slow": 0, "skip_remaining": 0})
    if dur > SLOW_SOURCE_THRESHOLD_SECONDS:
        consec = info.get("consecutive_slow", 0) + 1
        info["consecutive_slow"] = consec
        if consec >= 2:
            info["skip_remaining"] = 3
            info["consecutive_slow"] = 0
            logger.warning(
                f"[Orchestrator] Source '{source_name}' took {dur:.1f}s (> {SLOW_SOURCE_THRESHOLD_SECONDS:.0f}s twice in a row). Skipping next 3 cycles."
            )
    else:
        info["consecutive_slow"] = 0
    slow_state[source_name] = info

# Logging configuration with 5 MB rotation (keep 3 files)
_scraper_log_path = config.db_path.parent / "scraper.log"
logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(name)s: %(message)s",
    handlers=[
        logging.StreamHandler(sys.stdout),
        RotatingFileHandler(
            _scraper_log_path,
            maxBytes=5 * 1024 * 1024,
            backupCount=3,
            encoding="utf-8",
        ),
    ],
)
_root_logger = logging.getLogger()
if not any(
    isinstance(h, RotatingFileHandler) and Path(getattr(h, "baseFilename", "")).name == "scraper.log"
    for h in _root_logger.handlers
):
    _fh = RotatingFileHandler(
        _scraper_log_path,
        maxBytes=5 * 1024 * 1024,
        backupCount=3,
        encoding="utf-8",
    )
    _fh.setFormatter(logging.Formatter("%(asctime)s [%(levelname)s] %(name)s: %(message)s"))
    _root_logger.addHandler(_fh)
    if _root_logger.level == logging.WARNING:
        _root_logger.setLevel(logging.INFO)
logger = logging.getLogger("Orchestrator")


def compute_source_yield(path: Optional[Path] = None) -> dict:
    """
    Build a per-source and per-ats table of how many of Mattia's applications
    ended with status starting 'Submitted' (or 'Applied') vs 'Rejected' from applied_history.csv.
    """
    target = path or config.APPLIED_HISTORY
    if not target or not target.exists():
        return {}

    yield_data = defaultdict(lambda: {"submitted": 0, "rejected": 0, "blocked": 0, "other": 0})
    try:
        with open(target, mode="r", encoding="utf-8", errors="ignore") as f:
            reader = csv.DictReader(f)
            for row in reader:
                st = (row.get("status") or "").strip().lower()
                link = (row.get("link") or "").lower()
                src_field = (row.get("source") or "").lower()

                # Detect ATS / Source from URL or source description
                ats = "other"
                if "greenhouse.io" in link or "greenhouse" in src_field:
                    ats = "greenhouse"
                elif "ashbyhq.com" in link or "ashby" in src_field:
                    ats = "ashby"
                elif "personio" in link or "personio" in src_field:
                    ats = "personio"
                elif "lever.co" in link or "lever" in src_field:
                    ats = "lever"
                elif "workable.com" in link or "workable" in src_field:
                    ats = "workable"
                elif "recruitee.com" in link or "recruitee" in src_field:
                    ats = "recruitee"
                elif "smartrecruiters" in link or "smartrecruiters" in src_field:
                    ats = "smartrecruiters"
                elif "linkedin.com" in link or "linkedin" in src_field:
                    ats = "linkedin"
                elif "arbeitnow" in link or "arbeitnow" in src_field:
                    ats = "arbeitnow"
                elif "vc" in src_field:
                    ats = "vc"

                if st.startswith("submitted") or st.startswith("applied"):
                    yield_data[ats]["submitted"] += 1
                elif st.startswith("rejected"):
                    yield_data[ats]["rejected"] += 1
                elif st.startswith("blocked"):
                    yield_data[ats]["blocked"] += 1
                else:
                    yield_data[ats]["other"] += 1
    except Exception as exc:
        logger.warning(f"Error computing source yield: {exc}")

    return dict(yield_data)


class JobEngine:
    def __init__(self):
        self.db = Database(config.db_path)
        self.matcher = ProfileMatcher(config.profile)
        self.notifier = Notifier(config)
        self.scrapers: list[BaseScraper] = [
            GreenhouseScraper(db=self.db, matcher=self.matcher, concurrency=20),
            AshbyScraper(db=self.db, matcher=self.matcher, concurrency=12),
            LeverScraper(db=self.db, matcher=self.matcher, concurrency=12),
            PersonioScraper(db=self.db, matcher=self.matcher, concurrency=12),
            WorkableScraper(db=self.db, matcher=self.matcher, concurrency=12),
            RecruiteeScraper(db=self.db, matcher=self.matcher, concurrency=12),
            SmartRecruitersScraper(db=self.db, matcher=self.matcher, concurrency=12),
            TeamtailorScraper(db=self.db, concurrency=8),
            StepStoneScraper(db=self.db, matcher=self.matcher),
            EnglishJobsDeScraper(db=self.db),
            MunichStartupScraper(db=self.db),
            RemotiveScraper(db=self.db),
            WeWorkRemotelyScraper(db=self.db),
            ArbeitnowScraper(db=self.db, matcher=self.matcher, max_pages=15, concurrency=3),
            JobicyScraper(),
            ArbeitsagenturScraper(db=self.db),
            RemoteOkScraper(db=self.db),
        ]
        self.matcher.min_description_chars = MIN_DESCRIPTION_CHARS
        self._intake_stats: dict = {}
        try:
            seed_ecosystem_partners(self.db)
            import_known_slugs(self.db)
            import_expanded_slugs(self.db)
        except Exception as exc:
            logger.debug(f"[JobEngine] Ecosystem partners / known slugs seeding error: {exc}")

    def _evaluate_with_fix(self, job, fixer: IntakeFixer):
        """Evaluate a job; if rejected with a fixable intake gate, attempt public-page repair and re-evaluate."""
        evaluated = self.matcher.evaluate(job, client=fixer.client)
        for _ in range(2):
            if evaluated.gate_rejected in FIXABLE_GATES and fixer.fix(evaluated):
                evaluated = self.matcher.evaluate(evaluated, client=fixer.client)
            else:
                break
        return evaluated

    def _repair_passing_without_description(self, fixer: IntakeFixer, gate_counts: Optional[dict] = None) -> int:
        """Ensure every row in DB that currently passes the contract satisfies Brief 12 (description, employer link, hard blocks, Clera)."""
        with self.db._get_connection() as conn:
            rows = conn.execute(
                "SELECT * FROM jobs WHERE gate_rejected IS NULL AND score > 0 AND closed_at IS NULL"
            ).fetchall()
        if not rows:
            return 0
        passing_jobs = [self.db._row_to_job(r) for r in rows]
        updated = []
        for j in passing_jobs:
            ev = self._evaluate_with_fix(j, fixer)
            if ev.gate_rejected and gate_counts is not None:
                gate_counts[ev.gate_rejected] = gate_counts.get(ev.gate_rejected, 0) + 1
            updated.append(ev)
        self.db.save_jobs(updated)
        return len(updated)

    def _get_brief_7b_metrics(self) -> dict:
        """Compute Section 5 metrics for IMPROVEMENT_BRIEF_7b."""
        import re
        from urllib.parse import urlparse

        with self.db._get_connection() as conn:
            rows = conn.execute(
                "SELECT id, source, url, location, title, description FROM jobs WHERE gate_rejected IS NULL AND score > 0 AND closed_at IS NULL"
            ).fetchall()

        total_passed = len(rows)
        munich_passed = 0
        non_de_com_country_tld = 0
        non_de_com_country_no_uk = 0
        by_tld: dict[str, int] = defaultdict(int)

        munich_subs = tuple(set(MUNICH_SUBURBS) | {"munich", "münchen"})
        foreign_tld_re = re.compile(
            r"\.(fr|se|nl|es|it|pl|dk|no|be|co\.uk|uk|ch|at|pt|fi|cz|hu|ro|ie)$",
            re.I,
        )

        for r in rows:
            url = r["url"] or ""
            loc = (r["location"] or "").lower()
            tit = (r["title"] or "").lower()
            desc = (r["description"] or "").lower()

            if any(s in loc for s in munich_subs) or (not loc and any(s in f"{tit} {desc}" for s in munich_subs)):
                munich_passed += 1

            host = urlparse(url).netloc.lower().split(":")[0]
            m = foreign_tld_re.search(host)
            if m:
                tld = m.group(1).lower()
                by_tld[tld] += 1
                non_de_com_country_tld += 1
                if tld not in ("co.uk", "uk"):
                    non_de_com_country_no_uk += 1

        return {
            "total_passed": total_passed,
            "munich_passed": munich_passed,
            "non_de_com_country_tld": non_de_com_country_tld,
            "non_de_com_country_no_uk": non_de_com_country_no_uk,
            "by_tld": dict(by_tld),
        }

    def _get_brief_8_9_snapshot(self) -> dict:
        """Capture passed counts, location_scope breakdown, and gate counts before/after rescore."""
        with self.db._get_connection() as conn:
            passed_rows = conn.execute(
                """
                SELECT id, source, company, company_display, title, location, location_scope,
                       apply_route, needs_account, years_required, contract_type, german_required,
                       title_weight, score, coalesce(posted_at, created_at) as dt
                FROM jobs
                WHERE gate_rejected IS NULL AND score > 0 AND closed_at IS NULL
                """
            ).fetchall()

            gate_rows = conn.execute(
                """
                SELECT gate_rejected, COUNT(*) as cnt
                FROM jobs
                WHERE gate_rejected IS NOT NULL
                GROUP BY gate_rejected
                """
            ).fetchall()

        by_scope: dict[str, int] = defaultdict(int)
        by_route: dict[str, int] = defaultdict(int)
        remote_eu_last_7d = 0
        age_hours_list: list[float] = []
        now = datetime.now(timezone.utc)

        munich_subs = tuple(set(MUNICH_SUBURBS) | {"munich", "münchen"})

        for r in passed_rows:
            sc = r["location_scope"]
            if not sc:
                loc_l = (r["location"] or "").lower()
                if any(s in loc_l for s in munich_subs):
                    sc = "munich"
                elif "germany" in loc_l or "deutschland" in loc_l or "dach" in loc_l:
                    sc = "remote-de"
                else:
                    sc = "remote-eu"
            by_scope[sc] += 1
            by_route[r["apply_route"] or "unknown"] += 1

            dt_str = r["dt"]
            if dt_str:
                try:
                    dt_obj = datetime.fromisoformat(str(dt_str))
                    tz_dt = dt_obj if dt_obj.tzinfo else dt_obj.replace(tzinfo=timezone.utc)
                    age_h = max(0.0, (now - tz_dt).total_seconds() / 3600.0)
                    age_hours_list.append(age_h)
                    if sc == "remote-eu" and (age_h / 24.0) <= 7.0:
                        remote_eu_last_7d += 1
                except Exception:
                    pass

        return {
            "total_passed": len(passed_rows),
            "by_scope": dict(by_scope),
            "by_route": dict(by_route),
            "remote_eu_last_7d": remote_eu_last_7d,
            "gate_counts": {r["gate_rejected"]: r["cnt"] for r in gate_rows},
            "age_hours_list": age_hours_list,
        }

    def rescore_all_jobs(self) -> None:
        """
        Re-evaluates all previously stored jobs against updated gates (Briefs 1-14),
        cleans up legacy defaulted locations (Brief 7b), resolves employer apply links,
        and prints the audit report.
        """
        from scrapers.arbeitnow import infer_arbeitnow_location
        from intake_checks import resolve_employer_link_via_registry

        start_time = datetime.now(timezone.utc)
        logger.info(">>> Re-scoring all stored jobs in SQLite against updated gates...")
        self.matcher.reload_data()
        self.db.backfill_company_display()

        before_7b = self._get_brief_7b_metrics()
        before_8_9 = self._get_brief_8_9_snapshot()

        # Refresh Teamtailor jobs so real JSON-LD / HTML locations replace legacy 'Munich, Germany' defaults
        fresh_by_id = {}
        for scraper_name in ("teamtailor",):
            sc = next((s for s in self.scrapers if s.name == scraper_name), None)
            if sc is not None:
                try:
                    logger.info(f"[Rescore] Refreshing locations & metadata from {scraper_name}...")
                    for fj in sc.scrape():
                        fresh_by_id[fj.id] = fj
                except Exception as exc:
                    logger.warning(f"[Rescore] Could not refresh {scraper_name}: {exc}")

        # Resolve Arbeitnow candidates missing employer apply_url
        arbeitnow_scraper = next((s for s in self.scrapers if s.name == "arbeitnow"), None)
        if arbeitnow_scraper and hasattr(arbeitnow_scraper, "resolve_candidates"):
            with self.db._get_connection() as conn:
                cursor = conn.execute(
                    "SELECT * FROM jobs WHERE source = 'arbeitnow' AND gate_rejected IS NULL AND (apply_url IS NULL OR apply_kind = 'unknown')"
                )
                an_rows = cursor.fetchall()
            an_jobs = [self.db._row_to_job(r) for r in an_rows]
            an_to_resolve = []
            for aj in an_jobs:
                raw_loc = None if aj.location == "Munich, Germany" else aj.location
                loc_inferred, loc_src = infer_arbeitnow_location(raw_loc, aj.title, aj.description, aj.url)
                aj.location = loc_inferred
                aj.location_source = loc_src
                ok_loc, _ = self.matcher.check_gate_location(aj.location, aj.title, aj.description, aj.url, aj.location_source)
                ok_sen, _ = self.matcher.check_gate_seniority(aj.title)
                ok_age, _ = self.matcher.check_gate_age(aj.posted_at)
                ok_skip, _ = self.matcher.check_gate_skip_list(aj.company)
                ok_off, _ = self.matcher.check_gate_off_function(aj.title)
                ok_ger_tit, _ = self.matcher.check_gate_german(aj.title, "")
                if ok_loc and ok_sen and ok_age and ok_skip and ok_off and ok_ger_tit:
                    an_to_resolve.append(aj)
                else:
                    aj.apply_kind = "unknown"
            if an_to_resolve:
                logger.info(f"[Rescore] Resolving employer apply URLs for {len(an_to_resolve)} Arbeitnow candidate rows...")
                arbeitnow_scraper.resolve_candidates(an_to_resolve)
                self.db.save_jobs(an_to_resolve)

        all_jobs = self.db.get_all_jobs()
        logger.info(f"Loaded {len(all_jobs)} existing postings from SQLite.")

        # Reset dedupe counters right before the full rescore pass
        self.matcher.reload_data()

        gate_counts = defaultdict(int)
        source_counts = defaultdict(int)
        passed_count = 0
        linkedin_ats_resolved = 0
        rescored = []

        for job in all_jobs:
            # Apply refreshed Teamtailor data or clear legacy defaulted 'Munich, Germany'
            if job.id in fresh_by_id:
                fj = fresh_by_id[job.id]
                job.location = fj.location
                job.location_source = fj.location_source
                if fj.description:
                    job.description = fj.description
                if fj.url:
                    job.url = fj.url
                if fj.apply_url:
                    job.apply_url = fj.apply_url
            elif job.source in ("teamtailor", "softgarden", "wellfound") and job.location == "Munich, Germany":
                job.location = None
                job.location_source = "unknown"
            elif job.source == "arbeitnow":
                raw_loc = None if job.location == "Munich, Germany" else job.location
                loc_inferred, loc_src = infer_arbeitnow_location(raw_loc, job.title, job.description, job.url)
                job.location = loc_inferred
                job.location_source = loc_src

            # Resolve rows without external apply_url against known ATS boards in DB
            if not job.apply_url or "linkedin.com" in (job.apply_url or "").lower():
                if resolve_employer_link_via_registry(job, self.db):
                    linkedin_ats_resolved += 1

            source_counts[job.source] += 1
            updated = self.matcher.evaluate(job)
            rescored.append(updated)
            if updated.gate_rejected:
                gate_counts[updated.gate_rejected] += 1
            else:
                passed_count += 1

        self.db.save_jobs(rescored)

        # Cross-source deduplication
        dups_marked = self.db.deduplicate_all_jobs()
        if dups_marked:
            gate_counts["cross_source_dup"] += dups_marked

        with self.db._get_connection() as conn:
            passed_total = conn.execute(
                "SELECT COUNT(*) FROM jobs WHERE gate_rejected IS NULL AND score > 0 AND closed_at IS NULL"
            ).fetchone()[0]

            cur_k = conn.execute(
                "SELECT coalesce(apply_kind, 'unknown'), COUNT(*) FROM jobs WHERE gate_rejected IS NULL AND score > 0 AND closed_at IS NULL GROUP BY apply_kind"
            )
            by_apply_kind = {row[0]: row[1] for row in cur_k.fetchall()}

            cur_s = conn.execute(
                "SELECT source, COUNT(*) FROM jobs WHERE gate_rejected IS NULL AND score > 0 AND closed_at IS NULL GROUP BY source"
            )
            by_source = {row[0]: row[1] for row in cur_s.fetchall()}

            cur_dt = conn.execute(
                "SELECT coalesce(posted_at, created_at) FROM jobs WHERE gate_rejected IS NULL AND score > 0 AND closed_at IS NULL ORDER BY coalesce(posted_at, created_at) DESC LIMIT 1"
            )
            row_dt = cur_dt.fetchone()
            newest_posting_iso = row_dt[0] if row_dt and row_dt[0] else datetime.now(timezone.utc).isoformat()

        exported_count = 0

        # Record cycle metric for rescore
        duration_s = (datetime.now(timezone.utc) - start_time).total_seconds()
        self.db.record_cycle_metrics(
            started_at=start_time.isoformat(),
            duration_s=duration_s,
            per_source_postings=dict(source_counts),
            per_source_errors={},
            new_rows=len(rescored),
            rejected_gates=dict(gate_counts),
            passed=passed_total,
            closed=0,
            new_boards_found=0,
            linkedin_pauses=0,
            per_source_timing={},
            passed_total=passed_total,
            by_apply_kind=by_apply_kind,
            by_source=by_source,
            exported=exported_count,
        )

        heartbeat_file = config.db_path.parent / "daemon.heartbeat"
        heartbeat_file.write_text(datetime.now(timezone.utc).isoformat(), encoding="utf-8")

        # Write data/heartbeat.json (Brief 5)
        heartbeat_json = DATA_DIR / "heartbeat.json"
        DATA_DIR.mkdir(parents=True, exist_ok=True)
        hb_payload = {
            "time": datetime.now(timezone.utc).isoformat(),
            "cycle_seconds": round(duration_s, 1),
            "passed_total": passed_total,
            "newest_posting": newest_posting_iso,
        }
        heartbeat_json.write_text(json.dumps(hb_payload, indent=2), encoding="utf-8")

        self._print_funnel(
            source_counts=dict(source_counts),
            gate_counts=gate_counts,
            passed_count=passed_total,
            exported_count=exported_count,
        )
        self.print_brief_7_audit()
        after_7b = self._get_brief_7b_metrics()
        self.print_brief_7b_audit(before_7b, after_7b)
        after_8_9 = self._get_brief_8_9_snapshot()
        self.print_brief_8_9_report(before_8_9, after_8_9, linkedin_ats_resolved=linkedin_ats_resolved)

    def print_brief_8_9_report(self, before: dict, after: dict, linkedin_ats_resolved: int = 0) -> None:
        """
        Print the complete Brief 8 and Brief 9 report:
        - Rows passed before and after (overall and by location_scope: munich, remote-de, remote-eu)
        - New remote-eu rows from the last 7 days
        - Counts grouped by new gate code (`german_required`, `years_required`, `contract`, `deep_stack`, `clearance`)
          plus 10 example titles (and quotes) for each gate
        - 15 most penalised and 15 most boosted title terms from Section 1
        - Section 3 apply_route & needs_account breakdown
        - Section 4 age_hours freshness summary
        - Section 5 deduplication skipped counts
        """
        print("\n" + "=" * 90)
        print(" BRIEF 8 & BRIEF 9 REPORT")
        print("=" * 90)
        print(f"Total Passed Rows (gate_rejected IS NULL AND score > 0 AND closed_at IS NULL):")
        print(f"  Before: {before['total_passed']:5d}")
        print(f"  After:  {after['total_passed']:5d}  (Delta: {after['total_passed'] - before['total_passed']:+d})")

        print("\n1. Passed Rows by Location Scope (Brief 8):")
        for sc in ("munich", "remote-de", "remote-eu"):
            b_cnt = before["by_scope"].get(sc, 0)
            a_cnt = after["by_scope"].get(sc, 0)
            print(f"  - {sc:12s}: {b_cnt:5d} -> {a_cnt:5d}  ({a_cnt - b_cnt:+d})")
        print(f"  - New 'remote-eu' rows (last 7 days): {after['remote_eu_last_7d']:5d}")

        new_gates = ["german_required", "years_required", "contract", "deep_stack", "clearance"]
        print("\n2. Rows Gated by New Brief 9 Gate Codes (with 10 Example Titles per Gate):")
        with self.db._get_connection() as conn:
            for gcode in new_gates:
                b_g = before["gate_counts"].get(gcode, 0)
                a_g = after["gate_counts"].get(gcode, 0)
                print(f"\n  [{gcode}] — Total Gated: {a_g:5d} (Before: {b_g:5d})")
                ex_rows = conn.execute(
                    """
                    SELECT company_display, company, title, location, coalesce(gate_reason, rejection_reason) as reason
                    FROM jobs
                    WHERE gate_rejected = ?
                    ORDER BY coalesce(posted_at, created_at) DESC
                    LIMIT 10
                    """,
                    (gcode,),
                ).fetchall()
                if not ex_rows:
                    print("    (0 rows matched this gate)")
                for idx, r in enumerate(ex_rows, 1):
                    comp = r["company_display"] or r["company"] or "Unknown"
                    tit = r["title"] or ""
                    rsn = (r["reason"] or "")[:95]
                    print(f"    {idx:2d}. [{comp}] {tit} — \"{rsn}\"")

        penalised, boosted = self.matcher.get_top_title_weights(n=15)
        print("\n3. Outcome-Learned & Role-Family Title Weights (Brief 9 Section 1):")
        print("  Top 15 Most Penalised Title Terms:")
        for idx, (term, w, st) in enumerate(penalised, 1):
            print(
                f"    {idx:2d}. {term:34s} {w:+6.1f}  (sent={st.get('sent',0)}, rej={st.get('rejected',0)}, int={st.get('interview',0)})"
            )
        print("\n  Top 15 Most Boosted Title Terms:")
        for idx, (term, w, st) in enumerate(boosted, 1):
            print(
                f"    {idx:2d}. {term:34s} {w:+6.1f}  (sent={st.get('sent',0)}, rej={st.get('rejected',0)}, int={st.get('interview',0)})"
            )

        print("\n4. Apply Route Breakdown on Passing Rows (Brief 9 Section 3):")
        for route, cnt in sorted(after["by_route"].items(), key=lambda x: (-x[1], x[0])):
            print(f"  - {route:18s}: {cnt:5d}")
        with self.db._get_connection() as conn:
            needs_acc_cnt = conn.execute(
                "SELECT COUNT(*) FROM jobs WHERE gate_rejected IS NULL AND score > 0 AND closed_at IS NULL AND needs_account = 1"
            ).fetchone()[0]
        print(f"  - Passing rows parked with needs_account = 1: {needs_acc_cnt:5d}")
        print(f"  - LinkedIn rows resolved via known ATS boards: {linkedin_ats_resolved:5d}")

        print("\n5. Freshness (age_hours) on Passing Rows (Brief 9 Section 4):")
        ages = sorted(after.get("age_hours_list") or [])
        if ages:
            under_48h = sum(1 for a in ages if a <= 48.0)
            under_7d = sum(1 for a in ages if a <= 168.0)
            median_h = ages[len(ages) // 2]
            min_h = ages[0]
            print(f"  - Newest passing row age_hours: {min_h:6.1f}h")
            print(f"  - Median passing row age_hours: {median_h:6.1f}h")
            print(f"  - Passing rows <= 48 hours (+8 boost): {under_48h:5d}")
            print(f"  - Passing rows <= 7 days  (+4 boost): {under_7d:5d}")

        dedupe_stats = self.matcher.dedupe_skipped_counts
        print("\n6. Deduplication vs applied_history.csv (Brief 9 Section 5):")
        print(f"  - Exact (company, role) match skipped:          {dedupe_stats.get('exact_dup', 0):5d}")
        print(f"  - Submitted 14-char title prefix match skipped: {dedupe_stats.get('prefix14_dup', 0):5d}")
        print(f"  - Company cap (>= 3 Submitted, Amazon exempt):  {dedupe_stats.get('company_cap_3', 0):5d}")
        print(f"  - Total gate='dup' rows:                        {after['gate_counts'].get('dup', 0):5d}")
        print("=" * 90 + "\n")

    def print_brief_7b_audit(self, before: dict, after: dict) -> None:
        """Print Section 5 audit numbers requested by IMPROVEMENT_BRIEF_7b."""
        print("\n" + "=" * 80)
        print(" BRIEF 7b AUDIT NUMBERS (SECTION 5)")
        print("=" * 80)
        print(
            f"Passing rows with non-.de/.com TLD (excl. .uk): {before['non_de_com_country_no_uk']:4d} -> {after['non_de_com_country_no_uk']:4d}"
        )
        print(
            f"Passing rows with non-.de/.com TLD (incl. .uk): {before['non_de_com_country_tld']:4d} -> {after['non_de_com_country_tld']:4d}"
        )
        if before.get("by_tld"):
            print(f"  Before by TLD: {before['by_tld']}")
        if after.get("by_tld"):
            print(f"  After by TLD:  {after['by_tld']}")
        print(
            f"Passing Munich rows:                            {before['munich_passed']:4d} -> {after['munich_passed']:4d}"
        )
        print(
            f"Total Passed rows:                              {before['total_passed']:4d} -> {after['total_passed']:4d}"
        )
        print("=" * 80 + "\n")

    def print_brief_7_audit(self, new_boards_found: Optional[int] = None) -> None:
        """Print Section 6 audit numbers requested by IMPROVEMENT_BRIEF_7."""
        with self.db._get_connection() as conn:
            # Passed per source
            cur_s = conn.execute(
                "SELECT source, COUNT(*) FROM jobs WHERE gate_rejected IS NULL AND score > 0 AND closed_at IS NULL GROUP BY source ORDER BY COUNT(*) DESC"
            )
            by_source = dict(cur_s.fetchall())

            # Munich passed
            cur_munich = conn.execute("""
                SELECT COUNT(*) FROM jobs 
                WHERE gate_rejected IS NULL AND score > 0 AND closed_at IS NULL
                  AND (lower(location) LIKE '%munich%' OR lower(location) LIKE '%münchen%' 
                       OR lower(location) LIKE '%garching%' OR lower(location) LIKE '%unterföhring%'
                       OR lower(location) LIKE '%ismaning%' OR lower(location) LIKE '%aschheim%'
                       OR lower(location) LIKE '%martinsried%' OR lower(location) LIKE '%planegg%'
                       OR lower(location) LIKE '%oberhaching%' OR lower(location) LIKE '%gilching%'
                       OR lower(location) LIKE '%hallbergmoos%' OR lower(location) LIKE '%unterschleißheim%'
                       OR lower(location) LIKE '%grünwald%' OR lower(location) LIKE '%ottobrunn%'
                       OR lower(location) LIKE '%germering%' OR lower(location) LIKE '%dachau%'
                       OR lower(location) LIKE '%starnberg%'
                       OR (location IS NULL AND (lower(title) LIKE '%munich%' OR lower(title) LIKE '%münchen%' OR lower(description) LIKE '%munich%' OR lower(description) LIKE '%münchen%')))
            """)
            munich_passed = cur_munich.fetchone()[0]

            # Munich start-ups passed (is_startup=1)
            cur_munich_startup = conn.execute("""
                SELECT COUNT(*) FROM jobs 
                WHERE gate_rejected IS NULL AND score > 0 AND closed_at IS NULL
                  AND is_startup = 1
                  AND (lower(location) LIKE '%munich%' OR lower(location) LIKE '%münchen%' 
                       OR lower(location) LIKE '%garching%' OR lower(location) LIKE '%unterföhring%'
                       OR lower(location) LIKE '%ismaning%' OR lower(location) LIKE '%aschheim%'
                       OR lower(location) LIKE '%martinsried%' OR lower(location) LIKE '%planegg%'
                       OR lower(location) LIKE '%oberhaching%' OR lower(location) LIKE '%gilching%'
                       OR lower(location) LIKE '%hallbergmoos%' OR lower(location) LIKE '%unterschleißheim%'
                       OR lower(location) LIKE '%grünwald%' OR lower(location) LIKE '%ottobrunn%'
                       OR lower(location) LIKE '%germering%' OR lower(location) LIKE '%dachau%'
                       OR lower(location) LIKE '%starnberg%'
                       OR (location IS NULL AND (lower(title) LIKE '%munich%' OR lower(title) LIKE '%münchen%' OR lower(description) LIKE '%munich%' OR lower(description) LIKE '%münchen%')))
            """)
            munich_startup_passed = cur_munich_startup.fetchone()[0]

            # Total start-ups passed
            cur_startup = conn.execute(
                "SELECT COUNT(*) FROM jobs WHERE gate_rejected IS NULL AND score > 0 AND closed_at IS NULL AND is_startup = 1"
            )
            total_startup_passed = cur_startup.fetchone()[0]

            # Rows rescued by age change (age > 21 days)
            cur_rescued = conn.execute("""
                SELECT COUNT(*) FROM jobs 
                WHERE gate_rejected IS NULL AND score > 0 AND closed_at IS NULL
                  AND (julianday('now') - julianday(coalesce(posted_at, created_at))) > 21.0
            """)
            rescued_by_age = cur_rescued.fetchone()[0]

            if new_boards_found is None:
                cur_boards = conn.execute("SELECT new_boards_found FROM cycle_metrics ORDER BY id DESC LIMIT 1")
                row = cur_boards.fetchone()
                new_boards_found = row[0] if row else 0

        print("\n" + "=" * 80)
        print(" BRIEF 7 AUDIT NUMBERS")
        print("=" * 80)
        print("Passed Candidate Postings per Source:")
        for s, c in by_source.items():
            print(f"  - {s:20s}: {c:5d}")
        print(f"Total Passed:                {sum(by_source.values()):5d}")
        print(f"Munich Passed:               {munich_passed:5d}")
        print(f"Munich Start-ups Passed:     {munich_startup_passed:5d}")
        print(f"Total Start-ups Passed:      {total_startup_passed:5d}")
        print(f"Rows Rescued by Age Change:  {rescued_by_age:5d}  (> 21 days old, confirmed live)")
        print(f"New Boards Found by Prober:  {new_boards_found:5d}")
        print("=" * 80 + "\n")

    def print_health(self) -> None:
        """Print recent cycle metrics, passed breakdowns, outcome yield, and check health warnings."""
        cycles = self.db.get_recent_cycle_metrics(limit=10)
        if not cycles:
            print("\nNo cycle metrics recorded yet. Run `python main.py --run-once` first.")
            return

        print("\n" + "=" * 95)
        print(" PIPELINE HEALTH & METRICS (LAST 10 CYCLES)")
        print("=" * 95)
        header = f"{'ID':>4} | {'Started At':<19} | {'Dur(s)':>6} | {'New':>6} | {'Passed':>6} | {'Closed':>6} | {'Boards':>6} | {'429s':>4}"
        print(header)
        print("-" * 95)

        for c in reversed(cycles):
            st = c["started_at"][:19] if c["started_at"] else ""
            dur = f"{c['duration_s']:.1f}"
            print(
                f"{c['id']:>4} | {st:<19} | {dur:>6} | {c['new_rows']:>6} | {c['passed']:>6} | {c['closed']:>6} | {c['new_boards_found']:>6} | {c['linkedin_pauses']:>4}"
            )
        print("=" * 95)

        # Passed candidates breakdown
        with self.db._get_connection() as conn:
            cur_k = conn.execute(
                "SELECT coalesce(apply_kind, 'unknown'), COUNT(*) FROM jobs WHERE gate_rejected IS NULL AND score > 0 AND closed_at IS NULL GROUP BY apply_kind"
            )
            passed_by_kind = dict(cur_k.fetchall())

            cur_s = conn.execute(
                "SELECT source, COUNT(*) FROM jobs WHERE gate_rejected IS NULL AND score > 0 AND closed_at IS NULL GROUP BY source"
            )
            passed_by_source = dict(cur_s.fetchall())

            cur_dt = conn.execute(
                "SELECT coalesce(posted_at, created_at) FROM jobs WHERE gate_rejected IS NULL AND score > 0 AND closed_at IS NULL ORDER BY coalesce(posted_at, created_at) DESC LIMIT 1"
            )
            row_dt = cur_dt.fetchone()
            newest_posting_iso = row_dt[0] if row_dt and row_dt[0] else None

        total_passed_candidates = sum(passed_by_kind.values())
        auto_passed_candidates = passed_by_kind.get("auto", 0)

        print(f"\nCandidate Postings Breakdown (Total Passed: {total_passed_candidates}):")
        print(f"  [+] Automated Applications (apply_kind = 'auto'): {auto_passed_candidates:4d}")
        print("  By Apply Kind:")
        for k, v in sorted(passed_by_kind.items()):
            print(f"    - {k:15s}: {v:4d}")
        print("  By Source:")
        for s, v in sorted(passed_by_source.items()):
            print(f"    - {s:15s}: {v:4d}")

        # Postings, New (Today), and Passed per Source (Brief 6 Section 8)
        with self.db._get_connection() as conn:
            cur_daily = conn.execute("""
                SELECT source,
                       COUNT(*) as total_postings,
                       SUM(CASE WHEN date(created_at) = date('now') THEN 1 ELSE 0 END) as today_new,
                       SUM(CASE WHEN gate_rejected IS NULL AND score > 0 AND closed_at IS NULL THEN 1 ELSE 0 END) as passed_count
                FROM jobs
                GROUP BY source
                ORDER BY passed_count DESC, total_postings DESC
            """)
            daily_rows = cur_daily.fetchall()

        print("\nPostings, New (Today), and Passed per Source:")
        print(f"  {'Source':<22} | {'Total in DB':>12} | {'New Today':>10} | {'Passed (Active)':>15}")
        print("  " + "-" * 67)
        for r in daily_rows:
            print(f"  {r[0]:<22} | {r[1]:>12d} | {r[2]:>10d} | {r[3]:>15d}")

        newest_age_hours = None
        if newest_posting_iso:
            try:
                np_dt = datetime.fromisoformat(newest_posting_iso)
                tz_np = np_dt if np_dt.tzinfo else np_dt.replace(tzinfo=timezone.utc)
                newest_age_hours = (datetime.now(timezone.utc) - tz_np).total_seconds() / 3600.0
                print(f"\n  Newest Passed Candidate Age: {newest_age_hours:.1f} hours ({newest_posting_iso[:19]})")
            except Exception:
                print(f"\n  Newest Passed Candidate Date: {newest_posting_iso}")

        # Source Yield from applied_history.csv
        yield_stats = compute_source_yield()
        if yield_stats:
            print("\nSource Yield (Outcome Feedback from applied_history.csv):")
            print(f"  {'Source/ATS':<18} | {'Submitted':>10} | {'Rejected':>10} | {'Blocked':>10}")
            print("  " + "-" * 56)
            for src_name, y_info in sorted(yield_stats.items(), key=lambda x: x[1]['submitted'], reverse=True):
                print(f"  {src_name:<18} | {y_info['submitted']:>10d} | {y_info['rejected']:>10d} | {y_info['blocked']:>10d}")

        # Health warnings
        print("\nHealth Checks:")
        warnings_found = False

        # Daemon heartbeat check
        heartbeat_file = config.db_path.parent / "daemon.heartbeat"
        if heartbeat_file.exists():
            try:
                hb_iso = heartbeat_file.read_text(encoding="utf-8").strip()
                hb_dt = datetime.fromisoformat(hb_iso)
                tz_hb = hb_dt if hb_dt.tzinfo else hb_dt.replace(tzinfo=timezone.utc)
                age_min = (datetime.now(timezone.utc) - tz_hb).total_seconds() / 60.0
                print(f"  Daemon Heartbeat: {hb_iso} ({age_min:.1f} minutes ago)")
                if age_min > 60.0:
                    print(f"  [!] WARNING: Daemon heartbeat is older than 60 minutes ({age_min:.1f} min)!")
                    warnings_found = True
            except Exception as exc:
                print(f"  [!] WARNING: Could not parse daemon.heartbeat: {exc}")
                warnings_found = True
        else:
            print("  [!] WARNING: No daemon.heartbeat file found.")
            warnings_found = True

        # Warning when no new passing row in 24 hours
        if newest_age_hours is not None and newest_age_hours > 24.0:
            print(f"  [!] WARNING: No new passing candidate has appeared in 24 hours ({newest_age_hours:.1f} hours old)!")
            warnings_found = True

        if len(cycles) >= 3:
            recent_3 = cycles[:3]
            all_sources = [
                "greenhouse",
                "ashby",
                "lever",
                "personio",
                "workable",
                "recruitee",
                "smartrecruiters",
                "arbeitnow",
                "jobicy",
                "linkedin",
            ]
            for src in all_sources:
                zeros = 0
                for cy in recent_3:
                    src_dict = json.loads(cy.get("per_source_postings") or "{}")
                    if src_dict.get(src, 0) == 0:
                        zeros += 1
                if zeros >= 3:
                    print(f"  [!] WARNING: Source '{src}' returned 0 postings for 3 cycles in a row!")
                    warnings_found = True

            li_pauses_count = sum(1 for cy in recent_3 if cy.get("linkedin_pauses", 0) >= 4)
            if li_pauses_count >= 1:
                print("  [!] WARNING: LinkedIn hit maximum rate-limit pauses (4) in recent cycles.")
                warnings_found = True

        if not warnings_found:
            print("  [OK] All pipeline health checks passed cleanly.")
        print()

    def run_pipeline(self) -> None:
        """
        Executes a scheduled ingestion cycle across all scrapers, evaluates gates,
        persists to SQLite, closes missing postings, deduplicates cross-source,
        notifies, probes new company boards, and records metrics.
        """
        start_time = datetime.now(timezone.utc)
        logger.info(f">>> Starting scheduled job ingestion cycle at {start_time.isoformat()}...")

        # 1. Reload live data and backfill company display
        self.matcher.reload_data()
        # Brief 13 Section 1: read role_rules.json at the start of each cycle
        self.matcher.load_role_rules()
        # Brief 14 A2: reset 150-fetch cap per cycle for vague-location description checks
        self.matcher.reset_vague_fetch_budget(max_fetches=150)
        self.db.backfill_company_display()

        # Recalibrate apply_kind from real application results (Brief 10 Section 5)
        try:
            from route_calibrator import calibrate_route_success, apply_calibrated_routes_to_db
            calibrate_route_success()
            apply_calibrated_routes_to_db(self.db)
        except Exception as exc:
            logger.warning(f"[RouteCalibrator] Could not calibrate routes: {exc}")

        # Check automatic daily pruning (Brief 14 B6: 7 days)
        last_prune = self.db.get_metadata("last_prune_at")
        should_prune = True
        if last_prune:
            try:
                lp_dt = datetime.fromisoformat(last_prune)
                tz_lp = lp_dt if lp_dt.tzinfo else lp_dt.replace(tzinfo=timezone.utc)
                if (datetime.now(timezone.utc) - tz_lp).total_seconds() < 86400:
                    should_prune = False
            except Exception:
                should_prune = True
        if should_prune:
            self.db.prune_rejected_jobs(max_age_days=7)
            self.db.clean_rejected_descriptions()

        source_counts = defaultdict(int)
        source_errors = defaultdict(int)
        source_timings: dict[str, float] = {}
        source_listed: dict[str, int] = {}
        source_details: dict[str, int] = {}
        source_passed: dict[str, int] = {}
        gate_counts = defaultdict(int)
        passed_count = 0
        all_evaluated_jobs = []

        # Load slow source tracking state
        slow_state_raw = self.db.get_metadata("source_slow_state")
        slow_state = json.loads(slow_state_raw) if slow_state_raw else {}

        # Active IDs per ATS slug to detect closed postings
        active_ids_by_ats: dict[str, dict[str, set[str]]] = defaultdict(lambda: defaultdict(set))

        # 2. Ingestion across scrapers (Brief 14 A1: run greenhouse and ashby first)
        fixer = IntakeFixer(max_fetches=150, db=self.db)
        intake_totals = {"duplicate_triple": 0, "company_cap": 0, "fixed_description": 0, "fixed_link": 0}
        touch_heartbeat()
        ordered_scrapers = [s for s in self.scrapers if s.name in ("greenhouse", "ashby")] + [
            s for s in self.scrapers if s.name not in ("greenhouse", "ashby")
        ]
        for scraper in ordered_scrapers:
            if should_skip_slow_source(slow_state, scraper.name):
                rem = slow_state.get(scraper.name, {}).get("skip_remaining", 0)
                logger.warning(
                    f"[Orchestrator] Skipping slow source '{scraper.name}' (took > {SLOW_SOURCE_THRESHOLD_SECONDS:.0f}s twice in a row; {rem} skip cycles remaining)."
                )
                continue

            # Brief 12 section 4 & Brief 14 B1: low-yield sources run on their own cadence
            if not source_is_due(self.db, scraper.name):
                logger.info(f"[Orchestrator] Source '{scraper.name}' not due yet (low-yield cadence); skipping this cycle.")
                continue

            t0 = time.time()
            try:
                raw_jobs = scraper.scrape()
                dur = time.time() - t0
                source_timings[scraper.name] = dur
                source_counts[scraper.name] = len(raw_jobs)
                mark_source_run(self.db, scraper.name)
                touch_heartbeat()  # Brief 12 section 1: heartbeat after every source, not only at the start

                record_source_duration(slow_state, scraper.name, dur)

                if not raw_jobs:
                    source_listed[scraper.name] = getattr(scraper, "last_postings_listed", 0)
                    source_details[scraper.name] = getattr(scraper, "last_details_fetched", 0)
                    source_passed[scraper.name] = 0
                    logger.info(
                        f"[Source Stats] {scraper.name}: listed={source_listed[scraper.name]}, details_fetched={source_details[scraper.name]}, passed=0, time={dur:.1f}s"
                    )
                    continue

                for job in raw_jobs:
                    # Track active ATS postings (before dedupe, so closing logic sees every live id)
                    if ":" in job.id:
                        parts = job.id.split(":")
                        if len(parts) == 3:
                            src, slug, _ = parts
                            active_ids_by_ats[src][slug].add(job.id)

                # Brief 12 section 7: dedupe by normalized company + title + location before insert, with row caps
                kept_jobs, dstats = self.db.filter_new_duplicates(raw_jobs)
                for k_, v_ in dstats.items():
                    intake_totals[k_] = intake_totals.get(k_, 0) + v_

                p_before = passed_count
                source_evaluated = []
                for job in kept_jobs:
                    # A posting just returned by an ATS scraper in this cycle is already confirmed live
                    if job.last_liveness_check is None and ":" in job.id and len(job.id.split(":")) == 3:
                        job.last_liveness_check = datetime.now(timezone.utc)
                    evaluated = self._evaluate_with_fix(job, fixer)
                    source_evaluated.append(evaluated)
                    all_evaluated_jobs.append(evaluated)

                    if evaluated.gate_rejected:
                        gate_counts[evaluated.gate_rejected] += 1
                    else:
                        passed_count += 1

                # Brief 14 A1: Persist greenhouse and ashby rows immediately so they reach jobs.db earliest
                if scraper.name in ("greenhouse", "ashby") and source_evaluated:
                    self.db.save_jobs(source_evaluated)

                source_listed[scraper.name] = getattr(scraper, "last_postings_listed", len(raw_jobs))
                source_details[scraper.name] = getattr(scraper, "last_details_fetched", 0)
                source_passed[scraper.name] = passed_count - p_before
                logger.info(
                    f"[Source Stats] {scraper.name}: listed={source_listed[scraper.name]}, details_fetched={source_details[scraper.name]}, passed={source_passed[scraper.name]}, time={dur:.1f}s"
                )

            except Exception as exc:
                source_errors[scraper.name] += 1
                logger.error(f"Error executing scraper {scraper.name}: {exc}", exc_info=True)

        logger.info(
            f"[Vague Location Budget] Fetched {self.matcher.vague_fetches_this_cycle}/{self.matcher.max_vague_fetches_per_cycle} descriptions for vague-location postings this cycle."
        )

        self.db.set_metadata("source_slow_state", json.dumps(slow_state))
        if intake_totals.get("duplicate_triple"):
            gate_counts["dup_triple_at_intake"] += intake_totals["duplicate_triple"]
        if intake_totals.get("company_cap"):
            gate_counts["company_row_cap"] += intake_totals["company_cap"]

        # 3. Batch persist into SQLite
        self.db.save_jobs(all_evaluated_jobs)
        logger.info(f"Persisted {len(all_evaluated_jobs)} postings to database.")

        # Brief 12 section 7: repair passing rows that still have no description (bounded fetches), then re-evaluate
        self._repair_passing_without_description(fixer, gate_counts=gate_counts)
        intake_totals["fixed_description"] = fixer.fixed_description
        intake_totals["fixed_link"] = fixer.fixed_link
        self._intake_stats = {**intake_totals, "fetches": fixer.fetches, "unfixable": fixer.failed}
        fixer.close()
        touch_heartbeat()

        # 4. Closed postings tracking
        total_closed = 0
        for src, slugs_dict in active_ids_by_ats.items():
            for slug, active_ids in slugs_dict.items():
                closed_count = self.db.close_missing_ats_postings(src, slug, active_ids)
                total_closed += closed_count

        stale_agg_closed = self.db.close_stale_aggregator_postings(max_days=21)
        total_closed += stale_agg_closed
        if total_closed > 0:
            logger.info(f"[Closed] Marked {total_closed} missing/stale postings as closed.")

        # 5. Cross-source deduplication (Brief 13 Section 1: re-evaluate canonical with matcher)
        duplicates_marked = self.db.deduplicate_all_jobs(matcher=self.matcher)
        if duplicates_marked:
            gate_counts["cross_source_dup"] += duplicates_marked

        # Re-verify any rows that deduplicate_all_jobs() promoted from duplicate_of:... to canonical (gate_rejected=None)
        post_dedupe_fixer = IntakeFixer(max_fetches=50, db=self.db)
        self._repair_passing_without_description(post_dedupe_fixer, gate_counts=gate_counts)
        post_dedupe_fixer.close()

        # 6. Authoritative Liveness Verification on candidates
        liveness_engine = LivenessEngine(self.db, concurrency=15)
        liveness_res = liveness_engine.check_candidates(limit=150)
        if liveness_res["closed"] > 0:
            total_closed += liveness_res["closed"]
            logger.info(f"[Liveness] Verified candidates: {liveness_res['closed']} marked closed.")

        with self.db._get_connection() as conn:
            passed_total = conn.execute(
                "SELECT COUNT(*) FROM jobs WHERE gate_rejected IS NULL AND score > 0 AND closed_at IS NULL"
            ).fetchone()[0]

            cur_k = conn.execute(
                "SELECT coalesce(apply_kind, 'unknown'), COUNT(*) FROM jobs WHERE gate_rejected IS NULL AND score > 0 AND closed_at IS NULL GROUP BY apply_kind"
            )
            by_apply_kind = {row[0]: row[1] for row in cur_k.fetchall()}

            cur_s = conn.execute(
                "SELECT source, COUNT(*) FROM jobs WHERE gate_rejected IS NULL AND score > 0 AND closed_at IS NULL GROUP BY source"
            )
            by_source = {row[0]: row[1] for row in cur_s.fetchall()}

            cur_dt = conn.execute(
                "SELECT coalesce(posted_at, created_at) FROM jobs WHERE gate_rejected IS NULL AND score > 0 AND closed_at IS NULL ORDER BY coalesce(posted_at, created_at) DESC LIMIT 1"
            )
            row_dt = cur_dt.fetchone()
            newest_posting_iso = row_dt[0] if row_dt and row_dt[0] else datetime.now(timezone.utc).isoformat()

        exported_count = 0

        # 8. High-scoring unnotified matches (canonical, score >= 60, posted <= 3 days, not closed)
        matches_to_alert = self.db.get_unnotified_matches(min_score=60.0, max_age_days=3, limit=10)
        if matches_to_alert:
            self.notifier.notify(matches_to_alert)
            self.db.mark_as_notified([j.id for j in matches_to_alert])

        # 9. Brief 12 Section 2 & Brief 14 A4: Daily company discovery across seed lists (linkedin_employers.json first, max_names=600)
        new_boards_found = 0
        try:
            disc_stats = run_daily_discovery(self.db, on_progress=touch_heartbeat, max_names=600)
            if not disc_stats.get("skipped"):
                new_boards_found = int(disc_stats.get("hits", 0)) + int(disc_stats.get("imported_known", 0))
        except Exception as disc_exc:
            logger.warning(f"[Discovery] Daily company discovery error: {disc_exc}")
        touch_heartbeat()

        # 10. Record cycle metrics
        duration_s = (datetime.now(timezone.utc) - start_time).total_seconds()
        linkedin_pauses = 0

        self.db.record_cycle_metrics(
            started_at=start_time.isoformat(),
            duration_s=duration_s,
            per_source_postings=dict(source_counts),
            per_source_errors=dict(source_errors),
            new_rows=len(all_evaluated_jobs),
            rejected_gates=dict(gate_counts),
            passed=passed_count,
            closed=total_closed,
            new_boards_found=new_boards_found,
            linkedin_pauses=linkedin_pauses,
            per_source_timing=dict(source_timings),
            passed_total=passed_total,
            by_apply_kind=by_apply_kind,
            by_source=by_source,
            exported=exported_count,
        )

        heartbeat_file = config.db_path.parent / "daemon.heartbeat"
        heartbeat_file.write_text(datetime.now(timezone.utc).isoformat(), encoding="utf-8")

        # Write data/heartbeat.json
        heartbeat_json = DATA_DIR / "heartbeat.json"
        DATA_DIR.mkdir(parents=True, exist_ok=True)
        hb_payload = {
            "time": datetime.now(timezone.utc).isoformat(),
            "cycle_seconds": round(duration_s, 1),
            "passed_total": passed_total,
            "newest_posting": newest_posting_iso,
        }
        heartbeat_json.write_text(json.dumps(hb_payload, indent=2), encoding="utf-8")

        # Brief 13 Section 5 & Brief 14 B6: WAL checkpoint and daily prune (7 days)
        self.db.checkpoint_wal()
        prune_stats = self.db.prune_stale_rejected_jobs(max_age_days=7.0)
        if prune_stats.get("pruned", 0) > 0:
            logger.info(f"[Housekeeping] Pruned {prune_stats['pruned']} stale rejected rows.")
        if prune_stats.get("vacuum"):
            logger.info("[Housekeeping] Executed weekly VACUUM.")

        # 11. Print complete Funnel
        self._print_funnel(
            source_counts,
            gate_counts,
            passed_total,
            exported_count,
            source_listed=source_listed,
            source_details=source_details,
            source_passed=source_passed,
            source_timings=source_timings,
        )
        self.print_brief_7_audit(new_boards_found=new_boards_found)

        # 12. Generate interactive HTML run dashboard
        try:
            from dashboard_generator import generate_dashboard
            dash_path = generate_dashboard(self.db)
            print("=" * 80)
            print(" RUN DASHBOARD & IMPROVEMENT ENGINE")
            print("=" * 80)
            print(f"Interactive Dashboard generated: file:///{dash_path.resolve().as_posix()}")
            print("Open in your browser to inspect match analytics and improvement points.")
            print("=" * 80 + "\n")
        except Exception as dash_exc:
            logger.warning(f"Failed to generate dashboard: {dash_exc}")

    def _print_funnel(
        self,
        source_counts: dict[str, int],
        gate_counts: dict[str, int],
        passed_count: int,
        exported_count: int = 0,
        source_listed: Optional[dict[str, int]] = None,
        source_details: Optional[dict[str, int]] = None,
        source_passed: Optional[dict[str, int]] = None,
        source_timings: Optional[dict[str, float]] = None,
    ) -> None:
        total_discovered = sum(source_counts.values())
        print("\n" + "=" * 80)
        print(" PIPELINE FUNNEL SUMMARY")
        print("=" * 80)
        print("Postings Discovered per Source:")
        for src, count in sorted(source_counts.items()):
            l_cnt = (source_listed or {}).get(src, count)
            d_cnt = (source_details or {}).get(src, 0)
            p_cnt = (source_passed or {}).get(src, 0)
            t_s = (source_timings or {}).get(src, 0.0)
            print(f"  - {src:20s}: {count:5d} returned ({l_cnt:5d} listed, {d_cnt:4d} details, {p_cnt:3d} passed, {t_s:5.1f}s)")
        print(f"Total Discovered: {total_discovered:5d}\n")

        print("Filtered by Hard Gates:")
        gate_names = [
            ("dup", "Gate 1: Duplicate (applied_history.csv + cap)"),
            ("location", "Gate 2: Location (Munich / DE-remote / EU-remote)"),
            ("role-rules", "Gate 2a: Role Rules (role_rules.json contract)"),
            ("negative-keyword", "Gate 2b: Negative Keywords (config.py whole-word)"),
            ("seniority", "Gate 3: Seniority (Director/Head/Lead/Intern)"),
            ("german", "Gate 4: German (DACH in title / German text)"),
            ("german_required", "Gate 4a: German Required (English posting requires DE)"),
            ("language", "Gate 4b: Language (FR/SV/NL/ES/IT/PL text)"),
            ("years_required", "Gate 4c: Years Required (softened to score penalty)"),
            ("contract", "Gate 4d: Contract (fixed-term/temp/part-time/intern)"),
            ("deep_stack", "Gate 4e: Deep Stack (K8s/Terraform/ABAP/C++/kernel)"),
            ("clearance", "Gate 4f: Clearance (SÜG/SC/DV/German citizenship)"),
            ("skip-list", "Gate 5: Skip-list (ger_skip_list.csv + staffing)"),
            ("off-function", "Gate 6: Off-function title"),
            ("age", "Gate 7: Age (> 60 days or > 14d closed)"),
            ("account-only", "Gate 8: Account-only aggregators"),
            ("no-employer-link", "Gate 9: No employer link (Aggregator)"),
            ("no-description", "Gate 10: No description (< 300 chars)"),
            ("cross_source_dup", "Gate 11: Cross-source duplicate"),
            ("dup_triple_at_intake", "Intake: Duplicate triple (company+title+location)"),
            ("company_row_cap", "Intake: Per-company duplicate row cap"),
        ]
        for key, label in gate_names:
            print(f"  - {label:55s}: {gate_counts[key]:5d}")

        print(f"\nPassed All Gates: {passed_count:5d}")
        print("=" * 80 + "\n")

    def check_liveness(self, limit: Optional[int] = None) -> dict:
        """Run standalone authoritative liveness verification across candidate postings."""
        print("\n>>> Running Authoritative Liveness Verification Engine...")
        engine = LivenessEngine(self.db, concurrency=15)
        results = engine.check_candidates(limit=limit)
        print(f"\n[Liveness Audit Results]")
        print(f"  - Total Candidates Checked: {results['total']}")
        print(f"  - Verified Active / Live:  {results['live']}")
        print(f"  - Marked Closed / Expired: {results['closed']}")
        if results['closed_jobs']:
            print(f"\nSample newly closed jobs:")
            for jid, tit, comp, reason in results['closed_jobs'][:10]:
                print(f"    * [{comp}] {tit} ({jid}) -> {reason}")
        print("=" * 60 + "\n")
        return results


def main():
    parser = argparse.ArgumentParser(description="munichScraper - Discovery Layer for Mattia Carone")
    parser.add_argument(
        "--run-once",
        action="store_true",
        help="Run a single evaluation cycle immediately and print funnel.",
    )
    parser.add_argument(
        "--rescore",
        action="store_true",
        help="Re-apply gates and scoring to all existing database jobs.",
    )
    parser.add_argument(
        "--health",
        action="store_true",
        help="Print metrics from the last 10 cycles, candidate breakdowns, source yield, and run health checks.",
    )
    parser.add_argument(
        "--prune",
        action="store_true",
        help="Delete rejected jobs older than 7 days and run VACUUM.",
    )
    parser.add_argument(
        "--check-liveness",
        action="store_true",
        help="Run authoritative ATS & HTTP liveness verification on candidate jobs.",
    )
    parser.add_argument(
        "--web",
        action="store_true",
        help="Start local web dashboard on http://127.0.0.1:8000.",
    )
    parser.add_argument(
        "--dashboard",
        action="store_true",
        help="Generate interactive HTML run dashboard (dashboard.html).",
    )
    parser.add_argument(
        "--port",
        type=int,
        default=8000,
        help="Port for local web dashboard (default 8000).",
    )
    args = parser.parse_args()

    engine = JobEngine()

    if args.dashboard:
        from dashboard_generator import generate_dashboard
        out_p = generate_dashboard(engine.db)
        print(f"\n[Dashboard] Run dashboard generated: file:///{out_p.resolve().as_posix()}")
        return

    if args.prune:
        deleted = engine.db.prune_rejected_jobs(max_age_days=7)
        print(f"Pruned {deleted} rejected jobs older than 7 days.")
        return

    if args.health:
        engine.print_health()
        return

    if args.check_liveness:
        engine.check_liveness()
        return

    if args.web:
        run_server(port=args.port, db=engine.db)
        return

    if args.rescore:
        engine.rescore_all_jobs()
        return

    if args.run_once:
        logger.info("Executing one-off run mode...")
        engine.run_pipeline()
        return

    # Brief 12 Section 1: Run cycles back-to-back (start next cycle after the previous one ends)
    # instead of overlapping IntervalTrigger schedules that skip with "maximum number of running instances reached (1)".
    write_pid()
    touch_heartbeat()
    logger.info(
        f"Daemon armed in back-to-back cycle mode (pause {CYCLE_PAUSE_SECONDS}s between cycles; PID written to daemon.pid)."
    )
    try:
        run_forever(engine.run_pipeline, pause_seconds=CYCLE_PAUSE_SECONDS)
    except (KeyboardInterrupt, SystemExit):
        logger.info("Shutdown signal received. Stopping daemon cleanly.")


if __name__ == "__main__":
    main()
