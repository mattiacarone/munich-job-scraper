import time
from pathlib import Path
import supervisor


def test_autostart_scripts_exist_and_configured():
    install_script = Path("install_autostart.ps1.disabled")  # 5 Nov 2026: autostart disabled on purpose (Mattia chooses single or continuous runs)
    uninstall_script = Path("uninstall_autostart.ps1")

    assert install_script.exists()
    assert uninstall_script.exists()

    install_content = install_script.read_text(encoding="utf-8")
    assert "MunichScraper" in install_content
    assert "scraper.ps1" in install_content
    assert "Register-ScheduledTask" in install_content
    assert "Hidden" in install_content

    uninstall_content = uninstall_script.read_text(encoding="utf-8")
    assert "MunichScraper" in uninstall_content
    assert "Unregister-ScheduledTask" in uninstall_content


def test_supervisor_crash_backoff(tmp_path):
    crash_file = tmp_path / "crashes.json"
    now = 10000.0

    # 1 crash -> no backoff
    supervisor.record_crash(now - 1200.0, path=crash_file)
    in_backoff, wait_rem = supervisor.check_crash_backoff(now_ts=now, path=crash_file)
    assert in_backoff is False

    # 2 crashes -> no backoff
    supervisor.record_crash(now - 600.0, path=crash_file)
    in_backoff, wait_rem = supervisor.check_crash_backoff(now_ts=now, path=crash_file)
    assert in_backoff is False

    # 3rd crash within 1 hour (at now - 60s) -> backoff triggered (15 min / 900s)
    supervisor.record_crash(now - 60.0, path=crash_file)
    in_backoff, wait_rem = supervisor.check_crash_backoff(now_ts=now, path=crash_file)
    assert in_backoff is True
    assert 0 < wait_rem <= 900.0

    # After 15 minutes (901s after 3rd crash) -> backoff clears
    in_backoff_after, _ = supervisor.check_crash_backoff(now_ts=now + 850.0, path=crash_file)
    assert in_backoff_after is False

