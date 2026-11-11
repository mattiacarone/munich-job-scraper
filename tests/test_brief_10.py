"""
Unit tests for Brief 10 (Route Calibration):
- Route table: 6 Ashby results with 5 SUBMITTED turns Ashby rows to `auto`.
(Firecrawl-only tests removed in Brief 14 B2.)
"""

import json
from datetime import datetime, timezone
from pathlib import Path

from database import Database
from models import JobPosting
from route_calibrator import apply_calibrated_routes_to_db, calibrate_route_success


def test_route_table_calibrates_ashby_to_auto(tmp_path: Path):
    """
    Route table: 6 Ashby results with 5 SUBMITTED turns Ashby rows to `auto`.
    """
    ready_dir = tmp_path / "ready"
    ready_dir.mkdir()

    # Create 6 folders for Ashby roles: 5 SUBMITTED, 1 NEEDS-HUMAN
    for i in range(1, 7):
        folder = ready_dir / f"ashbyco_role{i}"
        folder.mkdir()
        status = "SUBMITTED" if i <= 5 else "NEEDS-HUMAN"
        (folder / "result.json").write_text(json.dumps({"status": status}), encoding="utf-8")
        (folder / "meta.json").write_text(
            json.dumps({"url": f"https://jobs.ashbyhq.com/ashbyco/{i}", "apply_url": f"https://jobs.ashbyhq.com/ashbyco/{i}"}),
            encoding="utf-8",
        )

    out_json = tmp_path / "data" / "route_success.json"
    results = calibrate_route_success(ready_dir=ready_dir, output_json=out_json)

    assert "ashby" in results
    ashby_stats = results["ashby"]
    assert ashby_stats["tried"] == 6
    assert ashby_stats["SUBMITTED"] == 5
    assert ashby_stats["submitted_pct"] == round(5 / 6 * 100, 1)
    assert ashby_stats["apply_kind"] == "auto"

    # Test applying to DB
    db = Database(tmp_path / "test_ashby.db")
    ashby_job = JobPosting(
        id="ashby:sample:100",
        source="ashby",
        company="Sample Co",
        title="Solutions Engineer",
        url="https://jobs.ashbyhq.com/sample/100",
        apply_url="https://jobs.ashbyhq.com/sample/100",
        ats="ashby",
        apply_route="ashby",
        apply_kind="captcha",
        score=60.0,
        posted_at=datetime.now(timezone.utc),
    )
    db.save_jobs([ashby_job])

    updated_cnt = apply_calibrated_routes_to_db(db, route_calibrations={"ashby": "auto"})
    assert updated_cnt == 1

    reloaded = db._row_to_job(db._get_connection().execute("SELECT * FROM jobs WHERE id = 'ashby:sample:100'").fetchone())
    assert reloaded.apply_kind == "auto"
