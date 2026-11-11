"""Ops dashboard: a read-only web page onto a running (or stopped) paper runner's state directory.

  python3 scripts/run_ops_dashboard.py --state-dir var/runner                      # http://127.0.0.1:8080/
  QUANT_OPS_TOKEN=... python3 scripts/run_ops_dashboard.py --state-dir var/runner --host 0.0.0.0

It only READS (session database opened read-only, status file, incident/intent logs, alert history). It cannot
start, stop or steer anything: kill switch, LIVE gate, intents and incidents stay CLI-only with a named operator.
Binding to anything but the loopback address requires QUANT_OPS_TOKEN (and you should put TLS in front)."""
from __future__ import annotations

import argparse
import os
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

LOOPBACK = {"127.0.0.1", "localhost", "::1"}


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("--state-dir", default="var/runner")
    parser.add_argument("--host", default="127.0.0.1")
    parser.add_argument("--port", type=int, default=8080)
    args = parser.parse_args(argv)

    token = os.environ.get("QUANT_OPS_TOKEN") or None
    if args.host not in LOOPBACK and not token:
        print("refusing to listen beyond the loopback address without QUANT_OPS_TOKEN set", file=sys.stderr)
        return 2
    state = Path(args.state_dir)
    if not state.is_dir():
        print(f"no such state directory: {state}", file=sys.stderr)
        return 2

    import uvicorn

    from apps.api.ops import create_ops_app
    from services.supervisor.ops_state import OpsState

    print(f"ops dashboard (read-only) for {state} at http://{args.host}:{args.port}/"
          + ("  [token required]" if token else ""))
    uvicorn.run(create_ops_app(OpsState(state), token=token), host=args.host, port=args.port, log_level="warning")
    return 0


if __name__ == "__main__":
    sys.exit(main())
