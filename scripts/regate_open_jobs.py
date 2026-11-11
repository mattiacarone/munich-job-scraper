import sys
import logging
from collections import defaultdict
from pathlib import Path

# Ensure root dir is in sys.path
sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

import config
from database import Database
from matcher import ProfileMatcher

logging.basicConfig(level=logging.INFO, format="%(asctime)s [%(levelname)s] %(message)s")
logger = logging.getLogger("regate")


def regate_open_jobs(db_path=None) -> dict:
    db = Database(db_path or config.config.db_path)
    matcher = ProfileMatcher(config.config.profile)
    matcher.reload_data()

    with db._get_connection() as conn:
        cursor = conn.execute(
            "SELECT * FROM jobs WHERE gate_rejected IS NULL AND closed_at IS NULL"
        )
        rows = cursor.fetchall()

    logger.info(f"Loaded {len(rows)} open rows with gate_rejected IS NULL.")
    rejection_counts = defaultdict(int)
    passed_count = 0
    updates = []

    for r in rows:
        job = db._row_to_job(r)
        ev = matcher.evaluate(job)
        if ev.gate_rejected:
            rejection_counts[ev.gate_rejected] += 1
            updates.append((ev.gate_rejected, ev.gate_reason, ev.gate_reason, 0.0, job.id))
        else:
            passed_count += 1
            # Ensure score and fields are fresh
            updates.append((None, None, None, ev.score, job.id))

    logger.info(f"Re-gating complete: {passed_count} passed, {len(rows) - passed_count} rejected.")
    for gate, cnt in sorted(rejection_counts.items(), key=lambda x: -x[1]):
        logger.info(f"  - {gate}: {cnt}")

    if updates:
        with db._get_connection() as conn:
            conn.executemany(
                """
                UPDATE jobs SET
                    gate_rejected = ?,
                    rejection_reason = ?,
                    gate_reason = ?,
                    score = ?
                WHERE id = ?
                """,
                updates,
            )
        logger.info(f"Persisted updates for {len(updates)} rows.")

    return {
        "total_evaluated": len(rows),
        "passed": passed_count,
        "rejected": len(rows) - passed_count,
        "by_gate": dict(rejection_counts),
    }


if __name__ == "__main__":
    stats = regate_open_jobs()
    print("DONE:", stats)

