"""7 Nov 2026: watchdog_check with its default paths referenced an undefined CRASH_FILE, so the daemon never started.
The other tests always pass a custom heartbeat_path and never reached that branch."""
import supervisor


def test_watchdog_check_default_paths_restart(tmp_path, monkeypatch):
    monkeypatch.setattr(supervisor, "HEARTBEAT_FILE", tmp_path / "daemon.heartbeat")
    monkeypatch.setattr(supervisor, "PID_FILE", tmp_path / "daemon.pid")
    monkeypatch.setattr(supervisor, "CRASH_HISTORY_FILE", tmp_path / "daemon_crashes.json")
    monkeypatch.setattr(supervisor, "WATCHDOG_LOG", tmp_path / "watchdog.log")
    started = []
    out = supervisor.watchdog_check(start_fn=lambda: started.append(1) or 4242, stop_fn=lambda pid: None,
                                    is_running_fn=lambda pid: False, log_path=tmp_path / "watchdog.log")
    assert out == "restarted" and started == [1]
