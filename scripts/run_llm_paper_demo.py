"""Paper trading demo using the REAL LLM APIs for Decision / Skeptic /
Audit AI (MASTER SPEC §25, §27-31, ADDENDUM A3), instead of the deterministic
Mocks used everywhere else in this repo.

Requires `pip install anthropic` and Anthropic credentials (MY_ANTHROPIC_API_KEY,
or `ant auth login`) — Skeptic AI and Audit AI are Claude Opus, non-negotiably
(§25), so Anthropic credentials gate the whole real-LLM path. Without them
this falls back to the Mock stack automatically and says so — the script
always runs.

Decision AI is separately specified as GPT-5.6 Sol on OpenAI (§25):
`build_llm_stack` picks it automatically when `OPENAI_API_KEY` (plus
`pip install openai`) is also configured, and falls back to the same Claude
model as Skeptic/Audit otherwise — see `resolve_decision_agent` in
packages/common/llm_client.py.

Usage:  python3 scripts/run_llm_paper_demo.py [--days N]

Model tiers default to a cost-aware split (see packages/common/llm_client.py):
Decision AI runs on every scanned candidate, Skeptic AI only on BUY
candidates, Audit AI only on sized orders — so volume shrinks at each stage
while the configured model capability can rise.
"""
from __future__ import annotations

import argparse
import sys
from datetime import date, datetime, time, timezone
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from packages.common.clock import FrozenClock
from packages.common.llm_client import DEFAULT_MODEL_CONFIG, credentials_available
from services.market_data.universe import UniverseManager, UniverseSymbol
from services.reconciliation.engine import ReconciliationEngine
from tests.conftest import SYMBOLS, build_pipeline


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--days", type=int, default=1)
    parser.add_argument("--cash", type=float, default=670.0)
    args = parser.parse_args()

    universe = UniverseManager()
    for s in SYMBOLS:
        universe.add(UniverseSymbol(symbol=s, listed_from=date(2015, 1, 1)))
    clock = FrozenClock(current=datetime.combine(date(2026, 8, 10), time(15, 0),
                                                 tzinfo=timezone.utc))

    if credentials_available():
        from services.decision.claude_adapters import build_llm_stack

        cfg = DEFAULT_MODEL_CONFIG
        decision, skeptic, auditor = build_llm_stack(config=cfg)
        pipeline = build_pipeline(clock, universe, initial_cash=args.cash,
                                  decision_model=decision, skeptic_model=skeptic,
                                  auditor=auditor)
        # decision.name reflects whichever provider build_llm_stack actually
        # resolved (OpenAI if configured, Claude fallback otherwise) — never
        # trust cfg.decision.agent_id here, it only names the fallback (§25)
        print(f"=== LIVE LLM APIs ===\n"
              f"  Decision AI : {decision.name}\n"
              f"  Skeptic AI  : {cfg.skeptic.agent_id}\n"
              f"  Audit AI    : {cfg.audit.agent_id}  (separate agent, same model)")
    else:
        pipeline = build_pipeline(clock, universe, initial_cash=args.cash)
        print("=== No Anthropic credentials found — using deterministic Mock "
              "Decision/Skeptic/Audit AI instead (set MY_ANTHROPIC_API_KEY to use "
              "the real API) ===")

    for day in range(args.days):
        result = pipeline.run_session(clock.now())
        print(f"\n--- session {day + 1} ---")
        print(f"funnel: scanned {result.scanned} → candidates {result.candidates} → "
              f"decisions {result.decision_candidates} → skeptic_vetoes "
              f"{result.skeptic_vetoes} → audit_passed {result.audit_passed} → "
              f"risk_passed {result.risk_passed} → filled {result.orders_filled}")
        for f in result.fills:
            print(f"  FILL {f.side.value} {f.qty:g} {f.symbol} @ {f.price:.2f}")
        if not result.traded:
            print("  NO TRADE — top reasons:")
            for r in result.no_trade_reasons[:5]:
                print(f"    · {r}")
        pipeline.execution.broker.settle()
        from datetime import timedelta
        clock.current = clock.current + timedelta(days=1)

    prices = {s: pipeline.market_data.quote(s, clock.now()).mid
              for s in pipeline.ledger.positions}
    snap = pipeline.ledger.snapshot(prices)
    recon = ReconciliationEngine(broker=pipeline.execution.broker, ledger=pipeline.ledger,
                                 risk_controller=pipeline.risk_controller, cash_tolerance=1.0)
    report = recon.reconcile()
    print(f"\nequity ${snap.equity:,.2f} | reconciliation: "
          f"{'CONSISTENT' if report.consistent else 'MISMATCH'}")
    print(f"audit completeness: {pipeline.provenance.audit_completeness():.0%}")


if __name__ == "__main__":
    main()
