"""LIVE gate CLI. Records or revokes the human approval that LIVE entries require
(QUANT_LIVE_GATE_FILE, default var/live_gate.json). Approvals expire (max 14 days).
This system sends no real orders today; the gate exists so that stays true until a human says so.

  python3 scripts/live_gate.py status
  python3 scripts/live_gate.py approve --operator NAME --reason "why" --days 7 --stage 0.1
  python3 scripts/live_gate.py revoke  --operator NAME --reason "why"
"""
from __future__ import annotations

import argparse
import sys
from datetime import datetime, timedelta, timezone
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from packages.common.live_gate import LiveGate


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    sub = parser.add_subparsers(dest="cmd", required=True)
    sub.add_parser("status")
    a = sub.add_parser("approve")
    a.add_argument("--days", type=float, required=True)
    a.add_argument("--stage", type=float, required=True, help="max exposure stage in (0, 1]")
    r = sub.add_parser("revoke")
    for p in (a, r):
        p.add_argument("--operator", required=True)
        p.add_argument("--reason", required=True)
    args = parser.parse_args(argv)

    gate, now = LiveGate.from_env(), datetime.now(timezone.utc)
    try:
        if args.cmd == "approve":
            gate.approve(args.operator, args.reason, now, now + timedelta(days=args.days), args.stage)
        elif args.cmd == "revoke":
            gate.revoke(args.operator, args.reason, now)
    except ValueError as e:
        print(f"refused: {e}", file=sys.stderr)
        return 2
    s = gate.status(now)
    print(f"LIVE gate ({gate.path}): {'OPEN' if s.is_open else 'CLOSED'} — {s.detail}"
          + (f"; by {s.operator}, stage <= {s.max_exposure_stage}, until {s.expires_at}" if s.is_open else ""))
    return 0


if __name__ == "__main__":
    sys.exit(main())
