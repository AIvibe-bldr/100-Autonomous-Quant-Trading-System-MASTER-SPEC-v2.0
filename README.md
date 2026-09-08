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
| 5 | Quant Scanner（§21）/ Backtest Microstructure Simulator（§25）/ Replay Engine（§62）/ Anti-Overfitting toolkit（§24: walk-forward, embargo, Monte Carlo, Bonferroni） | `services/quant`, `packages/strategy_sdk` |
| 6 | News Engine（§17-19: dedup/clustering/source hierarchy/SNS単独発注禁止/injection防御）/ Institutional Flow（§20: 単一Feature売買禁止） | `services/news`, `services/institutional` |
| 7 | Feature Store＋Lifecycle＋Drift（§22, §59, §61）/ Regime Engine（§26）/ Alpha Factory＋Judge（§23, §60） | `services/feature_manager`, `services/regime`, `services/alpha_factory` |
| 8 | Decision AI / Skeptic / Calibration / Disagreement Engine（§27-31, §30） | `services/decision` |
| 9 | Recovery Manager（§69: 重大障害は人間承認必須）/ Incident Postmortem（§71）/ Heartbeat（§67） | `services/supervisor` |
| 10 | Post-Stop/Post-Profit Tracker（§50-51）/ P&L Attribution（§54）/ Profit Quality（§55）/ Daily PDCA Review（§63-64） | `services/pdca` |
| 11 | Shadow Portfolios（§56）/ Ablation（§57）/ Champion-Challenger（§58） | `services/pdca/shadow.py`, `services/alpha_factory` |
| 12 | Operating Cost Engine / Two P&L（§80-81）/ Data ROI（§82） | `services/cost_manager` |
| 13 | Status API＋ダッシュボードUI（§86-97、read-only） | `apps/api/main.py`, `apps/web/dashboard.html` |
| — | Corporate Action Engine（§15）/ Event Calendar（§16） | `services/market_data` |
| — | Forecast Tracker（§32）/ Human Intent・Override・Human vs AI（§76-79） | `services/decision` |
| — | Experiment Registry（§98）/ Exit Optimizer（§52: 段階昇格＋人間承認必須） | `packages/common/experiments.py`, `services/pdca/exit_optimizer.py` |
| A1-A2 | **Decision Quality Engine**（全判断のImmutable Snapshot・Outcome/Process分離採点・月次レポート） | `services/pdca/decision_quality.py` |
| A3 | **Independent Audit AI**（発注直前の意味的監査、PASS/REJECT/REVIEW、条件付き強制Audit） | `services/decision/audit.py` |
| A4 | **Immutable Approved Order Snapshot**（SHA-256 hash照合、変更→再Audit必須） | `packages/schemas/audit.py` |
| A5-A6 | **Pre-Trade Audit Log / Near-Miss学習**（防止した誤発注の集計） | `services/pdca/audit_log.py` |
| A7 | 判断品質・発注安全性パネル（`/decision-quality`, `/order-safety`） | `apps/api`, `apps/web` |

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

## ダッシュボード + Status API

```bash
pip install fastapi uvicorn httpx
python3 scripts/run_dashboard.py --days 5   # http://localhost:8000/ にUI表示
```

`apps/web/dashboard.html` はビルド不要の自己完結UI（§86-97: Simple Mode / 資産チャート /
保有銘柄＋テーマタグ / テーマ配分（40%超で集中警告 §90）/ セッションファネルとNO TRADE理由 /
System Health / PAPER・LIVEバッジ / MASTER STOP状態表示）。Next.js/TypeScript版（§100）への
移行はフロントエンド専用フェーズで実施予定。

read-onlyエンドポイント: `/health`（環境バッジ・Risk状態 §73,97）/ `/portfolio`（Simple Mode §86、
Two P&L §81）/ `/chart`（§87）/ `/holdings`（§88-89）/ `/themes`（§90）/ `/session`
（ファネルとNO TRADE理由 §93）/ `/decision-quality`（ADDENDUM A2）/ `/order-safety`（ADDENDUM A7）/
`/final-trade-theses`（Decision×Skeptic統合とdisagreement_score §27）/
`/monitor`（Monitor AI、異常検知時のみconsult §66）/ `/features`（§94）/ `/risk-config`（閲覧のみ §39）。
**書き込み系エンドポイントは存在しない**（テストで強制）。

## 実LLM接続（Decision AI / Skeptic AI / Audit AI）

```bash
pip install anthropic          # または: pip install -e '.[llm]'
export MY_ANTHROPIC_API_KEY=...   # または `ant auth login`
python3 scripts/run_llm_paper_demo.py --days 3
```

認証情報が無い場合は自動的にMockにフォールバックしてデモが動作する（何もしなくても壊れない）。

3つのAIは**コストの高低とファネルの通過数**に応じてモデル階層を分けている（§21, §80）:

| AI | 既定モデル | 理由 |
|---|---|---|
| Decision AI | `claude-sonnet-5` | Quant Scannerで絞った後も1セッション約20候補呼ぶため、コスト効率を優先 |
| Skeptic AI | `claude-opus-5` | BUY候補のみに絞られ呼び出し数が少ない。Decision AIより強いモデルで独立レビュー（A3-5: 異なるModel） |
| Audit AI | `claude-haiku-4-5` | Sizing後の注文のみに絞られ最も呼び出し数が少ない。深い推論ではなく意味的整合の狭いチェックなので高速・低コストなモデルで十分 |

環境変数 `QUANT_DECISION_MODEL` / `QUANT_SKEPTIC_MODEL` / `QUANT_AUDIT_MODEL` で上書き可能（`packages/common/llm_client.py`）。

実装は `services/decision/claude_adapters.py`。`client.messages.parse(output_format=...)`
（Structured Outputs）でpydanticスキーマに強制し、既存のMalformed Output Reject契約
（INV-14, INV-20）をそのまま活かす。ニュース等の外部テキストは `<untrusted_external_data>`
タグで明示的に囲み、指示として解釈しないようシステムプロンプトで明記（§19）。
Skeptic AI / Audit AIが到達不能な場合はfail-safe（Skepticは自動veto、AuditはV1では
`audit_all=True`のため強制Audit扱いとなり発注ブロック — INV-19）。Decision AI/Skeptic AI/
Audit AIのいずれもBroker権限を持たない（`packages/broker_adapters`をimportしていないことを
既存のASTテストで強制）。

## 既知の制限（V1）

- 市場データは決定論的Mock（実データProviderは `MarketDataProvider` 実装で差し替え）
- Decision AI / Skeptic / Audit AI は既定でMock。実LLM接続は上記参照（`DecisionModel` /
  `SkepticModel` / `AuditModel` Protocol 実装で差し替え。出力は同じSchema検証を通る）
- News/Institutional は実フィード未接続（エンジンはテスト済み、入力はスタブ）
- Next.js フロントエンド（§100）は未着手 — Status API がバックエンド
- 通貨は内部USD（`docs/MASTER_SPEC.md` ISSUE-1参照）

## 免責

本システムは研究・教育目的のPaper Trading基盤です。実資金での運用判断は自己責任であり、
利益を保証するものではありません（§0: 100倍を保証するシステムではない）。
