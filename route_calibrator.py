"""
Route Calibrator for munichScraper (Brief 10 Section 5).
Reads application results from the application pipeline's ready directory
(`C:\\Users\\Mattia\\Desktop\\Job Application material\\ready\\<company>_<title>`),
derives the route from the host/URL, computes:
tried, SUBMITTED, CAPTCHA, ACCOUNT-ONLY.

Rules:
- If a route has at least 5 tries and at least 50% SUBMITTED, set apply_kind='auto'
  for its rows (this fixes Ashby) unless the page was already flagged with a visible captcha.
- If a route has at least 5 tries and 0 SUBMITTED with mostly CAPTCHA or ACCOUNT-ONLY, keep captcha or account.
- Writes the calibrated table to `data/route_success.json`.
- Never deletes a row because of apply_kind.
"""

import json
import logging
from collections import defaultdict
from pathlib import Path
from typing import Any, Optional
from urllib.parse import urlparse

logger = logging.getLogger(__name__)

BASE_DIR = Path(__file__).resolve().parent
DATA_DIR = BASE_DIR / "data"
DEFAULT_READY_DIR = Path(r"C:\Users\Mattia\Desktop\Job Application material\ready")
ROUTE_SUCCESS_FILE = DATA_DIR / "route_success.json"


def derive_route_from_target(url: str, apply_url: str = "") -> str:
    """Derive ATS/route identifier from job url or apply_url."""
    target = (apply_url or url or "").lower()
    if "greenhouse.io" in target:
        return "greenhouse"
    if "ashbyhq.com" in target:
        return "ashby"
    if "lever.co" in target:
        return "lever"
    if "personio" in target:
        return "personio"
    if "smartrecruiters" in target:
        return "smartrecruiters"
    if "join.com" in target:
        return "join"
    if "recruitee.com" in target:
        return "recruitee"
    if "teamtailor.com" in target:
        return "teamtailor"
    if "workable.com" in target:
        return "workable"
    if "softgarden" in target:
        return "softgarden"
    if "bamboohr" in target:
        return "bamboohr"
    if "breezy" in target:
        return "breezy"
    if "pinpoint" in target:
        return "pinpoint"
    if "linkedin.com" in target:
        return "linkedin"

    host = urlparse(target).netloc
    if host.startswith("www."):
        host = host[4:]
    return host or "unknown"


def calibrate_route_success(
    ready_dir: Optional[Path] = None,
    output_json: Optional[Path] = None,
) -> dict[str, dict[str, Any]]:
    """
    Read read-only result.json and meta.json files from ready directory,
    compute statistics per route, apply decision rules, and persist to route_success.json.
    """
    target_dir = ready_dir or DEFAULT_READY_DIR
    out_file = output_json or ROUTE_SUCCESS_FILE

    raw_stats: dict[str, dict[str, int]] = defaultdict(
        lambda: {"tried": 0, "SUBMITTED": 0, "CAPTCHA": 0, "ACCOUNT-ONLY": 0, "OTHER": 0}
    )

    if target_dir.exists():
        for p in target_dir.iterdir():
            if not p.is_dir():
                continue
            res_file = p / "result.json"
            meta_file = p / "meta.json"
            if not res_file.exists():
                continue

            try:
                res_data = json.loads(res_file.read_text(encoding="utf-8"))
                meta_data = json.loads(meta_file.read_text(encoding="utf-8")) if meta_file.exists() else {}
                raw_st = (res_data.get("status") or "").strip().upper()
                # Skip roles waiting in queue that have not been attempted yet
                if raw_st == "HANDOFF-READY":
                    continue

                route = derive_route_from_target(meta_data.get("url", ""), meta_data.get("apply_url", ""))
                raw_stats[route]["tried"] += 1

                if raw_st == "SUBMITTED":
                    raw_stats[route]["SUBMITTED"] += 1
                elif "CAPTCHA" in raw_st:
                    raw_stats[route]["CAPTCHA"] += 1
                elif "ACCOUNT" in raw_st:
                    raw_stats[route]["ACCOUNT-ONLY"] += 1
                else:
                    raw_stats[route]["OTHER"] += 1
            except Exception as exc:
                logger.debug(f"[RouteCalibrator] Error reading {p}: {exc}")

    result_table: dict[str, dict[str, Any]] = {}

    for route, s in raw_stats.items():
        tried = s["tried"]
        submitted = s["SUBMITTED"]
        captcha = s["CAPTCHA"]
        account = s["ACCOUNT-ONLY"]
        submitted_pct = round((submitted / tried * 100), 1) if tried > 0 else 0.0

        calibrated_kind = None
        # Rule 1: at least 5 tries and at least 50% SUBMITTED -> 'auto' (fixes Ashby)
        if tried >= 5 and (submitted / tried) >= 0.50:
            calibrated_kind = "auto"
        # Rule 2: at least 5 tries and 0 SUBMITTED with mostly CAPTCHA or ACCOUNT-ONLY -> keep captcha/account
        elif tried >= 5 and submitted == 0 and (captcha > 0 or account > 0):
            calibrated_kind = "captcha" if captcha >= account else "account"

        result_table[route] = {
            "tried": tried,
            "SUBMITTED": submitted,
            "CAPTCHA": captcha,
            "ACCOUNT-ONLY": account,
            "submitted_pct": submitted_pct,
            "apply_kind": calibrated_kind,
        }

    try:
        out_file.parent.mkdir(parents=True, exist_ok=True)
        out_file.write_text(json.dumps(result_table, indent=2), encoding="utf-8")
        logger.info(f"[RouteCalibrator] Saved route calibrations for {len(result_table)} routes to {out_file}")
    except Exception as exc:
        logger.warning(f"[RouteCalibrator] Could not write {out_file}: {exc}")

    return result_table


def load_route_calibrations(json_path: Optional[Path] = None) -> dict[str, str]:
    """Load route -> calibrated apply_kind mapping from route_success.json."""
    target_file = json_path or ROUTE_SUCCESS_FILE
    if not target_file.exists():
        # Calibrate once if file does not exist yet
        calibrated = calibrate_route_success(output_json=target_file)
        return {r: data["apply_kind"] for r, data in calibrated.items() if data.get("apply_kind")}

    try:
        data = json.loads(target_file.read_text(encoding="utf-8"))
        return {r: d["apply_kind"] for r, d in data.items() if isinstance(d, dict) and d.get("apply_kind")}
    except Exception:
        return {}


def apply_calibrated_routes_to_db(db, route_calibrations: Optional[dict[str, str]] = None) -> int:
    """
    Update jobs in SQLite database whose route has been calibrated to 'auto'
    (specifically unlocking Ashby rows where apply_kind was previously 'captcha').
    Returns count of updated rows.
    """
    mapping = route_calibrations or load_route_calibrations()
    if not mapping:
        return 0

    total_updated = 0
    with db._get_connection() as conn:
        for route, kind in mapping.items():
            if kind == "auto":
                # Unlock rows on this route previously parked as captcha or unknown
                cur = conn.execute(
                    """
                    UPDATE jobs
                    SET apply_kind = 'auto'
                    WHERE (ats = ? OR apply_route = ? OR lower(url) LIKE ? OR lower(apply_url) LIKE ?)
                      AND (apply_kind IS NULL OR apply_kind IN ('captcha', 'unknown'))
                      AND closed_at IS NULL
                    """,
                    (route, route, f"%{route}%", f"%{route}%"),
                )
                if cur.rowcount > 0:
                    total_updated += cur.rowcount
                    logger.info(f"[RouteCalibrator] Updated {cur.rowcount} rows for route '{route}' to apply_kind='auto'.")

    return total_updated
