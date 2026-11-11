"""Watchdog for the paper runner: run it OUTSIDE the runner (cron, a systemd timer, another machine).

  python3 scripts/watchdog.py --state-dir var/runner                 # check once; exit 0 ok / 1 warning / 2 critical
  python3 scripts/watchdog.py --state-dir var/runner --loop          # keep checking every --interval seconds

It reads the runner's status file (its proof of life), the incident log, the kill switch and (for LIVE) the
LIVE gate, prints what it finds, and alerts on CHANGE through the configured destinations (QUANT_ALERT_*
environment variables, see packages/common/alert_sinks.py; history in <state-dir>/alerts.jsonl).
A dead runner cannot report itself — this is what does.

A watchdog on the same machine dies with it. For that, set QUANT_WATCHDOG_PING_URL to an external dead-man's-switch
service (healthchecks.io style): the watchdog pings it after every check that is NOT critical, and the service
alerts you when the pings stop (host down, watchdog dead, or the runner critical)."""
from __future__ import annotations

import argparse
import logging
import os
import sys
import time
import urllib.error
import urllib.parse
import urllib.request
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


def ping(url: str, timeout: float = 10.0) -> bool:
    """GET the dead-man's-switch URL. https only (loopback http for tests). Failure is logged, never raised,
    and the URL (a secret) is never printed."""
    parts = urllib.parse.urlsplit(url)
    if not (parts.scheme == "https" or (parts.scheme == "http" and parts.hostname in ("127.0.0.1", "localhost"))):
        logging.getLogger("quant.watchdog").error("QUANT_WATCHDOG_PING_URL must be https; not pinging")
        return False
    try:
        with urllib.request.urlopen(url, timeout=timeout) as r:
            r.read(64)
        return True
    except (urllib.error.URLError, TimeoutError, OSError) as e:
        logging.getLogger("quant.watchdog").error("dead-man ping failed (%s)", type(e).__name__)
        return False


def check_once(state: Path, expect_running: bool, alert: bool, now: datetime | None = None,
               ping_url: str | None = None) -> int:
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
    code = exit_code(findings)
    if ping_url and code < 2:                    # silence on CRITICAL is itself the alarm
        ping(ping_url)
    return code


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
    ping_url = os.environ.get("QUANT_WATCHDOG_PING_URL") or None
    if not args.loop:
        return check_once(state, not args.expect_stopped, not args.no_alert, ping_url=ping_url)
    try:
        while True:
            check_once(state, not args.expect_stopped, not args.no_alert, ping_url=ping_url)
            time.sleep(args.interval)
    except KeyboardInterrupt:                    # Ctrl+C is how a person stops a console watchdog: not an error
        print("watchdog stopped")
        return 0


if __name__ == "__main__":
    sys.exit(main())
