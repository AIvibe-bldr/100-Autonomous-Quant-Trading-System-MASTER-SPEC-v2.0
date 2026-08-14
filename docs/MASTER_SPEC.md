# MASTER SPEC v2.0 — Requirement Map

ソース: `100× Autonomous Quant Trading System MASTER SPEC v2.0` (PDF, 111 sections)

## Mission
初期資金 ¥100,000 で米国株現物の自動売買を行う。Stretch Goal は 1年で ¥10,000,000（100×）だが、
100倍を保証するシステムではなく、本質は **Edge の発見・検証・安全な執行・追跡・自己改善が可能な
Quant Research & Trading Platform** の構築である。

## Optimization Objective (§1)
Return 単独最大化は禁止。評価軸: Expected Return / Expected Log Growth / P(100×) /
Drawdown / Ruin probability / OOS robustness / Transaction cost / Liquidity / Operating cost。
100×のために安全制約を解除してはならない。

## Requirement Map（仕様セクション → 実装モジュール）

| 仕様§ | 要件 | 実装場所 | Phase |
|---|---|---|---|
| 2 | Immutable Safety Rules（レバレッジ/信用/空売り/Martingale等の禁止） | `packages/common/safety.py` + invariantテスト | 0 |
| 3-5 | Capital定義 / Challenge終了条件 / High-Water Mark | `packages/common/ledger.py` | 1 |
| 6-7 | Core Architecture / AIのBroker直接アクセス禁止 | パイプライン全体構成 | 0 |
| 8 | Market Data Service | `services/market-data` | 1 |
| 9 | Unified Clock（4 timestamps, UTC） | `packages/common/clock.py` | 1 |
| 10 | Point-in-Time Data | `packages/common/pit_store.py` | 1 |
| 11 | Data Integrity Engine | `services/data-validation` | 1 |
| 12 | Universe Manager（Survivorship Bias防止） | `services/market-data/universe.py` | 1 |
| 13 | Trading Calendar（US Regular、ハードコード禁止） | `packages/common/calendar.py` | 1 |
| 14 | Settlement Manager（Cash Account） | `packages/common/settlement.py` | 1 |
| 15 | Corporate Action Engine | `services/market-data/corporate_actions.py` | 5 |
| 16 | Event Calendar | `services/market-data/event_calendar.py` | 6 |
| 17-19 | News Engine / Source Hierarchy / Prompt-Injection防御 | `services/news` | 6 |
| 20 | Institutional Flow Engine | `services/institutional` | 6 |
| 21 | Quant Scanner（LLM前段のPythonスキャン） | `services/quant` | 5 |
| 22 | Feature Store（value/timestamp/source/versions） | `services/feature-manager/store.py` | 7 |
| 23-24 | Alpha Factory / Anti-Overfitting | `services/alpha-factory` | 7 |
| 25 | Backtest Execution Simulator | `services/quant/backtest.py` | 5 |
| 26 | Market Regime Engine | `services/regime` | 7 |
| 27-28 | Decision AI（Broker権限なし、JSON Schema固定出力） | `services/decision` | 8 |
| 29-31 | Skeptic AI / Disagreement / Confidence Calibration | `services/decision` | 8 |
| 32 | Forecast Tracker | `services/pdca/forecast_tracker.py` | 10 |
| 33-34 | Loss Control Engine / Stop Types（Entryより先に設計） | `services/loss-control` | 3 |
| 35 | Position Sizing Engine（AIが自由に決めない） | `services/position-sizing` | 3 |
| 36-37 | Capital Allocation（Total Exposure<=100%）/ No Martingale | `services/capital-allocation` | 3 |
| 38 | Correlation / Factor Risk | `services/risk/correlation.py` | 3 |
| 39 | Drawdown Throttling（AI変更不可の閾値） | `services/risk/throttle.py` | 3 |
| 40 | Gap / Overnight Risk | `services/risk/gap_risk.py` | 3 |
| 41 | Liquidity / Capacity Engine | `services/risk/liquidity.py` | 3 |
| 42-43 | Master Risk Controller（deterministic・AI禁止）/ MASTER STOP semantics | `services/risk` | 3 |
| 44-47 | Execution Engine（deterministic）/ Order State Machine / Idempotency / Stale Order | `services/execution` | 2,4 |
| 48-49 | Broker Reconciliation / Single Source of Truth | `services/reconciliation` | 2 |
| 50-53 | Post-Stop / Post-Profit / Exit Optimizer / Counterfactual | `services/pdca` | 10 |
| 54-55 | P&L Attribution / Profit Quality Score | `services/pdca/attribution.py` | 10 |
| 56-59 | Shadow Portfolio / Ablation / Champion-Challenger / Feature Lifecycle | `services/feature-manager` | 11 |
| 60-61 | Builder/Judge/Pruner/Revival/Manager / Drift監視 | `services/alpha-factory`, `services/feature-manager` | 7,11 |
| 62 | Replay Engine | `tests/replay` + `services/quant/replay.py` | 5 |
| 63-65 | Daily/Weekly/Monthly PDCA | `services/pdca` | 10 |
| 66-69 | System Supervisor / Heartbeat / Recovery | `services/supervisor` | 9 |
| 70-71 | Red Team / Chaos / Incident Postmortem | `tests/chaos`, `services/supervisor` | 9 |
| 72-74 | Security Boundary / Paper-Live分離 / AI Output Security | `packages/common/security.py` | 0,9 |
| 75 | Decision Provenance（完全再現） | `packages/common/provenance.py` | 4 |
| 76-79 | Human Intent Analysis / Manual UI / Override / Human vs AI | `apps/api` | 13 |
| 80-83 | Operating Cost / Two P&L / Data ROI / Budget | `services/cost-manager` | 12 |
| 84 | Capital Scaling | `services/risk/liquidity.py` | 12 |
| 85 | Accounting Ledger | `packages/common/ledger.py` | 1 |
| 86-97 | UI（Simple Mode / Chart / Holdings / Theme / News Top3 / Feature Center / Health） | `apps/web`, `apps/api` | 13 |
| 98-99 | Experiment Registry / Version Everything | `packages/common/versioning.py` | 0 |
| 100-101 | Technology / Repository構成 | 本リポジトリ | 0 |
| 102 | Executable Invariants | `tests/unit/test_invariants.py` | 0 |
| 103 | Development Phases | `docs/phases.md` | 0 |
| 104-106 | Live Capital Ramp / Paper→Live Gate / Live Deployment Rule | `docs/paper_live_gate.md` | 14,15 |
| 107 | V1 Success Definition | E2E Paperフロー完走＋監査 | 4 |
| 108-110 | Claude役割 / First Task / Conflict指摘 | `docs/` 一式 | 0 |

## V1 スコープ（本実装）
仕様 §103 Phase 0〜4 ＋ Phase 5 の一部（Quant Scanner）を実装:

1. **Phase 0**: アーキテクチャ / スキーマ / 不変条件 / セキュリティ境界 / リポジトリ
2. **Phase 1**: Clock / Calendar / Ledger / Settlement / Market Data / Validation / Universe
3. **Phase 2**: Broker Adapter / Paper Account / Order State Machine / Reconciliation
4. **Phase 3**: Loss Control / Position Sizing / Capital Allocation / Master Risk
5. **Phase 4**: Paper Execution end-to-end
6. **Phase 5(一部)**: Quant Scanner（Python前段）

Decision AI / News / Institutional 等の LLM 依存部分は **インターフェースのみ** 定義し、
決定論的な Mock 実装を同梱する（§27 の Decision AI は外部モデルを想定するため）。

## 仕様上の指摘事項（§110 に基づく ISSUE 提示）

### ISSUE-1: 通貨単位の混在
- **ISSUE**: 資本は円建て（¥100,000）だが対象は米国株（USD建て）。仕様は FX 換算・FX Settlement に言及するが基準通貨を明示しない。
- **WHY**: P&L・Exposure・Risk Budget 計算に基準通貨が必須。
- **RISK**: 通貨換算の曖昧さによる Risk 計算誤り。
- **PROPOSED FIX**: V1 は内部会計を USD で行い、初期資金は開始時レートで USD 換算して固定記録。表示層で JPY 換算。
- **AFFECTED**: Ledger / Settlement / UI。

### ISSUE-2: Cash Account の T+1 決済と回転売買
- **ISSUE**: Cash Account 前提（§14）で未決済資金の再利用（Good Faith Violation）の扱いが未定義。
- **WHY**: 米国 Cash Account では未決済資金での売買にペナルティがある。
- **RISK**: Broker 制裁 / 口座凍結。
- **PROPOSED FIX**: Settlement Manager が Settled Cash のみを Buying Power として Master Risk Controller に渡す（実装済み、安全側）。
- **AFFECTED**: Settlement / Risk / Execution。

### ISSUE-3: 100×目標と Leverage 1.0 制約
- **ISSUE**: 現物・レバ無しで年間100×は極めて低確率。仕様自身が「保証しない」と明記しているため矛盾ではないが、目標未達を理由とする Risk 増加の誘因になり得る。
- **WHY**: §63/§65 が明示的に「未達だから Risk を上げる」を禁止している。
- **RISK**: 目標progress表示が Risk-taking バイアスを生む。
- **PROPOSED FIX**: 進捗表示は参考値とし、Risk 計算系から完全に分離（実装済み: PDCA は Risk 入力を持たない）。
- **AFFECTED**: PDCA / UI / Risk。
