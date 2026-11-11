"""
Re-evaluates and re-scores jobs in jobs.db impacted by:
1. Expanded Munich metro suburbs (Parsdorf, Manching, Brunnthal, Olching, Puchheim, Sauerlach, Ebersberg, Bergkirchen, Maisach)
2. Customer-facing engineering roles (Forward Deployed Engineer, Solutions Engineer, AI Solutions)
3. Unscored active postings in the Munich metro area
"""
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

import sqlite3
from datetime import datetime, timezone
import json

from config import ProfileConfig
from matcher import ProfileMatcher, MUNICH_SUBURBS
from database import Database
from models import JobPosting

DB_PATH = Path("jobs.db")

def main():
    if not DB_PATH.exists():
        print("jobs.db not found.")
        return

    db = Database(DB_PATH)
    matcher = ProfileMatcher(ProfileConfig())

    new_suburbs = [
        "parsdorf", "manching", "brunnthal", "olching", "puchheim",
        "sauerlach", "ebersberg", "bergkirchen", "maisach"
    ]

    # Query 1: Location-gated jobs in new suburbs
    suburb_clauses = " OR ".join(["LOWER(location) LIKE ?" for _ in new_suburbs])
    params = [f"%{s}%" for s in new_suburbs]

    with db._get_connection() as conn:
        suburb_rows = conn.execute(
            f"SELECT * FROM jobs WHERE gate_rejected = 'location' AND ({suburb_clauses})",
            params
        ).fetchall()

        # Query 2: Off-function gated jobs with customer-facing tech roles
        fde_rows = conn.execute(
            """
            SELECT * FROM jobs 
            WHERE gate_rejected = 'off-function' 
            AND (
                LOWER(title) LIKE '%forward deployed%' 
                OR LOWER(title) LIKE '%solutions engineer%'
                OR LOWER(title) LIKE '%solutions architect%'
                OR LOWER(title) LIKE '%sales engineer%'
                OR LOWER(title) LIKE '%customer engineer%'
                OR LOWER(title) LIKE '%ai solutions%'
            )
            """
        ).fetchall()

        # Query 3: Unscored active jobs in Munich
        unscored_rows = conn.execute(
            """
            SELECT * FROM jobs 
            WHERE (score IS NULL OR score = 0) 
            AND gate_rejected IS NULL 
            AND closed_at IS NULL
            AND (
                LOWER(location) LIKE '%munich%' 
                OR LOWER(location) LIKE '%münchen%'
                OR location_scope = 'munich'
            )
            """
        ).fetchall()

    print(f"Candidates to re-evaluate: {len(suburb_rows)} suburb-gated, {len(fde_rows)} fde/solutions-gated, {len(unscored_rows)} unscored")

    all_candidate_ids = set()
    combined_rows = []
    for r in list(suburb_rows) + list(fde_rows) + list(unscored_rows):
        if r["id"] not in all_candidate_ids:
            all_candidate_ids.add(r["id"])
            combined_rows.append(r)

    rescued = 0
    now_str = datetime.now(timezone.utc).isoformat()
    updated_jobs = []

    for r in combined_rows:
        job = db._row_to_job(r)
        evaluated = matcher.evaluate(job)

        # Check if status improved
        prev_gate = r["gate_rejected"]
        prev_score = r["score"] or 0.0

        if (evaluated.gate_rejected is None and prev_gate is not None) or (evaluated.score > prev_score):
            if evaluated.gate_rejected is None and evaluated.score > 0:
                rescued += 1
            updated_jobs.append(evaluated)

    if updated_jobs:
        db.save_jobs(updated_jobs)
        print(f"Successfully re-evaluated {len(combined_rows)} jobs: {rescued} jobs newly passed and rescued! Updated {len(updated_jobs)} in DB.")
    else:
        print("No job status updates needed.")

if __name__ == "__main__":
    main()
