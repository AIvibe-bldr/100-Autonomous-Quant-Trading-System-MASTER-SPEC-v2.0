"""The Judge is run over history and only ever RECOMMENDS."""
import importlib.util
from datetime import datetime, timezone
from pathlib import Path

from services.alpha_factory.factory import AlphaStage, JudgeRecommendation
from services.alpha_factory.report import judge_alphas
from services.market_data.service import MarketDataService, MockProvider

NOW = datetime(2026, 8, 11, 15, 0, tzinfo=timezone.utc)


def _bars(symbols, days=500):
    md = MarketDataService(MockProvider())
    return {s: [b.bar for b in md.bars(s, NOW, days, NOW)] for s in symbols}


def test_every_alpha_symbol_pair_gets_a_verdict_and_none_is_ever_promoted():
    rows = judge_alphas(_bars(["AAPL", "MSFT", "NVDA"]))
    assert len(rows) == 2 * 3
    assert {r["alpha"] for r in rows} == {"momentum", "mean_reversion"}
    assert all(r["stage"] != AlphaStage.PROMOTED.value for r in rows)             # the Judge cannot promote
    assert all(r["recommendation"] in {x.value for x in JudgeRecommendation} for r in rows)


def test_a_short_history_is_answered_with_research_more_not_a_verdict():
    rows = judge_alphas(_bars(["AAPL"], days=60))
    assert all(r["recommendation"] == "RESEARCH_MORE" and "insufficient sample" in r["reasons"][0]
               for r in rows)


def test_the_script_prints_recommendations_and_says_nothing_is_promoted(capsys):
    path = Path(__file__).resolve().parents[2] / "scripts" / "judge_alphas.py"
    spec = importlib.util.spec_from_file_location("judge_alphas", path)
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    assert mod.main(["AAPL", "--days", "400"]) == 0
    out = capsys.readouterr().out
    assert "momentum" in out and "Recommendations only: nothing is promoted" in out
