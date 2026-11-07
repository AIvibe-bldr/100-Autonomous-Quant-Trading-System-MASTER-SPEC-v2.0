"""Operator kill switch CLI (SAFETY_AUDIT F5). Halts NEW ENTRIES for any runner
that reads the same flag file (QUANT_KILL_SWITCH_FILE, default var/kill_switch.json).
Exits and protective stops are not affected. There is deliberately no automatic
resume: `clear` removes the flag, and a runner that already halted stays halted
until it is restarted.

  python3 scripts/kill_switch.py status
  python3 scripts/kill_switch.py engage --operator NAME --reason "why"
  python3 scripts/kill_switch.py clear  --operator NAME --reason "why"
"""
from __future__ import annotations

import argparse
import sys
from datetime import datetime, timezone
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from packages.common.kill_switch import KillSwitch, describe


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    sub = parser.add_subparsers(dest="cmd", required=True)
    sub.add_parser("status")
    for name in ("engage", "clear"):
        p = sub.add_parser(name)
        p.add_argument("--operator", required=True)
        p.add_argument("--reason", required=True)
    args = parser.parse_args(argv)

    ks = KillSwitch.from_env()
    if args.cmd == "status":
        print(f"kill switch ({ks.path}): {describe(ks.status())}")
        return 0
    try:
        getattr(ks, args.cmd)(operator=args.operator, reason=args.reason,
                              at=datetime.now(timezone.utc))
    except ValueError as e:
        print(f"refused: {e}", file=sys.stderr)
        return 2
    print(f"kill switch {args.cmd}: done — {describe(ks.status())}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
