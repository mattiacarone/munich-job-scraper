"""
Daemon supervision (Brief 12 sections 1 and 4).

  - heartbeat helpers: the daemon touches daemon.heartbeat after every source, so a long cycle is not
    mistaken for a dead one
  - run_forever(): next cycle starts when the previous one ends (no "maximum number of running
    instances reached" skips)
  - watchdog_check(): restarts the daemon when daemon.heartbeat is older than 30 minutes (or the
    daemon process is gone) and logs every restart to watchdog.log
  - per-source minimum poll intervals (stepstone / xing weekly, arbeitnow daily)

Run the watchdog:   python supervisor.py            (loop, checks every 5 minutes)
                    python supervisor.py --once      (single check, for Windows Task Scheduler)
"""

from __future__ import annotations

import logging
import os
import subprocess
import sys
import time
from datetime import datetime, timezone
from logging.handlers import RotatingFileHandler
from pathlib import Path
from typing import Callable, Optional

BASE_DIR = Path(__file__).resolve().parent
HEARTBEAT_FILE = BASE_DIR / "daemon.heartbeat"
PID_FILE = BASE_DIR / "daemon.pid"
WATCHDOG_LOG = BASE_DIR / "watchdog.log"
DAEMON_LOG = BASE_DIR / "daemon.log"

# 6 Nov 2026: hidden console for every process the supervisor starts or checks (no terminal windows on Mattia's screen)
NO_WINDOW = getattr(subprocess, "CREATE_NO_WINDOW", 0x08000000) if os.name == "nt" else 0

HEARTBEAT_MAX_AGE_MIN = 30.0
WATCHDOG_INTERVAL_SECONDS = 300
CYCLE_PAUSE_SECONDS = 600  # Brief 13 Section 2: 10 minute cycle interval

# Brief 13 Section 3: Supervisor crash backoff
CRASH_HISTORY_FILE = BASE_DIR / "daemon_crashes.json"
MAX_CRASHES_PER_HOUR = 3
CRASH_WINDOW_SECONDS = 3600.0
CRASH_BACKOFF_SECONDS = 900.0  # 15 minutes


def load_crash_history(path: Optional[Path] = None) -> list[float]:
    p = path or CRASH_HISTORY_FILE
    if not p.exists():
        return []
    try:
        import json
        data = json.loads(p.read_text(encoding="utf-8"))
        if isinstance(data, list):
            return [float(ts) for ts in data]
    except Exception:
        pass
    return []


def record_crash(ts: float, path: Optional[Path] = None) -> list[float]:
    p = path or CRASH_HISTORY_FILE
    history = load_crash_history(p)
    history.append(ts)
    cutoff = ts - 86400.0
    history = [t for t in history if t >= cutoff]
    try:
        import json
        p.write_text(json.dumps(history), encoding="utf-8")
    except Exception:
        pass
    return history


def check_crash_backoff(
    now_ts: Optional[float] = None,
    path: Optional[Path] = None,
    max_crashes: int = MAX_CRASHES_PER_HOUR,
    window_s: float = CRASH_WINDOW_SECONDS,
    backoff_s: float = CRASH_BACKOFF_SECONDS,
) -> tuple[bool, float]:
    """
    Returns (in_backoff, wait_remaining).
    If daemon crashed max_crashes (3) times in the last window_s (3600s),
    it pauses backoff_s (900s / 15 min) before the next restart.
    """
    now = now_ts or time.time()
    history = load_crash_history(path)
    recent = [t for t in history if (now - t) <= window_s]
    if len(recent) >= max_crashes:
        latest_crash = recent[-1]
        elapsed = now - latest_crash
        if elapsed < backoff_s:
            return True, backoff_s - elapsed
    return False, 0.0

# Brief 12 section 4 & Brief 14 B1: stepstone weekly; arbeitnow, arbeitsagentur, munichstartup once a day; lever every third cycle
SOURCE_MIN_INTERVAL_HOURS: dict[str, float] = {
    "stepstone": 168.0,
    "arbeitnow": 24.0,
    "arbeitsagentur": 24.0,
    "munichstartup": 24.0,
}

SOURCE_CYCLE_MODULO: dict[str, int] = {
    "lever": 3,
}

logger = logging.getLogger(__name__)
if not any(isinstance(h, RotatingFileHandler) for h in logger.handlers):
    _watchdog_handler = RotatingFileHandler(
        WATCHDOG_LOG,
        maxBytes=1 * 1024 * 1024,
        backupCount=2,
        encoding="utf-8",
    )
    _watchdog_handler.setFormatter(logging.Formatter("%(asctime)s [%(levelname)s] %(message)s"))
    logger.addHandler(_watchdog_handler)
    logger.setLevel(logging.INFO)


# ---------------------------------------------------------------------------
# heartbeat / pid
# ---------------------------------------------------------------------------
def touch_heartbeat(path: Optional[Path] = None) -> None:
    p = path or HEARTBEAT_FILE
    try:
        p.write_text(datetime.now(timezone.utc).isoformat(), encoding="utf-8")
    except Exception as exc:  # noqa: BLE001
        logger.debug(f"[Heartbeat] could not write {p}: {exc}")


def write_pid(path: Optional[Path] = None) -> None:
    (path or PID_FILE).write_text(str(os.getpid()), encoding="utf-8")


def heartbeat_age_minutes(path: Optional[Path] = None, now: Optional[datetime] = None) -> Optional[float]:
    p = path or HEARTBEAT_FILE
    if not p.exists():
        return None
    try:
        dt = datetime.fromisoformat(p.read_text(encoding="utf-8").strip())
        dt = dt if dt.tzinfo else dt.replace(tzinfo=timezone.utc)
    except Exception:
        return None
    return ((now or datetime.now(timezone.utc)) - dt).total_seconds() / 60.0


def pid_is_running(pid: int) -> bool:
    """Windows-safe process check (never signals the process)."""
    if pid <= 0:
        return False
    try:
        if os.name == "nt":
            out = subprocess.run(
                ["tasklist", "/FI", f"PID eq {pid}", "/NH"], capture_output=True, text=True, timeout=15,
                creationflags=NO_WINDOW,
            ).stdout
            return str(pid) in out
        os.kill(pid, 0)
        return True
    except Exception:
        return False


# ---------------------------------------------------------------------------
# cycle loop
# ---------------------------------------------------------------------------
def run_forever(
    run_cycle: Callable[[], None],
    pause_seconds: float = CYCLE_PAUSE_SECONDS,
    stop_after: Optional[int] = None,
    sleep: Callable[[float], None] = time.sleep,
    on_error: Optional[Callable[[Exception], None]] = None,
) -> int:
    """Run cycles back to back: the next one starts pause_seconds after the previous one ended."""
    n = 0
    while stop_after is None or n < stop_after:
        try:
            run_cycle()
        except (KeyboardInterrupt, SystemExit):
            raise
        except Exception as exc:  # noqa: BLE001
            logger.error(f"[Daemon] cycle crashed: {exc}", exc_info=True)
            if on_error:
                on_error(exc)
        n += 1
        touch_heartbeat()
        if stop_after is not None and n >= stop_after:
            break
        sleep(pause_seconds)
    return n


# ---------------------------------------------------------------------------
# per-source poll cadence
# ---------------------------------------------------------------------------
def source_is_due(
    db,
    name: str,
    now: Optional[datetime] = None,
    intervals: Optional[dict] = None,
    cycle_modulo: Optional[dict] = None,
) -> bool:
    import json

    mod = (cycle_modulo or SOURCE_CYCLE_MODULO).get(name)
    if mod and mod > 1:
        skip_raw = db.get_metadata(f"cycle_skip_rem_{name}")
        try:
            skip_rem = int(skip_raw) if skip_raw is not None else 0
        except Exception:
            skip_rem = 0
        if skip_rem > 0:
            db.set_metadata(f"cycle_skip_rem_{name}", str(skip_rem - 1))
            return False

    hours = (intervals or SOURCE_MIN_INTERVAL_HOURS).get(name)
    if not hours:
        return True
    last = db.get_metadata(f"last_run_source_{name}")
    if not last and hasattr(db, "get_recent_cycle_metrics"):
        try:
            for cy in db.get_recent_cycle_metrics(limit=10):
                postings = json.loads(cy.get("per_source_postings") or "{}")
                if postings.get(name, 0) > 0 and cy.get("started_at"):
                    last = cy["started_at"]
                    db.set_metadata(f"last_run_source_{name}", last)
                    break
        except Exception:
            pass
    if not last:
        return True
    try:
        dt = datetime.fromisoformat(last)
        dt = dt if dt.tzinfo else dt.replace(tzinfo=timezone.utc)
    except Exception:
        return True
    return ((now or datetime.now(timezone.utc)) - dt).total_seconds() >= hours * 3600.0


def mark_source_run(db, name: str, now: Optional[datetime] = None, cycle_modulo: Optional[dict] = None) -> None:
    db.set_metadata(f"last_run_source_{name}", (now or datetime.now(timezone.utc)).isoformat())
    mod = (cycle_modulo or SOURCE_CYCLE_MODULO).get(name)
    if mod and mod > 1:
        db.set_metadata(f"cycle_skip_rem_{name}", str(mod - 1))


# ---------------------------------------------------------------------------
# watchdog
# ---------------------------------------------------------------------------
def _log_restart(message: str, log_path: Optional[Path] = None) -> None:
    if log_path is not None and log_path != WATCHDOG_LOG:
        try:
            h = RotatingFileHandler(log_path, maxBytes=1 * 1024 * 1024, backupCount=2, encoding="utf-8")
            h.setFormatter(logging.Formatter("%(asctime)s [%(levelname)s] %(message)s"))
            rec = logger.makeRecord(logger.name, logging.WARNING, __file__, 0, message, (), None)
            h.emit(rec)
            h.close()
        except Exception:
            pass
    logger.warning(message)


def start_daemon() -> int:
    """Start `python main.py` detached; stdout and stderr go to DEVNULL (main.py logs to scraper.log). Returns the new pid."""
    flags = 0
    if os.name == "nt":
        # 6 Nov 2026 (Mattia: terminal windows kept popping up): DETACHED_PROCESS left main.py with no console at all, so every console
        # program it starts (tasklist ...) opened a new visible window. CREATE_NO_WINDOW gives it a hidden console that
        # all its children inherit, so nothing it launches shows a window.
        flags = NO_WINDOW | getattr(subprocess, "CREATE_NEW_PROCESS_GROUP", 0x200)
    proc = subprocess.Popen(
        [sys.executable, str(BASE_DIR / "main.py")],
        cwd=str(BASE_DIR),
        stdout=subprocess.DEVNULL,
        stderr=subprocess.DEVNULL,
        stdin=subprocess.DEVNULL,
        creationflags=flags,
    )
    PID_FILE.write_text(str(proc.pid), encoding="utf-8")
    touch_heartbeat()  # grace period so the next check does not restart it again
    return proc.pid


def stop_daemon(pid: int) -> None:
    try:
        if os.name == "nt":
            subprocess.run(["taskkill", "/PID", str(pid), "/T", "/F"], capture_output=True, timeout=30, creationflags=NO_WINDOW)
        else:
            os.kill(pid, 15)
    except Exception:
        pass


def watchdog_check(
    heartbeat_path: Optional[Path] = None,
    pid_path: Optional[Path] = None,
    max_age_min: float = HEARTBEAT_MAX_AGE_MIN,
    start_fn: Callable[[], int] = start_daemon,
    stop_fn: Callable[[int], None] = stop_daemon,
    is_running_fn: Callable[[int], bool] = pid_is_running,
    log_path: Optional[Path] = None,
    now: Optional[datetime] = None,
) -> str:
    """
    Returns 'ok' or 'restarted'. Restarts when daemon.heartbeat is older than max_age_min (or missing),
    or when the recorded daemon pid is gone. Every restart is appended to watchdog.log.
    """
    hb = heartbeat_path or HEARTBEAT_FILE
    pp = pid_path or PID_FILE
    age = heartbeat_age_minutes(hb, now)

    pid = 0
    if pp.exists():
        try:
            pid = int(pp.read_text(encoding="utf-8").strip())
        except Exception:
            pid = 0
    alive = bool(pid) and is_running_fn(pid)

    reason = None
    if age is None:
        reason = "no readable heartbeat"
    elif age > max_age_min:
        reason = f"heartbeat {age:.1f} min old (> {max_age_min:.0f})"
    elif not alive:
        reason = f"daemon pid {pid or 'none'} is not running (heartbeat {age:.1f} min old)"

    if reason is None:
        return "ok"

    # Brief 13 Section 3: if daemon crashed 3 times within 1 hour, wait 15 min before restarting
    now_ts = now.timestamp() if now else time.time()
    crash_path = (hb.parent / "daemon.crashes.json") if (heartbeat_path is not None and hb != HEARTBEAT_FILE) else CRASH_HISTORY_FILE  # 7 Nov 2026: was CRASH_FILE (undefined), so every watchdog check failed and the daemon never started
    in_backoff, wait_rem = check_crash_backoff(now_ts=now_ts, path=crash_path)
    if in_backoff:
        _log_restart(
            f"Watchdog backoff: daemon crashed {MAX_CRASHES_PER_HOUR} times in the last hour; pausing 15 min ({wait_rem:.0f}s remaining) before next restart. Trigger reason: {reason}",
            log_path,
        )
        return "backoff"

    record_crash(now_ts, path=crash_path)

    if alive:
        stop_fn(pid)  # hung daemon: stop it before starting a fresh one
    new_pid = start_fn()
    try:
        pp.write_text(str(new_pid), encoding="utf-8")
    except Exception:
        pass
    touch_heartbeat(hb)
    _log_restart(f"Watchdog restart: {reason}; old pid {pid or 'none'} -> new pid {new_pid}", log_path)
    return "restarted"


def watchdog_loop(interval_seconds: float = WATCHDOG_INTERVAL_SECONDS) -> None:
    logging.basicConfig(level=logging.INFO, format="%(asctime)s [%(levelname)s] %(message)s")
    logger.info(f"Watchdog running (check every {interval_seconds:.0f}s, heartbeat limit {HEARTBEAT_MAX_AGE_MIN:.0f} min).")
    while True:
        try:
            watchdog_check()
        except Exception as exc:  # noqa: BLE001
            logger.error(f"Watchdog check failed: {exc}")
        time.sleep(interval_seconds)


if __name__ == "__main__":
    if "--once" in sys.argv:
        logging.basicConfig(level=logging.INFO, format="%(asctime)s [%(levelname)s] %(message)s")
        print(watchdog_check())
    else:
        watchdog_loop()
