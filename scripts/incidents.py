"""Incident log CLI (MASTER SPEC §69, §71). A MAJOR fault stops the runner and stays OPEN until a
named human resolves it; the runner refuses to start while one is open (QUANT_INCIDENT_LOG, default
var/incidents.jsonl).

  python3 scripts/incidents.py list
  python3 scripts/incidents.py show INC-0003          # includes the postmortem skeleton to fill in
  python3 scripts/incidents.py resolve INC-0003 --operator NAME --reason "root cause and fix"
"""
from __future__ import annotations

import argparse
import json
import sys
from datetime import datetime, timezone
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from services.supervisor.incident_log import IncidentLog


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    sub = parser.add_subparsers(dest="cmd", required=True)
    sub.add_parser("list")
    sub.add_parser("show").add_argument("incident_id")
    r = sub.add_parser("resolve")
    r.add_argument("incident_id")
    r.add_argument("--operator", required=True)
    r.add_argument("--reason", required=True)
    args = parser.parse_args(argv)

    log = IncidentLog.from_env()
    if args.cmd == "list":
        items = log.incidents()
        for i in items:
            print(f"{i['incident_id']:15} {i['severity']:6} {'resolved' if i['resolved'] else 'OPEN':8} "
                  f"{i['service']:10} {i['description'][:80]}")
        print(f"{len(items)} incidents; {len(log.open_major())} open major")
        return 0
    if args.cmd == "show":
        match = [i for i in log.incidents() if i["incident_id"] == args.incident_id]
        if not match:
            print(f"unknown incident {args.incident_id}", file=sys.stderr)
            return 2
        print(json.dumps(match[0], indent=2, default=str))
        return 0
    try:
        log.record_resolution(args.incident_id, args.operator, args.reason,
                              datetime.now(timezone.utc), human_approved=True)
    except (ValueError, KeyError) as e:
        print(f"refused: {e}", file=sys.stderr)
        return 2
    print(f"{args.incident_id} resolved by {args.operator}; open major incidents: {len(log.open_major())}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
