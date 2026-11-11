import pytest
from unittest.mock import MagicMock
from datetime import datetime
from pathlib import Path
import tempfile
import httpx
import config
from database import Database
from scrapers.greenhouse import GreenhouseScraper
from matcher import ProfileMatcher


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


def test_greenhouse_skips_details_for_location_or_role_failing(temp_db):
    matcher = ProfileMatcher(config.config.profile)
    matcher.load_role_rules()
    scraper = GreenhouseScraper(db=temp_db, matcher=matcher)

    mock_client = MagicMock(spec=httpx.Client)

    def mock_get(url, **kwargs):
        res = MagicMock(spec=httpx.Response)
        res.status_code = 200
        if url.endswith("/v1/boards/testcompany"):
            res.json.return_value = {"name": "Test Company"}
        elif url.endswith("/v1/boards/testcompany/jobs"):
            res.json.return_value = {
                "jobs": [
                    {
                        "id": 101,
                        "title": "Recruitment Consultant",  # Blocked by role_rules
                        "location": {"name": "Munich, Germany"},
                        "absolute_url": "https://example.com/recruiter",
                        "updated_at": "2026-03-01T12:00:00Z",
                    },
                    {
                        "id": 102,
                        "title": "Software Developer",
                        "location": {"name": "San Francisco, CA"},  # Blocked by location
                        "absolute_url": "https://example.com/sf",
                        "updated_at": "2026-03-01T12:00:00Z",
                    },
                    {
                        "id": 103,
                        "title": "Python Developer",  # Passes both!
                        "location": {"name": "Munich, Germany"},
                        "absolute_url": "https://example.com/python",
                        "updated_at": "2026-03-01T12:00:00Z",
                    },
                ]
            }
        elif "/v1/boards/testcompany/jobs/103" in url:
            res.json.return_value = {"content": "<p>Python details description</p>"}
        else:
            res.status_code = 404
        return res

    mock_client.get.side_effect = mock_get

    jobs, listed, details = scraper._fetch_slug(mock_client, "testcompany")
    assert listed == 3
    assert details == 1  # Only job 103 fetched details!

    # Check that job 103 got content and jobs 101/102 did not
    job_103 = next(j for j in jobs if j.id == "greenhouse:testcompany:103")
    assert "Python details description" in job_103.description

    job_101 = next(j for j in jobs if j.id == "greenhouse:testcompany:101")
    assert job_101.description == ""


def test_greenhouse_reuses_cached_description_if_updated_at_matches(temp_db):
    matcher = ProfileMatcher(config.config.profile)
    matcher.load_role_rules()
    scraper = GreenhouseScraper(db=temp_db, matcher=matcher)

    # Insert existing job into temp_db
    with temp_db._get_connection() as conn:
        conn.execute(
            """
            INSERT INTO jobs (id, title, company, url, description, posted_at, first_seen, created_at)
            VALUES (?, ?, ?, ?, ?, ?, ?, datetime('now'))
            """,
            (
                "greenhouse:testco:201",
                "Python Developer",
                "Test Co",
                "https://example.com/201",
                "Cached description from previous run that is long enough to satisfy check",
                "2026-03-01T12:00:00",
                "2026-03-01T12:00:00",
            ),
        )

    mock_client = MagicMock(spec=httpx.Client)

    def mock_get(url, **kwargs):
        res = MagicMock(spec=httpx.Response)
        res.status_code = 200
        if url.endswith("/v1/boards/testco"):
            res.json.return_value = {"name": "Test Co"}
        elif url.endswith("/v1/boards/testco/jobs"):
            res.json.return_value = {
                "jobs": [
                    {
                        "id": 201,
                        "title": "Python Developer",
                        "location": {"name": "Munich, Germany"},
                        "absolute_url": "https://example.com/201",
                        "updated_at": "2026-03-01T12:00:00Z",
                    }
                ]
            }
        else:
            raise AssertionError(f"Unexpected detail fetch for url: {url}")
        return res

    mock_client.get.side_effect = mock_get

    jobs, listed, details = scraper._fetch_slug(mock_client, "testco")
    assert listed == 1
    assert details == 0  # Reused cache!
    assert "Cached description" in jobs[0].description


def test_board_cadence_filtering(temp_db):
    # Setup slugs: active_slug (has location passing job in last 30d) vs inactive_slug
    with temp_db._get_connection() as conn:
        conn.execute(
            """
            INSERT INTO jobs (id, title, company, url, gate_rejected, first_seen, created_at)
            VALUES (?, ?, ?, ?, ?, datetime('now', '-5 days'), datetime('now'))
            """,
            ("greenhouse:active_slug:1", "Python Dev", "Active Co", "https://example.com/1", None),
        )

    slugs = ["active_slug", "inactive_slug"]

    # First run: neither has poll record, so both should be polled
    to_poll, active_cnt, daily_cnt = temp_db.filter_slugs_by_cadence("greenhouse", slugs)
    assert "active_slug" in to_poll
    assert "inactive_slug" in to_poll

    # Record poll for both
    temp_db.record_board_poll("greenhouse", "active_slug")
    temp_db.record_board_poll("greenhouse", "inactive_slug")

    # Second run immediately: active_slug is active (polled every cycle), inactive_slug was polled < 24h ago
    to_poll_2, active_cnt_2, daily_cnt_2 = temp_db.filter_slugs_by_cadence("greenhouse", slugs)
    assert "active_slug" in to_poll_2
    assert "inactive_slug" not in to_poll_2
