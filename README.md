# 100× Autonomous Quant Trading System — V1 Core

MASTER SPEC v2.0 に基づく自動売買プラットフォームの V1 実装（Phase 0〜4 ＋ Quant Scanner）。

**本質**: 100倍を保証するシステムではなく、「Edgeを発見・検証し、安全に執行し、追跡し、
自ら改善できる Quant Research & Trading Platform」の土台（§0）。

## クイックスタート

```bash
pip install pydantic pytest

# 全テスト（安全不変条件 §102 を含む）
python3 -m pytest

# Paper trading デモ（決定論的モック市場で5営業日）
python3 scripts/run_paper_demo.py --days 5
```

## 実装済みスコープ

| Phase | 内容 | 場所 |
|---|---|---|
| 0 | 設計ドキュメント16点 / スキーマ / 不変条件 / 環境分離 | `docs/`, `packages/schemas`, `tests/unit/test_invariants.py` |
| 1 | Unified Clock / Trading Calendar / Ledger / Settlement / Market Data / Data Integrity / Universe / PIT Store | `packages/common`, `services/market_data`, `services/data_validation` |
| 2 | Broker Adapter Interface / Paper Broker / Order State Machine / Reconciliation | `packages/broker_adapters`, `services/execution`, `services/reconciliation` |
| 3 | Loss Control / Position Sizing / Capital Allocation / Master Risk Controller / Gap Risk | `services/loss_control`, `services/position_sizing`, `services/capital_allocation`, `services/risk` |
| 4 | Paper Execution E2E ＋ Decision Provenance | `services/pipeline.py`, `packages/common/provenance.py` |
| 5(一部) | Quant Scanner（LLM前段のPythonファネル §21） | `services/quant` |
| — | Decision AI / Skeptic インターフェース＋決定論的Mock（§27-31） | `services/decision` |
| — | Post-Stop/Post-Profit Tracker・Heartbeat（最小実装 §50-51, §67） | `services/pdca`, `services/supervisor` |

## 安全設計（AIから変更不能 §2）

- **Leverage ≤ 1.0 / 信用・空売り・Martingale禁止** — `RiskConfig` はコンストラクタで違反値を拒否、
  Ledger は margin 負債を型レベルで表現できない
- **AIはBrokerへ到達不能（§7）** — Decision AI は構造化提案を出すだけ。発注には
  Master Risk Controller の HMAC 署名付き承認が必須で、Execution Engine が検証する。
  `services/decision` が broker を import しないことをASTテストで強制
- **Stop設計がEntryに先行（§33）** — Stop Plan の無い提案は Loss Control で棄却
- **決定論的Risk審査（§42）** — 12チェック全PASSのみ執行。Reject理由は全件記録（Counterfactual §53）
- **MASTER STOP（§43）** — 新規Entry停止中も Protective Stop / Risk-reducing SELL は通る
- **Idempotency（§46）** — client_order_id 重複は構造的に不可能。Timeout後は無条件再送せず
  Broker照合（UNKNOWN → Reconciliation → Entry停止）
- **監査（§75）** — 全判断に完全なProvenance。デモ終了時に audit completeness 100% を検証

## テスト

```
tests/unit/test_invariants.py   … §102 実行可能不変条件 INV-1〜15
tests/unit/                     … Clock/Calendar/Ledger/Settlement/PIT/Universe/Integrity
tests/integration/              … Paper E2E（§107 V1成功定義）
tests/chaos/                    … 故障注入: timeout/disconnect/partial fill/duplicate（§70）
```

## ドキュメント（§109 の16成果物）

`docs/MASTER_SPEC.md`（Requirement Map ＋ ISSUE指摘）/ `architecture.md`（構成・Service Boundary・
Data Flow）/ `invariants.md`（Safety Invariants）/ `database.md`（DB Schema・Event Schema）/
`agents.md`（Agent Interfaces）/ `risk.md`（Risk Pipeline）/ `execution.md`（Order State Machine・
Broker Interface）/ `experiments.md`（Testing Strategy・Failure Matrix・Paper→Live Gate・Phase計画）

## 既知の制限（V1）

- 市場データは決定論的Mock（実データProviderは `MarketDataProvider` 実装で差し替え）
- Decision AI / Skeptic はMock（実LLMは `DecisionModel` Protocol 実装で差し替え。
  出力は同じSchema検証を通る）
- News / Institutional / Regime / Alpha Factory / Shadow / UI は未実装（Phase 6以降）
- 通貨は内部USD（`docs/MASTER_SPEC.md` ISSUE-1参照）

## 免責

本システムは研究・教育目的のPaper Trading基盤です。実資金での運用判断は自己責任であり、
利益を保証するものではありません（§0: 100倍を保証するシステムではない）。
