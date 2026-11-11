"""
Persistence layer using SQLite.
Handles atomic insertions, schema migrations, state tracking, export tracking,
cross-source deduplication, closed postings, company display caching, and metrics.
"""

import json
import logging
import re
import sqlite3
from collections import defaultdict
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Optional, Tuple
from models import JobPosting

logger = logging.getLogger(__name__)

VALID_ID_PATTERN = re.compile(
    r"^(?:(?:greenhouse|ashby|lever|personio|workable|recruitee|smartrecruiters|bamboohr|teamtailor|softgarden|join|breezy|pinpoint|factorial|comeet|vc|workday|amazon_jobs|arbeitsagentur|eures|remoteok):[^:]+:[^:]+|li:[^:]+|arbeitnow:[^:]+|jobicy:[^:]+|stepstone:[^:]+|xing:[^:]+|germantechjobs:[^:]+|englishjobsingermany:[^:]+|wellfound:[^:]+|remotive:[^:]+|himalayas:[^:]+|weworkremotely:[^:]+|workingnomads:[^:]+|workday:[^:]+|amazon_jobs:[^:]+|arbeitsagentur:[^:]+|eures:[^:]+|remoteok:[^:]+)$"
)


def compute_dedupe_key(company: str, title: str) -> str:
    """
    Normalised company + normalised title:
    lowercase; strip "(m/w/d)", "(f/m/d)", "(all genders)", "(w/m/d)", "(m/f/x)"
    and anything in brackets; keep a-z0-9.
    """
    def clean(text: str) -> str:
        if not text:
            return ""
        t = text.lower()
        t = re.sub(r"[\(\[\{][^\)\]\}]*[\)\]\}]", "", t)
        t = re.sub(r"\b(m/w/d|f/m/d|w/m/d|m/f/x|all\s+genders|gn)\b", "", t, flags=re.I)
        return re.sub(r"[^a-z0-9]", "", t)

    return f"{clean(company)}{clean(title)}"


class Database:
    def __init__(self, db_path: Path):
        self.db_path = db_path
        self._init_db()
        self.backfill_company_display()
        self.clean_rejected_descriptions()
        self.clean_linkedin_apply_urls()

    def _get_connection(self) -> sqlite3.Connection:
        conn = sqlite3.connect(self.db_path)
        conn.execute("PRAGMA journal_mode = WAL;")
        conn.execute("PRAGMA synchronous = NORMAL;")
        conn.row_factory = sqlite3.Row
        return conn

    def _init_db(self) -> None:
        """Create necessary tables and handle schema migrations."""
        with self._get_connection() as conn:
            conn.execute(
                """
                CREATE TABLE IF NOT EXISTS jobs (
                    id TEXT PRIMARY KEY,
                    title TEXT NOT NULL,
                    company TEXT NOT NULL,
                    location TEXT,
                    location_source TEXT,
                    url TEXT NOT NULL,
                    description TEXT,
                    source TEXT,
                    score REAL DEFAULT 0.0,
                    matched_skills TEXT,
                    matched_role TEXT,
                    matched_categories TEXT,
                    language_status TEXT,
                    fit_summary TEXT,
                    gate_rejected TEXT,
                    rejection_reason TEXT,
                    is_notified INTEGER DEFAULT 0,
                    exported_at TEXT,
                    posted_at TEXT,
                    created_at TEXT NOT NULL,
                    dedupe_key TEXT,
                    company_display TEXT,
                    apply_url TEXT,
                    ats TEXT,
                    ats_slug TEXT,
                    closed_at TEXT
                );
                """
            )
            conn.execute(
                """
                CREATE TABLE IF NOT EXISTS scraper_metadata (
                    key TEXT PRIMARY KEY,
                    value TEXT NOT NULL
                );
                """
            )
            conn.execute(
                """
                CREATE TABLE IF NOT EXISTS companies (
                    source TEXT NOT NULL,
                    slug TEXT NOT NULL,
                    display_name TEXT NOT NULL,
                    updated_at TEXT NOT NULL,
                    PRIMARY KEY (source, slug)
                );
                """
            )
            conn.execute(
                """
                CREATE TABLE IF NOT EXISTS probed_companies (
                    company_name TEXT PRIMARY KEY,
                    last_probed_at TEXT NOT NULL,
                    found_ats TEXT,
                    found_slug TEXT
                );
                """
            )
            conn.execute(
                """
                CREATE TABLE IF NOT EXISTS query_performance (
                    query TEXT PRIMARY KEY,
                    total_runs INTEGER DEFAULT 0,
                    consecutive_zero_runs INTEGER DEFAULT 0,
                    total_passing INTEGER DEFAULT 0,
                    last_run_at TEXT
                );
                """
            )
            conn.execute(
                """
                CREATE TABLE IF NOT EXISTS cycle_metrics (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    started_at TEXT NOT NULL,
                    duration_s REAL NOT NULL,
                    per_source_postings TEXT,
                    per_source_errors TEXT,
                    new_rows INTEGER DEFAULT 0,
                    rejected_gates TEXT,
                    passed INTEGER DEFAULT 0,
                    closed INTEGER DEFAULT 0,
                    new_boards_found INTEGER DEFAULT 0,
                    linkedin_pauses INTEGER DEFAULT 0
                );
                """
            )

            # Ensure new columns exist for existing databases
            cursor = conn.execute("PRAGMA table_info(jobs);")
            col_rows = cursor.fetchall()
            columns = {row["name"] for row in col_rows}

            # Migrate location TEXT NOT NULL -> location TEXT if needed (Brief 7b)
            for row in col_rows:
                if row["name"] == "location" and row["notnull"] == 1:
                    conn.execute("PRAGMA writable_schema = 1;")
                    conn.execute(
                        "UPDATE sqlite_master SET sql = replace(sql, 'location TEXT NOT NULL', 'location TEXT') WHERE type = 'table' AND name = 'jobs';"
                    )
                    sv = conn.execute("PRAGMA schema_version;").fetchone()[0]
                    conn.execute(f"PRAGMA schema_version = {sv + 1};")
                    conn.execute("PRAGMA writable_schema = 0;")
                    break

            for col, col_type in [
                ("location_source", "TEXT"),
                ("location_scope", "TEXT"),
                ("gate_rejected", "TEXT"),
                ("rejection_reason", "TEXT"),
                ("gate_reason", "TEXT"),
                ("exported_at", "TEXT"),
                ("dedupe_key", "TEXT"),
                ("company_display", "TEXT"),
                ("apply_url", "TEXT"),
                ("ats", "TEXT"),
                ("ats_slug", "TEXT"),
                ("closed_at", "TEXT"),
                ("user_status", "TEXT DEFAULT 'new'"),
                ("applied_at", "TEXT"),
                ("notes", "TEXT"),
                ("apply_kind", "TEXT DEFAULT 'unknown'"),
                ("apply_host", "TEXT"),
                ("apply_route", "TEXT"),
                ("needs_account", "INTEGER DEFAULT 0"),
                ("years_required", "INTEGER"),
                ("contract_type", "TEXT"),
                ("german_required", "INTEGER DEFAULT 0"),
                ("title_weight", "REAL DEFAULT 0.0"),
                ("is_startup", "INTEGER DEFAULT 0"),
                ("startup_source", "TEXT"),
                ("last_liveness_check", "TEXT"),
                ("apply_url_source", "TEXT"),
                ("apply_url_status", "TEXT"),
                ("description_source", "TEXT"),
                ("first_seen", "TEXT"),
                ("triple_key", "TEXT"),
            ]:
                if col not in columns:
                    conn.execute(f"ALTER TABLE jobs ADD COLUMN {col} {col_type};")

            # Ensure probed_companies columns exist
            cur_p = conn.execute("PRAGMA table_info(probed_companies);")
            p_cols = {row["name"] for row in cur_p.fetchall()}
            for col, col_type in [("last_germany_jobs", "INTEGER DEFAULT 0"), ("status", "TEXT")]:
                if col not in p_cols:
                    conn.execute(f"ALTER TABLE probed_companies ADD COLUMN {col} {col_type};")

            # Ensure cycle_metrics columns exist
            cur_cm = conn.execute("PRAGMA table_info(cycle_metrics);")
            cm_cols = {row["name"] for row in cur_cm.fetchall()}
            if "per_source_timing" not in cm_cols:
                conn.execute("ALTER TABLE cycle_metrics ADD COLUMN per_source_timing TEXT;")
            for col, col_type in [
                ("passed_total", "INTEGER DEFAULT 0"),
                ("by_apply_kind", "TEXT"),
                ("by_source", "TEXT"),
                ("exported", "INTEGER DEFAULT 0"),
            ]:
                if col not in cm_cols:
                    conn.execute(f"ALTER TABLE cycle_metrics ADD COLUMN {col} {col_type};")

            conn.execute(
                """
                CREATE TABLE IF NOT EXISTS resolved_apply_urls (
                    url TEXT PRIMARY KEY,
                    final_url TEXT NOT NULL,
                    ats TEXT,
                    ats_slug TEXT,
                    apply_kind TEXT,
                    resolved_at TEXT NOT NULL
                );
                """
            )

            conn.execute("CREATE INDEX IF NOT EXISTS idx_jobs_score ON jobs(score);")
            conn.execute("CREATE INDEX IF NOT EXISTS idx_jobs_notified ON jobs(is_notified);")
            conn.execute("CREATE INDEX IF NOT EXISTS idx_jobs_exported ON jobs(exported_at);")
            conn.execute("CREATE INDEX IF NOT EXISTS idx_jobs_gate ON jobs(gate_rejected);")
            conn.execute("CREATE INDEX IF NOT EXISTS idx_jobs_dedupe ON jobs(dedupe_key);")
            conn.execute("CREATE INDEX IF NOT EXISTS idx_jobs_closed ON jobs(closed_at);")
            conn.execute("CREATE INDEX IF NOT EXISTS idx_jobs_apply_kind ON jobs(apply_kind);")
            conn.execute("CREATE INDEX IF NOT EXISTS idx_jobs_triple ON jobs(triple_key);")

            # Brief 13 Section 5 & Brief 14 B6: seen_keys table for pruned 7d+ rejected rows (id is PRIMARY KEY)
            conn.execute(
                """
                CREATE TABLE IF NOT EXISTS seen_keys (
                    dedupe_key TEXT,
                    url TEXT,
                    id TEXT PRIMARY KEY,
                    seen_at TEXT
                );
                """
            )
            conn.execute("DROP INDEX IF EXISTS idx_seen_keys_dedupe;")
            conn.execute("DROP INDEX IF EXISTS idx_seen_keys_url;")

            # Brief 12 section 7: every row gets a first_seen (backfill from created_at)
            conn.execute("UPDATE jobs SET first_seen = created_at WHERE first_seen IS NULL OR first_seen = ''")

    def backfill_triple_keys(self, batch: int = 20000) -> int:
        """One-off: fill triple_key (normalized company+title+location) for rows that lack it."""
        from intake_checks import triple_key

        done = 0
        with self._get_connection() as conn:
            while True:
                rows = conn.execute(
                    "SELECT id, company, title, location FROM jobs WHERE triple_key IS NULL LIMIT ?", (batch,)
                ).fetchall()
                if not rows:
                    break
                conn.executemany(
                    "UPDATE jobs SET triple_key = ? WHERE id = ?",
                    [(triple_key(r["company"], r["title"], r["location"]), r["id"]) for r in rows],
                )
                done += len(rows)
        return done

    def filter_new_duplicates(self, jobs: list) -> tuple[list, dict]:
        """
        Brief 12 section 7: before insert, drop NEW rows (id not yet in the DB) whose normalized
        company+title+location already exists or repeats inside the batch, and apply the
        per-company row cap. Rows whose id already exists pass through (they are updates).
        """
        from intake_checks import COMPANY_ROW_CAPS, dedupe_and_cap, normalize_part, triple_key

        if not jobs:
            return jobs, {"duplicate_triple": 0, "company_cap": 0}
        ids = [j.id for j in jobs]
        known: set = set()
        with self._get_connection() as conn:
            for i in range(0, len(ids), 500):
                chunk = ids[i : i + 500]
                q = ",".join("?" for _ in chunk)
                known.update(r[0] for r in conn.execute(f"SELECT id FROM jobs WHERE id IN ({q})", chunk))
            new_jobs = [j for j in jobs if j.id not in known]
            keys = list({triple_key(j.company, j.title, j.location) for j in new_jobs})
            existing_by_src: dict = defaultdict(set)
            for i in range(0, len(keys), 500):
                chunk = keys[i : i + 500]
                q = ",".join("?" for _ in chunk)
                for r in conn.execute(f"SELECT DISTINCT triple_key, source FROM jobs WHERE triple_key IN ({q})", chunk):
                    existing_by_src[r[1]].add(r[0])
            # Brief 13 Section 5: drop rows matching seen_keys (pruned 30d+ rejected rows)
            if new_jobs:
                b_ids = [j.id for j in new_jobs]
                b_urls = [j.url for j in new_jobs if j.url]
                b_dks = [j.dedupe_key for j in new_jobs if j.dedupe_key]
                seen_found = set()
                for i in range(0, len(b_ids), 300):
                    chk_ids = b_ids[i : i + 300]
                    q = ",".join("?" for _ in chk_ids)
                    for r in conn.execute(f"SELECT id FROM seen_keys WHERE id IN ({q})", chk_ids):
                        seen_found.add(r[0])
                if seen_found:
                    new_jobs = [j for j in new_jobs if j.id not in seen_found]

            counts: dict = {}
            if any(normalize_part(j.company) in COMPANY_ROW_CAPS for j in new_jobs):
                for r in conn.execute("SELECT company, COUNT(*) c FROM jobs GROUP BY company"):
                    n = normalize_part(r["company"])
                    if n in COMPANY_ROW_CAPS:
                        counts[n] = counts.get(n, 0) + r["c"]
        # Duplicates are only dropped within the same source: a company-ATS copy must still be able to
        # replace a lower-priority LinkedIn/aggregator copy in the cross-source dedupe.
        kept_ids: set = set()
        stats = {"duplicate_triple": 0, "company_cap": 0}
        for src in {j.source for j in new_jobs}:
            grp = [j for j in new_jobs if j.source == src]
            kept_grp, st = dedupe_and_cap(grp, existing_triples=existing_by_src.get(src, set()), existing_company_counts=counts)
            kept_ids.update(j.id for j in kept_grp)
            stats["duplicate_triple"] += st["duplicate_triple"]
            stats["company_cap"] += st["company_cap"]
        out = [j for j in jobs if j.id in known or j.id in kept_ids]
        return out, stats

    def stamp_triple_keys(self, jobs: list) -> None:
        from intake_checks import triple_key

        if not jobs:
            return
        with self._get_connection() as conn:
            conn.executemany(
                "UPDATE jobs SET triple_key = ? WHERE id = ?",
                [(triple_key(j.company, j.title, j.location), j.id) for j in jobs],
            )

    def passing_without_description(self, min_chars: int = 300, limit: int = 200) -> list:
        """Rows that currently pass the contract but have no/short description."""
        with self._get_connection() as conn:
            rows = conn.execute(
                """SELECT * FROM jobs WHERE gate_rejected IS NULL AND score > 0 AND closed_at IS NULL
                   AND length(coalesce(description, '')) < ? LIMIT ?""",
                (min_chars, limit),
            ).fetchall()
        return [self._row_to_job(r) for r in rows]

    def clean_legacy_rows(self) -> int:
        """
        One-off migration: delete DB rows whose id is not in the
        {source}:{slug}:{postingId} / li:{id} / arbeitnow:{slug} / jobicy:{id} form.
        """
        with self._get_connection() as conn:
            cursor = conn.execute("SELECT id FROM jobs")
            all_ids = [row["id"] for row in cursor.fetchall()]
            invalid_ids = [i for i in all_ids if not VALID_ID_PATTERN.match(i)]
            if not invalid_ids:
                logger.info("[Migration] No legacy DB rows found to clean.")
                return 0

            chunk_size = 500
            for i in range(0, len(invalid_ids), chunk_size):
                chunk = invalid_ids[i:i + chunk_size]
                placeholders = ",".join("?" for _ in chunk)
                conn.execute(f"DELETE FROM jobs WHERE id IN ({placeholders})", chunk)

            logger.info(f"[Migration] Cleaned {len(invalid_ids)} legacy DB rows with invalid IDs.")
            return len(invalid_ids)

    def filter_unseen_ids(self, candidate_ids: list[str]) -> set[str]:
        """Check candidate IDs against the database. Returns unseen IDs."""
        if not candidate_ids:
            return set()

        with self._get_connection() as conn:
            seen_ids = set()
            chunk_size = 500
            for i in range(0, len(candidate_ids), chunk_size):
                chunk = candidate_ids[i:i + chunk_size]
                placeholders = ",".join("?" for _ in chunk)
                cursor = conn.execute(
                    f"SELECT id FROM jobs WHERE id IN ({placeholders})",
                    chunk,
                )
                seen_ids.update(row["id"] for row in cursor.fetchall())

            return set(candidate_ids) - seen_ids

    def save_jobs(self, jobs: list[JobPosting]) -> int:
        """Batch persist job postings in a single transaction."""
        if not jobs:
            return 0

        with self._get_connection() as conn:
            params = [
                (
                    job.id,
                    job.title,
                    job.company,
                    job.location,
                    job.location_source or ("unknown" if job.location is None else "source"),
                    job.location_scope,
                    job.url,
                    (job.description or None)
                    if (job.gate_rejected is None or job.gate_rejected in ("no-description", "no-employer-link"))
                    else None,
                    job.source,
                    job.score,
                    json.dumps(job.matched_skills),
                    job.matched_role,
                    json.dumps(job.matched_categories),
                    job.language_status,
                    job.fit_summary,
                    job.gate_rejected,
                    job.rejection_reason or job.gate_reason,
                    job.gate_reason or job.rejection_reason,
                    1 if job.is_notified else 0,
                    job.exported_at.isoformat() if job.exported_at else None,
                    (job.posted_at or job.created_at).isoformat() if (job.posted_at or job.created_at) else None,
                    job.created_at.isoformat(),
                    job.dedupe_key or compute_dedupe_key(job.company, job.title),
                    job.company_display or job.company or (job.ats_slug.replace("-", " ").title() if job.ats_slug else "Unknown Company"),
                    job.apply_url,
                    job.ats,
                    job.ats_slug,
                    job.closed_at.isoformat() if job.closed_at else None,
                    job.user_status or "new",
                    job.applied_at.isoformat() if job.applied_at else None,
                    job.notes,
                    job.apply_kind or "unknown",
                    job.apply_host,
                    job.apply_route or "unknown",
                    1 if job.needs_account else 0,
                    job.years_required,
                    job.contract_type or "unknown",
                    1 if job.german_required else 0,
                    float(job.title_weight or 0.0),
                    1 if job.is_startup else 0,
                    job.startup_source,
                    job.last_liveness_check.isoformat() if job.last_liveness_check else None,
                    job.apply_url_source,
                    job.apply_url_status,
                    job.description_source,
                    (job.first_seen or job.created_at).isoformat() if (job.first_seen or job.created_at) else None,
                )
                for job in jobs
            ]
            cursor = conn.executemany(
                """
                INSERT INTO jobs (
                    id, title, company, location, location_source, location_scope, url, description,
                    source, score, matched_skills, matched_role,
                    matched_categories, language_status, fit_summary,
                    gate_rejected, rejection_reason, gate_reason,
                    is_notified, exported_at, posted_at, created_at,
                    dedupe_key, company_display, apply_url, ats, ats_slug, closed_at,
                    user_status, applied_at, notes, apply_kind, apply_host,
                    apply_route, needs_account, years_required, contract_type,
                    german_required, title_weight,
                    is_startup, startup_source, last_liveness_check,
                    apply_url_source, apply_url_status, description_source,
                    first_seen
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                ON CONFLICT(id) DO UPDATE SET
                    title = excluded.title,
                    company = excluded.company,
                    location = excluded.location,
                    location_source = excluded.location_source,
                    location_scope = excluded.location_scope,
                    url = excluded.url,
                    description = CASE WHEN excluded.description != '' THEN excluded.description ELSE jobs.description END,
                    source = excluded.source,
                    score = excluded.score,
                    matched_skills = excluded.matched_skills,
                    matched_role = excluded.matched_role,
                    matched_categories = excluded.matched_categories,
                    language_status = excluded.language_status,
                    fit_summary = excluded.fit_summary,
                    gate_rejected = excluded.gate_rejected,
                    rejection_reason = excluded.rejection_reason,
                    gate_reason = excluded.gate_reason,
                    posted_at = coalesce(jobs.posted_at, excluded.posted_at, jobs.created_at),
                    dedupe_key = excluded.dedupe_key,
                    company_display = excluded.company_display,
                    apply_url = coalesce(excluded.apply_url, jobs.apply_url),
                    ats = coalesce(excluded.ats, jobs.ats),
                    ats_slug = coalesce(excluded.ats_slug, jobs.ats_slug),
                    closed_at = coalesce(jobs.closed_at, excluded.closed_at),
                    user_status = coalesce(jobs.user_status, excluded.user_status, 'new'),
                    applied_at = coalesce(jobs.applied_at, excluded.applied_at),
                    notes = coalesce(jobs.notes, excluded.notes),
                    apply_kind = coalesce(excluded.apply_kind, jobs.apply_kind, 'unknown'),
                    apply_host = coalesce(excluded.apply_host, jobs.apply_host),
                    apply_route = coalesce(excluded.apply_route, jobs.apply_route, 'unknown'),
                    needs_account = coalesce(excluded.needs_account, jobs.needs_account, 0),
                    years_required = excluded.years_required,
                    contract_type = coalesce(excluded.contract_type, jobs.contract_type, 'unknown'),
                    german_required = coalesce(excluded.german_required, jobs.german_required, 0),
                    title_weight = excluded.title_weight,
                    is_startup = coalesce(excluded.is_startup, jobs.is_startup, 0),
                    startup_source = coalesce(excluded.startup_source, jobs.startup_source),
                    last_liveness_check = coalesce(excluded.last_liveness_check, jobs.last_liveness_check),
                    apply_url_source = coalesce(excluded.apply_url_source, jobs.apply_url_source),
                    apply_url_status = coalesce(excluded.apply_url_status, jobs.apply_url_status),
                    description_source = coalesce(excluded.description_source, jobs.description_source),
                    first_seen = coalesce(jobs.first_seen, excluded.first_seen, jobs.created_at)
                """,
                params,
            )
            try:
                from intake_checks import triple_key as _tk

                conn.executemany(
                    "UPDATE jobs SET triple_key = ? WHERE id = ?",
                    [(_tk(j.company, j.title, j.location), j.id) for j in jobs],
                )
            except Exception as exc:  # noqa: BLE001
                logger.debug(f"[DB] triple_key stamp failed: {exc}")
            return cursor.rowcount

    def deduplicate_all_jobs(self, matcher=None) -> int:
        """
        Cross-source deduplication:
        When two rows share a dedupe_key, keep ONE as the canonical row and
        set gate_rejected = 'duplicate_of:<id>' on the others.
        Priority: company ATS (greenhouse/ashby/lever/personio/workable/recruitee/smartrecruiters) > arbeitnow/jobicy > LinkedIn.
        Brief 13 Section 1 fix: Only deduplicate active passing or duplicate_of rows. Never unmask
        hard-rejected rows (location, role-rules, seniority, etc.) to gate_rejected = None.
        """
        with self._get_connection() as conn:
            cursor = conn.execute(
                "SELECT * FROM jobs WHERE (gate_rejected IS NULL OR gate_rejected LIKE 'duplicate_of:%') AND closed_at IS NULL"
            )
            rows = cursor.fetchall()

        by_key = defaultdict(list)
        for r in rows:
            dk = r["dedupe_key"] or compute_dedupe_key(r["company"], r["title"])
            if dk:
                by_key[dk].append(self._row_to_job(r))

        ATS_SOURCES = {
            "greenhouse", "ashby", "lever", "personio", "workable", "recruitee",
            "smartrecruiters", "bamboohr", "teamtailor", "softgarden", "join",
            "breezy", "pinpoint", "factorial", "comeet",
        }
        ENGLISH_BOARDS = {"germantechjobs", "englishjobsingermany", "wellfound", "vc"}
        GERMAN_BOARDS = {"stepstone", "xing"}
        AGGREGATOR_SOURCES = {"arbeitnow", "jobicy", "remotive", "himalayas", "weworkremotely", "workingnomads"}

        def source_priority(src: str) -> int:
            s = (src or "").lower()
            if s in ATS_SOURCES:
                return 1
            if s in ENGLISH_BOARDS:
                return 2
            if s in GERMAN_BOARDS:
                return 3
            if s in {"linkedin", "li"}:
                return 4
            if s in AGGREGATOR_SOURCES:
                return 5
            return 6

        duplicates_marked = 0
        updates_to_make = []

        for dk, group in by_key.items():
            if len(group) <= 1:
                continue

            def sort_key(j: JobPosting):
                is_failed_gate = 1 if (j.gate_rejected and not j.gate_rejected.startswith("duplicate_of:")) else 0
                score = float(j.score or 0.0)
                dt = j.posted_at or j.created_at
                ts = dt.timestamp() if dt else 0.0
                return (is_failed_gate, source_priority(j.source), -score, -ts)

            sorted_group = sorted(group, key=sort_key)
            canonical = sorted_group[0]

            # If canonical was previously marked duplicate_of:..., unmark it (re-evaluating through matcher if present)
            if canonical.gate_rejected and canonical.gate_rejected.startswith("duplicate_of:"):
                if matcher is not None:
                    ev = matcher.evaluate(canonical)
                    if ev.gate_rejected:
                        updates_to_make.append((canonical.id, ev.gate_rejected, ev.gate_reason))
                    else:
                        updates_to_make.append((canonical.id, None, None))
                else:
                    updates_to_make.append((canonical.id, None, None))

            for other in sorted_group[1:]:
                expected_rejection = f"duplicate_of:{canonical.id}"
                if (other.gate_rejected is None or other.gate_rejected.startswith("duplicate_of:")) and other.gate_rejected != expected_rejection and other.id != canonical.id:
                    updates_to_make.append((other.id, expected_rejection, f"Cross-source duplicate of {canonical.id}"))
                    duplicates_marked += 1

        # Second pass: Dedupe by resolved employer apply_url
        EXCLUDED_APPLY_DOMAINS = {
            "linkedin.com", "arbeitnow.com", "stepstone.de", "stepstone.com",
            "xing.com", "wellfound.com", "germantechjobs.de", "englishjobsingermany.com"
        }
        by_url = defaultdict(list)
        for r in rows:
            job = self._row_to_job(r)
            au = (job.apply_url or "").strip()
            if au and not any(d in au.lower() for d in EXCLUDED_APPLY_DOMAINS):
                try:
                    p = urllib.parse.urlparse(au)
                    netloc = (p.netloc or "").lower()
                    path = p.path.rstrip("/").lower()
                    if netloc and len(path.strip("/")) >= 3:
                        by_url[f"{netloc}{path}"].append(job)
                except Exception:
                    pass

        for clean_u, u_group in by_url.items():
            if len(u_group) <= 1:
                continue

            sorted_u_group = sorted(u_group, key=sort_key)
            canonical = sorted_u_group[0]

            for other in sorted_u_group[1:]:
                expected_rejection = f"duplicate_of:{canonical.id}"
                if (other.gate_rejected is None or other.gate_rejected.startswith("duplicate_of:")) and other.gate_rejected != expected_rejection and other.id != canonical.id:
                    updates_to_make.append((other.id, expected_rejection, f"Resolved apply_url duplicate of {canonical.id}"))
                    duplicates_marked += 1

        if updates_to_make:
            with self._get_connection() as conn:
                conn.executemany(
                    "UPDATE jobs SET gate_rejected = ?, rejection_reason = ?, gate_reason = CASE WHEN ? IS NULL THEN NULL ELSE ? END WHERE id = ?",
                    [(rej, reason, rej, reason, jid) for jid, rej, reason in updates_to_make],
                )
            logger.info(
                f"[Dedupe] Cross-source deduplication updated {len(updates_to_make)} rows ({duplicates_marked} new duplicates)."
            )

        return duplicates_marked

    def close_missing_ats_postings(self, source: str, slug: str, active_ids: set[str]) -> int:
        """
        A posting that was listed on its ATS board before but is no longer in the board's
        API response gets closed_at = now.
        """
        now_str = datetime.now(timezone.utc).isoformat()
        pattern = f"{source}:{slug}:%"
        with self._get_connection() as conn:
            cursor = conn.execute(
                "SELECT id FROM jobs WHERE id LIKE ? AND closed_at IS NULL",
                (pattern,),
            )
            existing_ids = {row["id"] for row in cursor.fetchall()}
            to_close = existing_ids - active_ids
            if not to_close:
                return 0

            placeholders = ",".join("?" for _ in to_close)
            conn.execute(
                f"UPDATE jobs SET closed_at = ? WHERE id IN ({placeholders})",
                [now_str] + list(to_close),
            )
            return len(to_close)

    def close_stale_aggregator_postings(self, max_days: int = 21) -> int:
        """
        LinkedIn/Arbeitnow rows older than 21 days get closed_at too.
        """
        now = datetime.now(timezone.utc)
        now_str = now.isoformat()
        with self._get_connection() as conn:
            cursor = conn.execute(
                "SELECT id, posted_at, created_at FROM jobs WHERE source IN ('linkedin', 'arbeitnow') AND closed_at IS NULL"
            )
            rows = cursor.fetchall()
            to_close = []
            for r in rows:
                dt_str = r["posted_at"] or r["created_at"]
                if dt_str:
                    try:
                        dt = datetime.fromisoformat(dt_str)
                        tz_dt = dt if dt.tzinfo else dt.replace(tzinfo=timezone.utc)
                        if (now - tz_dt).total_seconds() / 86400.0 > max_days:
                            to_close.append(r["id"])
                    except Exception:
                        pass

            if not to_close:
                return 0

            chunk_size = 500
            for i in range(0, len(to_close), chunk_size):
                chunk = to_close[i:i + chunk_size]
                placeholders = ",".join("?" for _ in chunk)
                conn.execute(
                    f"UPDATE jobs SET closed_at = ? WHERE id IN ({placeholders})",
                    [now_str] + chunk,
                )
            return len(to_close)

    def get_company_display(self, source: str, slug: str) -> Optional[str]:
        with self._get_connection() as conn:
            cursor = conn.execute(
                "SELECT display_name FROM companies WHERE source = ? AND slug = ?",
                (source, slug),
            )
            row = cursor.fetchone()
            return row["display_name"] if row else None

    def save_company_display(self, source: str, slug: str, display_name: str) -> None:
        now_str = datetime.now(timezone.utc).isoformat()
        with self._get_connection() as conn:
            conn.execute(
                "INSERT OR REPLACE INTO companies (source, slug, display_name, updated_at) VALUES (?, ?, ?, ?)",
                (source, slug, display_name, now_str),
            )

    def get_unprobed_companies(self, max_count: int = 300, max_age_days: int = 30) -> list[str]:
        """
        Get unique company names from jobs that haven't been probed in the last 30 days.
        """
        cutoff = (datetime.now(timezone.utc) - timedelta(days=max_age_days)).isoformat()
        with self._get_connection() as conn:
            cursor = conn.execute(
                """
                SELECT DISTINCT company FROM jobs
                WHERE company NOT IN (
                    SELECT company_name FROM probed_companies WHERE last_probed_at > ?
                )
                ORDER BY created_at DESC
                LIMIT ?
                """,
                (cutoff, max_count),
            )
            return [row["company"] for row in cursor.fetchall() if row["company"]]

    def record_probed_company(
        self,
        company_name: str,
        found_ats: Optional[str] = None,
        found_slug: Optional[str] = None,
        last_germany_jobs: int = 0,
        status: Optional[str] = None,
    ) -> None:
        now_str = datetime.now(timezone.utc).isoformat()
        with self._get_connection() as conn:
            conn.execute(
                """
                INSERT OR REPLACE INTO probed_companies (
                    company_name, last_probed_at, found_ats, found_slug, last_germany_jobs, status
                ) VALUES (?, ?, ?, ?, ?, ?)
                """,
                (company_name, now_str, found_ats, found_slug, last_germany_jobs, status),
            )

    def backfill_company_display(self) -> int:
        """
        Backfill company_display for existing rows where company_display IS NULL or empty:
        - ATS rows from cached company display or title-cased slug.
        - Arbeitnow/Jobicy/LinkedIn rows use company.
        - Fallback: slug title-cased or 'Unknown Company'. Never leave NULL.
        """
        with self._get_connection() as conn:
            cursor = conn.execute(
                "SELECT id, source, company, ats_slug FROM jobs WHERE company_display IS NULL OR company_display = ''"
            )
            rows = cursor.fetchall()
            if not rows:
                return 0

            updates = []
            for r in rows:
                jid = r["id"]
                src = (r["source"] or "").lower()
                comp = (r["company"] or "").strip()
                slug = (r["ats_slug"] or "").strip()
                if not slug and ":" in jid:
                    parts = jid.split(":")
                    if len(parts) >= 2 and parts[0] == src:
                        slug = parts[1]

                display = None
                if src in {"greenhouse", "ashby", "lever", "personio", "workable", "recruitee", "smartrecruiters"} and slug:
                    display = self.get_company_display(src, slug)
                    if not display:
                        display = slug.replace("-", " ").replace("_", " ").title()
                elif comp:
                    display = comp
                elif slug:
                    display = slug.replace("-", " ").replace("_", " ").title()
                else:
                    display = "Unknown Company"

                updates.append((display, jid))

            conn.executemany("UPDATE jobs SET company_display = ? WHERE id = ?", updates)
            logger.info(f"[Backfill] Updated company_display for {len(updates)} rows.")
            return len(updates)

    def clean_rejected_descriptions(self) -> int:
        """
        Brief 14 B6: Keep description text only for rows that pass gates (gate_rejected IS NULL)
        or wait for A2/intake decision. Set description = NULL for rows rejected on location,
        role-rules, negative-keyword, seniority, german, skip-list, account-only, or duplicate.
        """
        with self._get_connection() as conn:
            cursor = conn.execute(
                """
                UPDATE jobs
                SET description = NULL
                WHERE description IS NOT NULL
                  AND (
                      gate_rejected IN (
                          'location', 'role-rules', 'negative-keyword', 'seniority',
                          'german', 'german_required', 'skip-list', 'account-only',
                          'dup', 'off-function', 'contract', 'deep_stack', 'clearance',
                          'language', 'clera', 'age'
                      )
                      OR gate_rejected LIKE 'duplicate_of:%'
                  )
                """
            )
            return cursor.rowcount

    def clean_linkedin_apply_urls(self) -> int:
        """
        LinkedIn rows must not have apply_url pointing to linkedin.com.
        If apply_url contains linkedin.com, set apply_url = NULL and ats = 'linkedin-easy-apply'.
        """
        with self._get_connection() as conn:
            cursor = conn.execute(
                """
                UPDATE jobs
                SET apply_url = NULL,
                    ats = COALESCE(ats, 'linkedin-easy-apply')
                WHERE source = 'linkedin'
                  AND apply_url LIKE '%linkedin.com%'
                """
            )
            count = cursor.rowcount
            if count > 0:
                logger.info(f"[LinkedIn Migration] Cleared {count} self-referencing LinkedIn apply_urls.")
            return count

    def prune_rejected_jobs(self, max_age_days: int = 7) -> int:
        """
        Brief 14 B6: Deletes rejected rows older than max_age_days (default 7 days; keeps passed rows
        and applied_history matches, and stores dedupe keys in seen_keys). Performs VACUUM afterwards.
        """
        cutoff = (datetime.now(timezone.utc) - timedelta(days=max_age_days)).isoformat()
        now_str = datetime.now(timezone.utc).isoformat()
        with self._get_connection() as conn:
            rows = conn.execute(
                """
                SELECT id, dedupe_key, url
                FROM jobs
                WHERE gate_rejected IS NOT NULL
                  AND gate_rejected != 'dup'
                  AND (user_status IS NULL OR user_status IN ('new', 'unknown', ''))
                  AND applied_at IS NULL
                  AND exported_at IS NULL
                  AND (
                      (posted_at IS NOT NULL AND posted_at < ?)
                      OR (posted_at IS NULL AND created_at < ?)
                  )
                """,
                (cutoff, cutoff),
            ).fetchall()
            if rows:
                conn.executemany(
                    "INSERT OR IGNORE INTO seen_keys (dedupe_key, url, id, seen_at) VALUES (?, ?, ?, ?)",
                    [(r["dedupe_key"] or "", r["url"] or "", r["id"], now_str) for r in rows],
                )
            cursor = conn.execute(
                """
                DELETE FROM jobs
                WHERE gate_rejected IS NOT NULL
                  AND gate_rejected != 'dup'
                  AND (user_status IS NULL OR user_status IN ('new', 'unknown', ''))
                  AND applied_at IS NULL
                  AND exported_at IS NULL
                  AND (
                      (posted_at IS NOT NULL AND posted_at < ?)
                      OR (posted_at IS NULL AND created_at < ?)
                  )
                """,
                (cutoff, cutoff),
            )
            deleted = cursor.rowcount
            logger.info(f"[Prune] Deleted {deleted} rejected jobs older than {max_age_days} days (saved to seen_keys).")

        with sqlite3.connect(self.db_path) as conn:
            conn.execute("VACUUM;")
            logger.info("[Prune] SQLite VACUUM completed.")

        self.set_metadata("last_prune_at", datetime.now(timezone.utc).isoformat())
        return deleted

    def record_cycle_metrics(
        self,
        started_at: str,
        duration_s: float,
        per_source_postings: dict[str, int],
        per_source_errors: dict[str, int],
        new_rows: int,
        rejected_gates: dict[str, int],
        passed: int,
        closed: int,
        new_boards_found: int,
        linkedin_pauses: int,
        per_source_timing: Optional[dict[str, float]] = None,
        passed_total: int = 0,
        by_apply_kind: Optional[dict] = None,
        by_source: Optional[dict] = None,
        exported: int = 0,
    ) -> None:
        with self._get_connection() as conn:
            conn.execute(
                """
                INSERT INTO cycle_metrics (
                    started_at, duration_s, per_source_postings, per_source_errors,
                    new_rows, rejected_gates, passed, closed, new_boards_found, linkedin_pauses,
                    per_source_timing, passed_total, by_apply_kind, by_source, exported
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    started_at,
                    duration_s,
                    json.dumps(per_source_postings),
                    json.dumps(per_source_errors),
                    new_rows,
                    json.dumps(rejected_gates),
                    passed,
                    closed,
                    new_boards_found,
                    linkedin_pauses,
                    json.dumps(per_source_timing or {}),
                    passed_total,
                    json.dumps(by_apply_kind or {}),
                    json.dumps(by_source or {}),
                    exported,
                ),
            )

    def get_resolved_apply_url(self, url: str) -> Optional[dict]:
        """Fetch cached resolved apply URL record."""
        if not url:
            return None
        with self._get_connection() as conn:
            cursor = conn.execute("SELECT * FROM resolved_apply_urls WHERE url = ?", (url,))
            row = cursor.fetchone()
            return dict(row) if row else None

    def save_resolved_apply_url(
        self,
        url: str,
        final_url: str,
        ats: Optional[str] = None,
        ats_slug: Optional[str] = None,
        apply_kind: str = "unknown",
    ) -> None:
        """Cache resolved apply URL mapping."""
        if not url or not final_url:
            return
        now_str = datetime.now(timezone.utc).isoformat()
        with self._get_connection() as conn:
            conn.execute(
                """
                INSERT OR REPLACE INTO resolved_apply_urls (url, final_url, ats, ats_slug, apply_kind, resolved_at)
                VALUES (?, ?, ?, ?, ?, ?)
                """,
                (url, final_url, ats, ats_slug, apply_kind, now_str),
            )

    def get_all_candidates_for_full_export(self, days: int = 14) -> list[JobPosting]:
        """
        Fetch all candidate jobs for full Claude pipeline export:
        gate_rejected IS NULL AND score > 0 AND closed_at IS NULL AND apply_kind != 'account',
        posted/created within `days`.
        """
        now = datetime.now(timezone.utc)
        with self._get_connection() as conn:
            cursor = conn.execute(
                """
                SELECT * FROM jobs
                WHERE gate_rejected IS NULL
                  AND score > 0
                  AND closed_at IS NULL
                  AND (apply_kind IS NULL OR apply_kind != 'account')
                ORDER BY score DESC, created_at DESC
                """
            )
            rows = cursor.fetchall()
            results = []
            for row in rows:
                dt = None
                if row["posted_at"]:
                    try:
                        dt = datetime.fromisoformat(row["posted_at"])
                    except Exception:
                        pass
                if not dt and row["created_at"]:
                    try:
                        dt = datetime.fromisoformat(row["created_at"])
                    except Exception:
                        pass
                if dt:
                    tz_dt = dt if dt.tzinfo else dt.replace(tzinfo=timezone.utc)
                    if (now - tz_dt).total_seconds() / 86400.0 > days:
                        continue
                results.append(self._row_to_job(row))
            return results

    def get_recent_cycle_metrics(self, limit: int = 10) -> list[dict]:
        with self._get_connection() as conn:
            cursor = conn.execute("SELECT * FROM cycle_metrics ORDER BY id DESC LIMIT ?", (limit,))
            rows = cursor.fetchall()
            return [dict(r) for r in rows]

    def get_unnotified_matches(
        self, min_score: float = 60.0, max_age_days: int = 3, limit: int = 10
    ) -> list[JobPosting]:
        """
        Fetch qualified jobs for alerting:
        Score >= min_score, passed all gates, posted within max_age_days, unnotified, closed_at IS NULL.
        """
        with self._get_connection() as conn:
            cursor = conn.execute(
                """
                SELECT * FROM jobs 
                WHERE is_notified = 0 
                  AND gate_rejected IS NULL 
                  AND closed_at IS NULL
                  AND score >= ?
                ORDER BY score DESC, created_at DESC
                """,
                (min_score,),
            )
            rows = cursor.fetchall()
            results = []
            now = datetime.now(timezone.utc)

            for row in rows:
                posted_at = None
                if row["posted_at"]:
                    try:
                        posted_at = datetime.fromisoformat(row["posted_at"])
                    except Exception:
                        pass

                if posted_at:
                    tz_posted = posted_at if posted_at.tzinfo else posted_at.replace(tzinfo=timezone.utc)
                    age_days = (now - tz_posted).total_seconds() / 86400.0
                    if age_days > max_age_days:
                        continue

                results.append(self._row_to_job(row))
                if len(results) >= limit:
                    break

            return results

    def get_unexported_matches(self, max_age_days: int = 14) -> list[JobPosting]:
        """
        Fetch jobs that passed all gates, posted within the last 14 days, closed_at IS NULL,
        and not yet exported.
        """
        with self._get_connection() as conn:
            cursor = conn.execute(
                """
                SELECT * FROM jobs 
                WHERE exported_at IS NULL 
                  AND gate_rejected IS NULL 
                  AND closed_at IS NULL
                  AND score > 0
                ORDER BY score DESC, created_at DESC
                """
            )
            rows = cursor.fetchall()
            results = []
            now = datetime.now(timezone.utc)

            for row in rows:
                posted_at = None
                if row["posted_at"]:
                    try:
                        posted_at = datetime.fromisoformat(row["posted_at"])
                    except Exception:
                        pass

                if posted_at:
                    tz_posted = posted_at if posted_at.tzinfo else posted_at.replace(tzinfo=timezone.utc)
                    age_days = (now - tz_posted).total_seconds() / 86400.0
                    if age_days > max_age_days:
                        continue
                else:
                    try:
                        created_at = datetime.fromisoformat(row["created_at"])
                        tz_created = created_at if created_at.tzinfo else created_at.replace(tzinfo=timezone.utc)
                        if (now - tz_created).total_seconds() / 86400.0 > max_age_days:
                            continue
                    except Exception:
                        pass

                results.append(self._row_to_job(row))

            return results

    def get_candidate_jobs(self, max_age_days: int = 14) -> list[JobPosting]:
        """
        Fetch candidate jobs for the pipeline:
        gate_rejected IS NULL AND score > 0 AND closed_at IS NULL and posted within max_age_days.
        """
        with self._get_connection() as conn:
            cursor = conn.execute(
                """
                SELECT * FROM jobs 
                WHERE gate_rejected IS NULL 
                  AND closed_at IS NULL
                  AND score > 0
                ORDER BY score DESC, created_at DESC
                """
            )
            rows = cursor.fetchall()
            results = []
            now = datetime.now(timezone.utc)
            for row in rows:
                dt = None
                if row["posted_at"]:
                    try:
                        dt = datetime.fromisoformat(row["posted_at"])
                    except Exception:
                        pass
                if not dt and row["created_at"]:
                    try:
                        dt = datetime.fromisoformat(row["created_at"])
                    except Exception:
                        pass
                if dt:
                    tz_dt = dt if dt.tzinfo else dt.replace(tzinfo=timezone.utc)
                    if (now - tz_dt).total_seconds() / 86400.0 > max_age_days:
                        continue
                results.append(self._row_to_job(row))
            return results

    def mark_as_exported(self, job_ids: list[str]) -> None:
        """Record exported timestamp for given jobs."""
        if not job_ids:
            return
        now_str = datetime.now(timezone.utc).isoformat()
        with self._get_connection() as conn:
            placeholders = ",".join("?" for _ in job_ids)
            conn.execute(
                f"UPDATE jobs SET exported_at = ? WHERE id IN ({placeholders})",
                [now_str] + job_ids,
            )

    def mark_as_notified(self, job_ids: list[str]) -> None:
        """Update notification status to avoid duplicate alerts."""
        if not job_ids:
            return
        with self._get_connection() as conn:
            placeholders = ",".join("?" for _ in job_ids)
            conn.execute(
                f"UPDATE jobs SET is_notified = 1 WHERE id IN ({placeholders})",
                job_ids,
            )

    def get_metadata(self, key: str) -> Optional[str]:
        """Retrieve key from scraper_metadata."""
        with self._get_connection() as conn:
            cursor = conn.execute("SELECT value FROM scraper_metadata WHERE key = ?", (key,))
            row = cursor.fetchone()
            return row["value"] if row else None

    def set_metadata(self, key: str, value: str) -> None:
        """Store key/value in scraper_metadata."""
        with self._get_connection() as conn:
            conn.execute(
                "INSERT OR REPLACE INTO scraper_metadata (key, value) VALUES (?, ?)",
                (key, value),
            )

    def filter_slugs_by_cadence(self, ats: str, slugs: list[str]) -> tuple[list[str], int, int]:
        """
        Brief 13 Section 2 & Brief 14 Section A1:
        - A board that produced ANY posting passing location in the last 30 days is polled EVERY cycle.
        - A board that produced NO posting passing location in the last 30 days is polled ONCE A DAY (24h).
        Returns (due_slugs, active_board_count, daily_board_count).
        """
        if not slugs:
            return [], 0, 0

        prefix_lo = f"{ats}:"
        prefix_hi = f"{ats};"
        prefix_poll = f"last_poll_{ats}_"
        with self._get_connection() as conn:
            cursor = conn.execute(
                """
                SELECT DISTINCT coalesce(ats_slug, '') as slug_val, id
                FROM jobs
                WHERE (ats = ? OR (id >= ? AND id < ?))
                  AND (gate_rejected IS NULL OR (gate_rejected != 'location' AND gate_rejected NOT LIKE 'duplicate_of:%' AND location_scope IS NOT NULL))
                  AND (julianday('now') - julianday(coalesce(posted_at, created_at))) <= 30.0
                """,
                (ats, prefix_lo, prefix_hi),
            )
            rows = cursor.fetchall()

            poll_rows = conn.execute(
                "SELECT key, value FROM scraper_metadata WHERE key LIKE ? || '%'",
                (prefix_poll,),
            ).fetchall()
            poll_map = {r["key"]: r["value"] for r in poll_rows}

        active_slugs = set()
        for r in rows:
            s = r["slug_val"]
            if s:
                active_slugs.add(s)
            elif ":" in r["id"]:
                parts = r["id"].split(":")
                if len(parts) >= 2:
                    active_slugs.add(parts[1])

        now_dt = datetime.now(timezone.utc)
        due_slugs = []
        active_count = 0
        daily_count = 0

        for slug in slugs:
            if slug in active_slugs:
                active_count += 1
                due_slugs.append(slug)
            else:
                daily_count += 1
                key = f"last_poll_{ats}_{slug}"
                last_polled_str = poll_map.get(key)
                if not last_polled_str:
                    due_slugs.append(slug)
                else:
                    try:
                        last_dt = datetime.fromisoformat(last_polled_str)
                        if (now_dt - last_dt).total_seconds() >= 86400.0:  # 24 hours
                            due_slugs.append(slug)
                    except Exception:
                        due_slugs.append(slug)

        return due_slugs, active_count, daily_count

    def record_board_polls_batch(self, ats: str, slugs: list[str]) -> None:
        """Record board poll timestamps in a single transaction."""
        if not slugs:
            return
        now_iso = datetime.now(timezone.utc).isoformat()
        records = [(f"last_poll_{ats}_{slug}", now_iso) for slug in slugs]
        with self._get_connection() as conn:
            conn.executemany(
                "INSERT OR REPLACE INTO scraper_metadata (key, value) VALUES (?, ?)",
                records,
            )

    def get_company_displays_for_source(self, source: str) -> dict[str, str]:
        with self._get_connection() as conn:
            rows = conn.execute(
                "SELECT slug, display_name FROM companies WHERE source = ? AND display_name IS NOT NULL",
                (source,),
            ).fetchall()
            return {r["slug"]: r["display_name"] for r in rows}

    def save_company_displays_batch(self, source: str, displays: dict[str, str]) -> None:
        if not displays:
            return
        now_str = datetime.now(timezone.utc).isoformat()
        records = [(source, slug, name, now_str) for slug, name in displays.items()]
        with self._get_connection() as conn:
            conn.executemany(
                "INSERT OR REPLACE INTO companies (source, slug, display_name, updated_at) VALUES (?, ?, ?, ?)",
                records,
            )

    def checkpoint_wal(self) -> None:
        """Brief 13 Section 5: Run PRAGMA wal_checkpoint(TRUNCATE) after each cycle."""
        try:
            with self._get_connection() as conn:
                conn.execute("PRAGMA wal_checkpoint(TRUNCATE)")
            logger.info("[DB Housekeeping] WAL checkpoint (TRUNCATE) completed.")
        except Exception as exc:
            logger.warning(f"[DB Housekeeping] WAL checkpoint failed: {exc}")

    def prune_stale_rejected_jobs(self, force: bool = False, max_age_days: float = 7.0) -> dict:
        """
        Brief 13 Section 5 & Brief 14 B6:
        Once a day: delete rejected rows that are older than 7 days (default),
        keeping their dedupe_key/url/id in `seen_keys` table.
        VACUUM at most once a week.
        Never delete rows that passed, rows with user_status/applied_at set, or rows the pipeline exported.
        """
        now = datetime.now(timezone.utc)
        if not force:
            last_prune_str = self.get_metadata("last_daily_prune")
            if last_prune_str:
                try:
                    last_prune_dt = datetime.fromisoformat(last_prune_str)
                    if (now - last_prune_dt).total_seconds() < 86400.0:
                        return {"pruned": 0, "status": "skipped_not_due"}
                except Exception:
                    pass

        to_prune = []
        with self._get_connection() as conn:
            cursor = conn.execute(
                """
                SELECT id, dedupe_key, url
                FROM jobs
                WHERE gate_rejected IN ('location', 'role-rules', 'negative-keyword')
                  AND (julianday('now') - julianday(coalesce(posted_at, created_at))) > ?
                  AND (score IS NULL OR score <= 0)
                  AND (user_status IS NULL OR user_status IN ('new', 'unknown', ''))
                  AND applied_at IS NULL
                  AND exported_at IS NULL
                  AND (is_notified IS NULL OR is_notified = 0)
                """,
                (max_age_days,),
            )
            to_prune = cursor.fetchall()
            if to_prune:
                now_str = now.isoformat()
                seen_records = [
                    (
                        r["dedupe_key"] or "",
                        r["url"] or "",
                        r["id"],
                        now_str,
                    )
                    for r in to_prune
                ]
                conn.executemany(
                    "INSERT OR IGNORE INTO seen_keys (dedupe_key, url, id, seen_at) VALUES (?, ?, ?, ?)",
                    seen_records,
                )
                ids_to_del = [r["id"] for r in to_prune]
                for i in range(0, len(ids_to_del), 500):
                    chunk = ids_to_del[i : i + 500]
                    q = ",".join("?" for _ in chunk)
                    conn.execute(f"DELETE FROM jobs WHERE id IN ({q})", chunk)
                logger.info(f"[DB Housekeeping] Pruned {len(ids_to_del)} 30d+ rejected rows (kept in seen_keys).")

        # Check weekly vacuum
        vacuum_performed = False
        last_vac_str = self.get_metadata("last_weekly_vacuum")
        should_vac = force
        if not should_vac:
            if not last_vac_str:
                should_vac = True
            else:
                try:
                    last_vac_dt = datetime.fromisoformat(last_vac_str)
                    if (now - last_vac_dt).total_seconds() >= (7 * 86400.0):
                        should_vac = True
                except Exception:
                    should_vac = True

        if should_vac:
            try:
                import sqlite3
                vac_conn = sqlite3.connect(self.db_path)
                vac_conn.isolation_level = None
                vac_conn.execute("VACUUM")
                vac_conn.close()
                self.set_metadata("last_weekly_vacuum", now.isoformat())
                vacuum_performed = True
                logger.info("[DB Housekeeping] Executed weekly VACUUM.")
            except Exception as exc:
                logger.warning(f"[DB Housekeeping] VACUUM failed: {exc}")

        self.set_metadata("last_daily_prune", now.isoformat())

        return {"pruned": len(to_prune), "vacuum": vacuum_performed, "status": "ok"}

    def should_poll_board(self, ats: str, slug: str, force: bool = False) -> Tuple[bool, str]:
        """
        Brief 11 Section 7:
        - 3+ passing rows in last 30 days -> poll every 6 hours
        - 1-2 passing rows in last 30 days -> poll daily (24 hours)
        - 0 passing rows in last 30 days -> poll weekly (168 hours)
        """
        from typing import Tuple
        if force:
            return True, "forced"

        with self._get_connection() as conn:
            row = conn.execute(
                """
                SELECT COUNT(*) as passing_cnt
                FROM jobs
                WHERE ((ats = ? AND ats_slug = ?) OR id LIKE ? || ':' || ? || ':%')
                  AND gate_rejected IS NULL
                  AND score > 0
                  AND (julianday('now') - julianday(coalesce(posted_at, created_at))) <= 30.0
                """,
                (ats, slug, ats, slug),
            ).fetchone()
            passing_cnt = row["passing_cnt"] if row else 0

        key = f"last_poll_{ats}_{slug}"
        last_polled_str = self.get_metadata(key)
        if not last_polled_str:
            return True, f"first_poll (passing_30d: {passing_cnt})"

        try:
            last_dt = datetime.fromisoformat(last_polled_str)
            now_dt = datetime.now(timezone.utc)
            elapsed_hours = (now_dt - last_dt).total_seconds() / 3600.0
        except Exception:
            return True, "parse_error"

        if passing_cnt >= 3:
            interval_hours = 6.0
            cadence = "6h"
        elif passing_cnt > 0:
            interval_hours = 24.0
            cadence = "daily"
        else:
            interval_hours = 168.0
            cadence = "weekly"

        if elapsed_hours >= interval_hours:
            return True, f"due_{cadence} (elapsed: {elapsed_hours:.1f}h >= {interval_hours}h, passing_30d: {passing_cnt})"
        return False, f"skip_{cadence} (elapsed: {elapsed_hours:.1f}h < {interval_hours}h, passing_30d: {passing_cnt})"

    def record_board_poll(self, ats: str, slug: str) -> None:
        """Record board poll timestamp in scraper_metadata."""
        key = f"last_poll_{ats}_{slug}"
        self.set_metadata(key, datetime.now(timezone.utc).isoformat())

    def record_query_run(self, query: str, passing_count: int) -> None:
        """
        Brief 11 Section 6:
        Track runs and consecutive zero-passing runs per query.
        """
        now_iso = datetime.now(timezone.utc).isoformat()
        with self._get_connection() as conn:
            cursor = conn.execute("SELECT total_runs, consecutive_zero_runs, total_passing FROM query_performance WHERE query = ?", (query,))
            row = cursor.fetchone()
            if row:
                t_runs = row["total_runs"] + 1
                t_pass = row["total_passing"] + passing_count
                cz_runs = (row["consecutive_zero_runs"] + 1) if passing_count == 0 else 0
                conn.execute(
                    """
                    UPDATE query_performance
                    SET total_runs = ?, consecutive_zero_runs = ?, total_passing = ?, last_run_at = ?
                    WHERE query = ?
                    """,
                    (t_runs, cz_runs, t_pass, now_iso, query),
                )
            else:
                cz_runs = 1 if passing_count == 0 else 0
                conn.execute(
                    """
                    INSERT INTO query_performance (query, total_runs, consecutive_zero_runs, total_passing, last_run_at)
                    VALUES (?, 1, ?, ?, ?)
                    """,
                    (query, cz_runs, passing_count, now_iso),
                )

    def get_eligible_queries(self, base_queries: Optional[list[str]] = None) -> list[str]:
        """
        Brief 11 Section 6:
        - Prioritizes best-converting title list first + synonyms.
        - Excludes near-zero titles ("Integration Engineer", "Application Engineer", "ServiceNow", "Talent Acquisition").
        - Stops adding new queries for a title after two runs with zero passing rows.
        """
        best_converting = [
            "Business Systems Analyst",
            "Enablement",
            "Partner Manager",
            "Partner Solutions",
            "Technical Program Manager",
            "Technical Project Manager",
            "Implementation Manager",
            "Implementation Specialist",
            "Solution Engineer",
            "Solutions Engineer",
            "Solution Consultant",
            "Solutions Consultant",
            "Technical Account Manager",
            "Customer Success Engineer",
            "Marketing Operations",
            "Marketing Analyst",
            "AI Solutions",
            "AI Deployment",
            "Forward Deployed",
        ]
        synonyms = [
            "Customer Enablement",
            "Partner Enablement",
            "Alliance Manager",
            "Channel Manager",
            "CRM Analyst",
            "Product Analyst",
            "GTM Engineer",
            "Solutions Analyst",
            "Partner Technical Manager",
            "Integration Consultant",
            "iPaaS",
        ]
        excluded = {
            "integration engineer",
            "application engineer",
            "servicenow",
            "talent acquisition",
        }

        # Combine in priority order
        ordered_candidates: list[str] = []
        seen = set()
        for q in best_converting + synonyms + (base_queries or []):
            q_clean = q.strip()
            q_norm = q_clean.lower()
            if not q_clean or q_norm in seen:
                continue
            seen.add(q_norm)
            if any(exc in q_norm for exc in excluded):
                continue
            ordered_candidates.append(q_clean)

        # Check consecutive zero runs from DB
        with self._get_connection() as conn:
            cursor = conn.execute("SELECT query FROM query_performance WHERE consecutive_zero_runs >= 2")
            blocked_queries = {row["query"].strip().lower() for row in cursor.fetchall()}

        return [q for q in ordered_candidates if q.lower() not in blocked_queries]

    def get_all_jobs(self) -> list[JobPosting]:
        """Fetch all raw jobs for re-evaluation."""
        with self._get_connection() as conn:
            cursor = conn.execute("SELECT * FROM jobs")
            rows = cursor.fetchall()
            return [self._row_to_job(row) for row in rows]

    def _row_to_job(self, row: sqlite3.Row) -> JobPosting:
        keys = row.keys()
        posted_at = None
        if row["posted_at"]:
            try:
                posted_at = datetime.fromisoformat(row["posted_at"])
            except Exception:
                pass

        created_at = datetime.now(timezone.utc)
        if row["created_at"]:
            try:
                created_at = datetime.fromisoformat(row["created_at"])
            except Exception:
                pass

        first_seen = None
        if "first_seen" in keys and row["first_seen"]:
            try:
                first_seen = datetime.fromisoformat(row["first_seen"])
            except Exception:
                pass
        if first_seen is None:
            first_seen = created_at

        exported_at = None
        if "exported_at" in keys and row["exported_at"]:
            try:
                exported_at = datetime.fromisoformat(row["exported_at"])
            except Exception:
                pass

        closed_at = None
        if "closed_at" in keys and row["closed_at"]:
            try:
                closed_at = datetime.fromisoformat(row["closed_at"])
            except Exception:
                pass

        user_status = row["user_status"] if ("user_status" in keys and row["user_status"]) else "new"
        applied_at = None
        if "applied_at" in keys and row["applied_at"]:
            try:
                applied_at = datetime.fromisoformat(row["applied_at"])
            except Exception:
                pass
        notes = row["notes"] if ("notes" in keys and row["notes"]) else None
        apply_kind = row["apply_kind"] if ("apply_kind" in keys and row["apply_kind"]) else "unknown"
        apply_host = row["apply_host"] if ("apply_host" in keys and row["apply_host"]) else None
        is_startup = int(row["is_startup"] or 0) if "is_startup" in keys else 0
        startup_source = row["startup_source"] if ("startup_source" in keys and row["startup_source"]) else None
        last_liveness_check = None
        if "last_liveness_check" in keys and row["last_liveness_check"]:
            try:
                last_liveness_check = datetime.fromisoformat(row["last_liveness_check"])
            except Exception:
                pass

        location_source = row["location_source"] if ("location_source" in keys and row["location_source"]) else None
        location_scope = row["location_scope"] if ("location_scope" in keys and row["location_scope"]) else None
        gate_reason = (
            row["gate_reason"]
            if ("gate_reason" in keys and row["gate_reason"])
            else (row["rejection_reason"] if "rejection_reason" in keys else None)
        )
        apply_route = row["apply_route"] if ("apply_route" in keys and row["apply_route"]) else None
        needs_account = int(row["needs_account"] or 0) if "needs_account" in keys else 0
        years_required = int(row["years_required"]) if ("years_required" in keys and row["years_required"] is not None) else None
        contract_type = row["contract_type"] if ("contract_type" in keys and row["contract_type"]) else None
        german_required = int(row["german_required"] or 0) if "german_required" in keys else 0
        title_weight = float(row["title_weight"] or 0.0) if "title_weight" in keys else 0.0

        return JobPosting(
            id=row["id"],
            title=row["title"],
            company=row["company"],
            location=row["location"],
            location_source=location_source,
            location_scope=location_scope,
            url=row["url"],
            description=row["description"] or "",
            source=row["source"],
            score=float(row["score"] or 0.0),
            matched_skills=json.loads(row["matched_skills"]) if row["matched_skills"] else [],
            matched_role=row["matched_role"],
            matched_categories=json.loads(row["matched_categories"])
            if ("matched_categories" in keys and row["matched_categories"])
            else {},
            language_status=row["language_status"]
            if "language_status" in keys and row["language_status"]
            else "English Friendly",
            fit_summary=row["fit_summary"] if "fit_summary" in keys and row["fit_summary"] else "",
            gate_rejected=row["gate_rejected"] if "gate_rejected" in keys else None,
            rejection_reason=row["rejection_reason"] if "rejection_reason" in keys else gate_reason,
            gate_reason=gate_reason,
            is_notified=bool(row["is_notified"]),
            exported_at=exported_at,
            posted_at=posted_at,
            created_at=created_at,
            first_seen=first_seen,
            dedupe_key=row["dedupe_key"] if "dedupe_key" in keys else None,
            company_display=row["company_display"] if "company_display" in keys else None,
            apply_url=row["apply_url"] if "apply_url" in keys else None,
            ats=row["ats"] if "ats" in keys else None,
            ats_slug=row["ats_slug"] if "ats_slug" in keys else None,
            closed_at=closed_at,
            user_status=user_status,
            applied_at=applied_at,
            notes=notes,
            apply_kind=apply_kind,
            apply_host=apply_host,
            apply_route=apply_route,
            needs_account=needs_account,
            years_required=years_required,
            contract_type=contract_type,
            german_required=german_required,
            title_weight=title_weight,
            is_startup=is_startup,
            startup_source=startup_source,
            last_liveness_check=last_liveness_check,
            apply_url_source=row["apply_url_source"] if "apply_url_source" in keys else None,
            apply_url_status=row["apply_url_status"] if "apply_url_status" in keys else None,
            description_source=row["description_source"] if "description_source" in keys else None,
        )

    def set_job_user_status(self, job_id: str, status: str, notes: Optional[str] = None) -> bool:
        """
        Update user tracking status for a job (e.g. 'new', 'saved', 'applied', 'archived').
        Sets applied_at when status is 'applied'.
        """
        valid_statuses = {"new", "saved", "applied", "archived"}
        if status not in valid_statuses:
            raise ValueError(f"Invalid status: {status}. Must be one of {valid_statuses}")

        now_str = datetime.now(timezone.utc).isoformat()
        with self._get_connection() as conn:
            if status == "applied":
                if notes is not None:
                    conn.execute(
                        "UPDATE jobs SET user_status = ?, applied_at = coalesce(applied_at, ?), notes = ? WHERE id = ?",
                        (status, now_str, notes, job_id),
                    )
                else:
                    conn.execute(
                        "UPDATE jobs SET user_status = ?, applied_at = coalesce(applied_at, ?) WHERE id = ?",
                        (status, now_str, job_id),
                    )
            else:
                if notes is not None:
                    conn.execute(
                        "UPDATE jobs SET user_status = ?, notes = ? WHERE id = ?",
                        (status, notes, job_id),
                    )
                else:
                    conn.execute(
                        "UPDATE jobs SET user_status = ? WHERE id = ?",
                        (status, job_id),
                    )
            return conn.total_changes > 0

    def get_web_jobs(
        self,
        query: Optional[str] = None,
        language: Optional[str] = None,
        user_status: Optional[str] = None,
        min_score: float = 0.0,
        source: Optional[str] = None,
        include_closed: bool = False,
        include_rejected: bool = False,
        sort_by: str = "score",
        limit: int = 150,
        offset: int = 0,
    ) -> tuple[list[dict], int]:
        """
        Fetch filtered and paginated jobs for the web dashboard.
        Returns (list_of_job_dicts, total_count).
        """
        conditions = []
        params = []

        if not include_rejected:
            conditions.append("gate_rejected IS NULL")
            conditions.append("score > 0")

        if not include_closed:
            conditions.append("closed_at IS NULL")

        if min_score > 0:
            conditions.append("score >= ?")
            params.append(min_score)

        if language and language != "All":
            conditions.append("language_status = ?")
            params.append(language)

        if user_status and user_status != "All":
            conditions.append("coalesce(user_status, 'new') = ?")
            params.append(user_status)

        if source and source != "All":
            conditions.append("source = ?")
            params.append(source)

        if query and query.strip():
            terms = query.strip().split()
            for t in terms:
                conditions.append(
                    "(title LIKE ? OR company LIKE ? OR company_display LIKE ? OR matched_skills LIKE ? OR location LIKE ?)"
                )
                wildcard = f"%{t}%"
                params.extend([wildcard, wildcard, wildcard, wildcard, wildcard])

        where_clause = ("WHERE " + " AND ".join(conditions)) if conditions else ""

        order_clause = "ORDER BY score DESC, created_at DESC"
        if sort_by == "date":
            order_clause = "ORDER BY coalesce(posted_at, created_at) DESC"
        elif sort_by == "company":
            order_clause = "ORDER BY coalesce(company_display, company) ASC, score DESC"
        elif sort_by == "score":
            order_clause = "ORDER BY score DESC, created_at DESC"

        count_sql = f"SELECT COUNT(*) FROM jobs {where_clause}"

        select_sql = f"""
            SELECT id, title, company, company_display, location, url, apply_url,
                   source, ats, ats_slug, score, matched_skills, matched_role,
                   matched_categories, language_status, fit_summary,
                   gate_rejected, rejection_reason, closed_at, posted_at, created_at,
                   user_status, applied_at, notes, description
            FROM jobs
            {where_clause}
            {order_clause}
            LIMIT ? OFFSET ?
        """

        with self._get_connection() as conn:
            total_count = conn.execute(count_sql, params).fetchone()[0]
            cursor = conn.execute(select_sql, params + [limit, offset])
            rows = cursor.fetchall()

            jobs = []
            for r in rows:
                item = dict(r)
                if item["matched_skills"]:
                    try:
                        item["matched_skills"] = json.loads(item["matched_skills"])
                    except Exception:
                        item["matched_skills"] = []
                else:
                    item["matched_skills"] = []

                if item["matched_categories"]:
                    try:
                        item["matched_categories"] = json.loads(item["matched_categories"])
                    except Exception:
                        item["matched_categories"] = {}
                else:
                    item["matched_categories"] = {}
                jobs.append(item)

            return jobs, total_count

    def get_dashboard_stats(self) -> dict:
        """Aggregate KPIs for dashboard summary cards."""
        with self._get_connection() as conn:
            total_db = conn.execute("SELECT COUNT(*) FROM jobs").fetchone()[0]
            total_candidates = conn.execute(
                "SELECT COUNT(*) FROM jobs WHERE gate_rejected IS NULL AND score > 0 AND closed_at IS NULL"
            ).fetchone()[0]
            english_only = conn.execute(
                "SELECT COUNT(*) FROM jobs WHERE gate_rejected IS NULL AND score > 0 AND closed_at IS NULL AND language_status = 'English Only'"
            ).fetchone()[0]
            german_plus = conn.execute(
                "SELECT COUNT(*) FROM jobs WHERE gate_rejected IS NULL AND score > 0 AND closed_at IS NULL AND language_status = 'German is a Plus'"
            ).fetchone()[0]
            saved = conn.execute(
                "SELECT COUNT(*) FROM jobs WHERE user_status = 'saved'"
            ).fetchone()[0]
            applied = conn.execute(
                "SELECT COUNT(*) FROM jobs WHERE user_status = 'applied'"
            ).fetchone()[0]
            archived = conn.execute(
                "SELECT COUNT(*) FROM jobs WHERE user_status = 'archived'"
            ).fetchone()[0]
            closed = conn.execute(
                "SELECT COUNT(*) FROM jobs WHERE closed_at IS NOT NULL"
            ).fetchone()[0]

            return {
                "total_db": total_db,
                "total_candidates": total_candidates,
                "english_only": english_only,
                "german_plus": german_plus,
                "saved": saved,
                "applied": applied,
                "archived": archived,
                "closed": closed,
            }
