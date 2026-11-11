"""The runner as a real process: it starts, writes its status, and a stop request ends it cleanly and promptly.

A stop request that takes a minute to act (or kills the process without recording STOPPED) leaves an operator
closing the window by hand. Windows is the case that needs checking: its console signals differ from POSIX."""
import json
import os
import signal
import subprocess
import sys
import time
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[2]
SCRIPT = ROOT / "scripts" / "run_paper_runner.py"
STOP_WITHIN_SECONDS = 25          # one heartbeat is 60 s: a stop that waits for the next tick is too slow


def _state(path: Path):
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return None


def test_the_runner_process_stops_cleanly_and_promptly_on_a_stop_request(tmp_path):
    env = {k: v for k, v in os.environ.items() if "API_KEY" not in k and "APCA" not in k}
    flags = subprocess.CREATE_NEW_PROCESS_GROUP if os.name == "nt" else 0
    log = tmp_path / "runner.log"
    with log.open("w", encoding="utf-8") as out:
        proc = subprocess.Popen([sys.executable, str(SCRIPT), "--state-dir", str(tmp_path / "state")],
                                cwd=ROOT, env=env, stdout=out, stderr=subprocess.STDOUT, creationflags=flags)
        try:
            status_file = tmp_path / "state" / "runner_status.json"
            deadline = time.monotonic() + 90
            while time.monotonic() < deadline and proc.poll() is None:
                st = _state(status_file)
                if st and st.get("state") in ("WAITING", "RUNNING"):
                    break
                time.sleep(0.5)
            assert proc.poll() is None, f"the runner exited early:\n{log.read_text(encoding='utf-8')[-1500:]}"
            assert _state(status_file), "the runner never wrote its status"

            time.sleep(2)                                           # let it settle into its tick wait
            sent = time.monotonic()
            proc.send_signal(signal.CTRL_BREAK_EVENT if os.name == "nt" else signal.SIGTERM)
            code = proc.wait(timeout=90)
            took = time.monotonic() - sent
        finally:
            if proc.poll() is None:
                proc.kill()
    text = log.read_text(encoding="utf-8")[-1500:]
    assert code == 0, f"exit code {code}:\n{text}"
    assert _state(status_file)["state"] == "STOPPED", text
    assert took <= STOP_WITHIN_SECONDS, f"the stop request took {took:.0f}s to act"
