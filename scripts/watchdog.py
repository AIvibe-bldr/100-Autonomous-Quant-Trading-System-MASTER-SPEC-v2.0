"""Watchdog for the paper runner: run it OUTSIDE the runner (cron, a systemd timer, another machine).

  python3 scripts/watchdog.py --state-dir var/runner                 # check once; exit 0 ok / 1 warning / 2 critical
  python3 scripts/watchdog.py --state-dir var/runner --loop          # keep checking every --interval seconds

It reads the runner's status file (its proof of life), the incident log, the kill switch and (for LIVE) the
LIVE gate, prints what it finds, and alerts on CHANGE through the configured destinations (QUANT_ALERT_*
environment variables, see packages/common/alert_sinks.py; history in <state-dir>/alerts.jsonl).
A dead runner cannot report itself — this is what does."""
from __future__ import annotations

import argparse
import logging
import sys
import time
from datetime import datetime, timezone
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from packages.common.alert_sinks import alert_sink_from_env
from packages.common.calendar import TradingCalendar
from packages.common.kill_switch import KillSwitch
from packages.common.live_gate import LiveGate
from services.supervisor.incident_log import IncidentLog
from services.supervisor.status_file import FILE_NAME
from services.supervisor.watchdog import (
    WatchdogConfig,
    alert_on_change,
    evaluate,
    exit_code,
    gather,
    worst,
)


def check_once(state: Path, expect_running: bool, alert: bool, now: datetime | None = None) -> int:
    now = now or datetime.now(timezone.utc)
    status_path = state / FILE_NAME
    probe = gather(status_path, IncidentLog.from_env(), KillSwitch.from_env(), None, now)
    if probe.status is not None and probe.status.environment == "LIVE":
        probe.live_gate_open = LiveGate.from_env().status(now).is_open
    findings = evaluate(probe, TradingCalendar(), WatchdogConfig(), expect_running=expect_running)
    for f in findings:
        print(f"{f.level.value:8} {f.code:18} {f.message}")
    print(f"watchdog: {worst(findings).value} ({len(findings)} finding(s)) at {now:%Y-%m-%d %H:%M:%S}Z")
    if alert:
        sink = alert_sink_from_env(state / "alerts.jsonl")
        alert_on_change(findings, state / "watchdog_state.json", sink, now)
    return exit_code(findings)


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("--state-dir", default="var/runner")
    parser.add_argument("--loop", action="store_true")
    parser.add_argument("--interval", type=float, default=60.0)
    parser.add_argument("--no-alert", action="store_true", help="print findings only")
    parser.add_argument("--expect-stopped", action="store_true", help="a runner stopped on request is not a warning")
    args = parser.parse_args(argv)
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(name)s %(message)s")
    state = Path(args.state_dir)
    if not args.loop:
        return check_once(state, not args.expect_stopped, not args.no_alert)
    code = 0
    while True:
        code = check_once(state, not args.expect_stopped, not args.no_alert)
        time.sleep(args.interval)


if __name__ == "__main__":
    sys.exit(main())
