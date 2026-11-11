"""Human intent CLI (MASTER SPEC §76-79): say you want to buy something, read the analysis, confirm.

  python3 scripts/human_intent.py submit AAPL --operator NAME --reason "why"   # a wish, NOT an order
  python3 scripts/human_intent.py list
  python3 scripts/human_intent.py show HI-20260811-0001                        # the analysis to read
  python3 scripts/human_intent.py confirm HI-20260811-0001 --operator NAME     # only once ANALYZED
  python3 scripts/human_intent.py cancel HI-20260811-0001 --operator NAME --reason "why"

A confirmed intent is offered to the next trading session as a candidate. It goes through the SAME gates
as an AI candidate (stop plan, sizing, allocation, Audit AI, Master Risk Controller) and can be refused
by any of them; the confirmation expires after 2 days. Only buying is supported. Log: QUANT_INTENT_LOG
(default var/human_intents.jsonl)."""
from __future__ import annotations

import argparse
import json
import sys
from datetime import datetime, timezone
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from services.decision.human_intent import IntentAction
from services.decision.intent_inbox import IntentError, IntentInbox, IntentInboxUnreadable


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    sub = parser.add_subparsers(dest="cmd", required=True)
    s = sub.add_parser("submit")
    s.add_argument("symbol")
    s.add_argument("--operator", required=True)
    s.add_argument("--reason", required=True)
    s.add_argument("--action", default="WANT_BUY", help="only WANT_BUY is supported")
    sub.add_parser("list")
    sub.add_parser("show").add_argument("intent_id")
    c = sub.add_parser("confirm")
    c.add_argument("intent_id")
    c.add_argument("--operator", required=True)
    x = sub.add_parser("cancel")
    x.add_argument("intent_id")
    x.add_argument("--operator", required=True)
    x.add_argument("--reason", required=True)
    args = parser.parse_args(argv)

    box, now = IntentInbox.from_env(), datetime.now(timezone.utc)
    try:
        if args.cmd == "submit":
            iid = box.submit(args.symbol, IntentAction(args.action), args.reason, args.operator, now)
            print(f"{iid} submitted (a wish, not an order). The system will attach an analysis during market "
                  f"hours; read it with 'show', then 'confirm'.")
        elif args.cmd == "list":
            recs = box.records()
            for r in recs:
                print(f"{r.intent_id:20} {r.symbol:6} {r.status.value:10} by {r.submitted_by:10} {r.stated_reason[:50]}")
            print(f"{len(recs)} intents; {sum(r.is_open for r in recs)} open")
        elif args.cmd == "show":
            r = box.get(args.intent_id)
            print(json.dumps({"intent_id": r.intent_id, "symbol": r.symbol, "status": r.status.value,
                              "reason": r.stated_reason, "outcome": r.outcome, "history": r.history,
                              "analysis": r.analysis}, indent=2, default=str))
        elif args.cmd == "confirm":
            box.confirm(args.intent_id, args.operator, now)
            print(f"{args.intent_id} confirmed by {args.operator}; the next session will offer it to the pipeline "
                  f"(valid 2 days). Audit and Risk can still refuse it.")
        else:
            box.cancel(args.intent_id, args.operator, args.reason, now)
            print(f"{args.intent_id} cancelled")
    except (IntentError, IntentInboxUnreadable) as e:
        print(f"refused: {e}", file=sys.stderr)
        return 2
    return 0


if __name__ == "__main__":
    sys.exit(main())
