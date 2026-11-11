import pytest
from pathlib import Path
import tempfile
from datetime import datetime, timezone, timedelta
from database import Database
from models import JobPosting


@pytest.fixture
def temp_db():
    f = Path(tempfile.mktemp(suffix=".db"))
    db = Database(f)
    yield db
    try:
        f.unlink(missing_ok=True)
        Path(str(f) + "-wal").unlink(missing_ok=True)
        Path(str(f) + "-shm").unlink(missing_ok=True)
    except Exception:
        pass


def test_checkpoint_wal(temp_db):
    # Should execute PRAGMA wal_checkpoint(TRUNCATE) cleanly
    temp_db.checkpoint_wal()


def test_prune_stale_rejected_jobs(temp_db):
    old_date = (datetime.now(timezone.utc) - timedelta(days=35)).isoformat()
    recent_date = (datetime.now(timezone.utc) - timedelta(days=5)).isoformat()

    with temp_db._get_connection() as conn:
        # 1. Old location rejected job -> SHOULD BE PRUNED
        conn.execute(
            """
            INSERT INTO jobs (id, title, company, url, gate_rejected, posted_at, created_at)
            VALUES (?, ?, ?, ?, ?, ?, ?)
            """,
            ("job:old_location", "Accountant", "Acme", "https://example.com/1", "location", old_date, old_date),
        )
        # 2. Old role-rules rejected job -> SHOULD BE PRUNED
        conn.execute(
            """
            INSERT INTO jobs (id, title, company, url, gate_rejected, posted_at, created_at)
            VALUES (?, ?, ?, ?, ?, ?, ?)
            """,
            ("job:old_role", "Dentist", "Acme", "https://example.com/2", "role-rules", old_date, old_date),
        )
        # 3. Old negative-keyword rejected job -> SHOULD BE PRUNED
        conn.execute(
            """
            INSERT INTO jobs (id, title, company, url, gate_rejected, posted_at, created_at)
            VALUES (?, ?, ?, ?, ?, ?, ?)
            """,
            ("job:old_neg", "Electrician", "Acme", "https://example.com/3", "negative-keyword", old_date, old_date),
        )
        # 4. Recent rejected job (< 30d) -> SHOULD NOT BE PRUNED
        conn.execute(
            """
            INSERT INTO jobs (id, title, company, url, gate_rejected, posted_at, created_at)
            VALUES (?, ?, ?, ?, ?, ?, ?)
            """,
            ("job:recent_rej", "Dentist", "Acme", "https://example.com/4", "location", recent_date, recent_date),
        )
        # 5. Old passing job -> NEVER PRUNED
        conn.execute(
            """
            INSERT INTO jobs (id, title, company, url, gate_rejected, score, posted_at, created_at)
            VALUES (?, ?, ?, ?, ?, ?, ?, ?)
            """,
            ("job:old_pass", "Python Dev", "Acme", "https://example.com/5", None, 85, old_date, old_date),
        )
        # 6. Old job with applied_at -> NEVER PRUNED
        conn.execute(
            """
            INSERT INTO jobs (id, title, company, url, gate_rejected, applied_at, posted_at, created_at)
            VALUES (?, ?, ?, ?, ?, ?, ?, ?)
            """,
            ("job:old_applied", "Dentist", "Acme", "https://example.com/6", "location", old_date, old_date, old_date),
        )
        # 7. Old job with exported_at -> NEVER PRUNED
        conn.execute(
            """
            INSERT INTO jobs (id, title, company, url, gate_rejected, exported_at, posted_at, created_at)
            VALUES (?, ?, ?, ?, ?, ?, ?, ?)
            """,
            ("job:old_exported", "Dentist", "Acme", "https://example.com/7", "location", old_date, old_date, old_date),
        )

    res = temp_db.prune_stale_rejected_jobs(force=True, max_age_days=30.0)
    assert res["pruned"] == 3

    # Check remaining jobs
    with temp_db._get_connection() as conn:
        cursor = conn.execute("SELECT id FROM jobs")
        remaining_ids = {r[0] for r in cursor.fetchall()}
        assert "job:old_location" not in remaining_ids
        assert "job:old_role" not in remaining_ids
        assert "job:old_neg" not in remaining_ids
        assert "job:recent_rej" in remaining_ids
        assert "job:old_pass" in remaining_ids
        assert "job:old_applied" in remaining_ids
        assert "job:old_exported" in remaining_ids

        # Check seen_keys
        cursor = conn.execute("SELECT id FROM seen_keys")
        seen_ids = {r[0] for r in cursor.fetchall()}
        assert "job:old_location" in seen_ids
        assert "job:old_role" in seen_ids
        assert "job:old_neg" in seen_ids


def test_filter_new_duplicates_drops_seen_keys(temp_db):
    with temp_db._get_connection() as conn:
        conn.execute(
            "INSERT INTO seen_keys (dedupe_key, url, id, seen_at) VALUES (?, ?, ?, datetime('now'))",
            ("acme:oldjob", "https://example.com/seen", "job:seen_id"),
        )

    incoming = [
        JobPosting(
            id="job:seen_id",
            title="Software Dev",
            company="Acme",
            url="https://example.com/seen",
            source="greenhouse",
        ),
        JobPosting(
            id="job:brand_new_id",
            title="Software Dev",
            company="Acme",
            url="https://example.com/new",
            source="greenhouse",
        ),
    ]

    kept, stats = temp_db.filter_new_duplicates(incoming)
    kept_ids = [j.id for j in kept]
    assert "job:seen_id" not in kept_ids
    assert "job:brand_new_id" in kept_ids

