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
from packages.common.llm_usage import LLMUsageLedger
from services.cost_manager.engine import CostCategory, CostEntry
from packages.common.llm_client import DEFAULT_MODEL_CONFIG, credentials_available
from services.market_data.universe import UniverseManager, UniverseSymbol
from services.reconciliation.engine import ReconciliationEngine
from packages.common.kill_switch import KillSwitch
from tests.conftest import SYMBOLS, build_pipeline


def _print_decision_rationale(records, limit: int = 3) -> None:
    """Display-only: why each candidate got its action (read from the
    provenance records the pipeline already keeps — no extra LLM calls)."""
    print("\nDecision AI rationale per candidate:")
    for rec in records:
        out = rec.output
        if not out:
            err = (rec.result or {}).get("rejected", "no decision recorded")
            print(f"  {rec.decision_id}: {err}")
            continue
        print(f"  {out['symbol']}: {out['action']} (confidence {out['confidence']:.2f}, "
              f"horizon {out['expected_horizon']})")
        for label, key in (("evidence", "key_evidence"), ("counter", "counter_evidence"),
                           ("risks", "risk_factors"), ("unknowns", "unknowns")):
            for item in out[key][:limit]:
                print(f"      {label}: {item}")


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--days", type=int, default=1)
    parser.add_argument("--cash", type=float, default=670.0)
    parser.add_argument("--real-data", action="store_true",
                        help="use Alpaca market data (needs APCA_API_KEY_ID / "
                             "APCA_API_SECRET_KEY; run during US market hours, "
                             "quotes are unavailable when the market is closed). "
                             "Orders still go to the PaperBroker — nothing is sent "
                             "to a real broker.")
    args = parser.parse_args()
    provider = None
    if args.real_data:
        from services.market_data.alpaca_provider import AlpacaMarketDataProvider
        from services.market_data.service import MarketDataUnavailable
        try:
            provider = AlpacaMarketDataProvider.from_env()
        except MarketDataUnavailable as e:
            sys.exit(f"--real-data: {e} (set APCA_API_KEY_ID and APCA_API_SECRET_KEY)")
        args.days = 1      # real data has no replay; one live session

    universe = UniverseManager()
    for s in SYMBOLS:
        universe.add(UniverseSymbol(symbol=s, listed_from=date(2015, 1, 1)))
    clock = FrozenClock(current=(datetime.now(timezone.utc) if provider is not None
                                 else datetime.combine(date(2026, 8, 10), time(15, 0),
                                                       tzinfo=timezone.utc)))

    usage = None
    cost_sink: list[CostEntry] = []
    if credentials_available():
        from services.decision.claude_adapters import build_llm_stack

        cfg = DEFAULT_MODEL_CONFIG
        usage = LLMUsageLedger(on_cost=lambda at, agent, usd: cost_sink.append(
            CostEntry(at=at, category=CostCategory.AI, amount=usd, note=agent)))
        decision, skeptic, auditor = build_llm_stack(config=cfg, usage=usage)
        pipeline = build_pipeline(clock, universe, initial_cash=args.cash,
                                  kill_switch=KillSwitch.from_env(),
                                  decision_model=decision, skeptic_model=skeptic,
                                  auditor=auditor, provider=provider)
        # decision.name reflects whichever provider build_llm_stack actually
        # resolved (OpenAI if configured, Claude fallback otherwise) — never
        # trust cfg.decision.agent_id here, it only names the fallback (§25)
        print(f"=== LIVE LLM APIs ===\n"
              f"  Decision AI : {decision.name}\n"
              f"  Skeptic AI  : {cfg.skeptic.agent_id}\n"
              f"  Audit AI    : {cfg.audit.agent_id}  (separate agent, same model)")
    else:
        pipeline = build_pipeline(clock, universe, initial_cash=args.cash, provider=provider,
                                  kill_switch=KillSwitch.from_env())
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
        _print_decision_rationale(pipeline.provenance.all())
        pipeline.execution.broker.settle()
        from datetime import timedelta
        clock.current = clock.current + timedelta(days=1)

    prices = pipeline.mark_prices({}, clock.now())   # quote → last close → cost
    snap = pipeline.ledger.snapshot(prices)
    recon = ReconciliationEngine(broker=pipeline.execution.broker, ledger=pipeline.ledger,
                                 risk_controller=pipeline.risk_controller, cash_tolerance=1.0)
    report = recon.reconcile()
    print(f"\nequity ${snap.equity:,.2f} | reconciliation: "
          f"{'CONSISTENT' if report.consistent else 'MISMATCH'}")
    print(f"audit completeness: {pipeline.provenance.audit_completeness():.0%}")
    if usage is not None:
        print("\nLLM usage per AI (cost needs QUANT_LLM_PRICES):")
        print(f"  spending cap ${usage.budget_usd:.2f} (QUANT_LLM_BUDGET_USD), "
              f"spent {usage.total_cost_usd() or 0.0:.4f} USD")
        for r in usage.snapshot():
            cost = "n/a (no price set)" if r.cost_usd is None else f"${r.cost_usd:.4f}"
            print(f"  {r.agent_id}: calls {r.calls} fail {r.failures} "
                  f"in {r.input_tokens} out {r.output_tokens} cost {cost}")
        if pipeline.cost_engine is not None:
            for e in cost_sink:
                pipeline.cost_engine.record(e)


if __name__ == "__main__":
    main()
