# Architecture

## Core Pipeline (§6, §7)

```
Market / External Data
        ↓
Data Validation (services/data-validation)
        ↓
Point-in-Time Store (packages/common/pit_store)
        ↓
Universe / Calendar (services/market-data, packages/common/calendar)
        ↓
Quant Scanner (services/quant)          … 5000 → 200 → 20 (Python/SQL、LLM前段)
        ↓
News / SEC / IR / Institutional (services/news, services/institutional)
        ↓
Feature Store (services/feature-manager)
        ↓
Alpha / Regime Engine (services/alpha-factory, services/regime)
        ↓
Opportunity Engine
        ↓
Decision AI (services/decision)         … Broker権限なし・JSON Schema固定
        ↓
Skeptic / Counterargument (services/decision/skeptic)
        ↓
Loss & Exit Planning (services/loss-control)   … Entryより先にStop設計
        ↓
Position Sizing (services/position-sizing)     … AIが自由に決めない
        ↓
Capital Allocation (services/capital-allocation) … Total Exposure <= 100%
        ↓
Independent Audit AI (services/decision/audit)  … 意味的整合の監査（ADDENDUM A3）
        ↓                                          Decision≠Order方向等をREJECT
Master Risk Controller (services/risk)  … deterministic・AI禁止・PASSしない注文は送らない
        ↓                                  ＝ 最終防壁（AuditはAIなので最終責任を持たない）
Immutable Approved Order Snapshot        … hash固定（ADDENDUM A4）。Field変更→再Audit+再Risk
        ↓
Execution Engine (services/execution)   … deterministic・LLM禁止・Snapshot hash一致のみ送信
        ↓
Broker (packages/broker-adapters)       … Adapter Interface、V1はPaper
        ↓
Reconciliation (services/reconciliation) … 起動時＋定期＋異常時
        ↓
Post-Trade Tracking / PDCA (services/pdca)
```

## AI Broker アクセス禁止 (§7)

Decision AI は `TradeProposal`（構造化データ）を出力するだけである。
Broker API を呼べるのは Execution Engine のみで、Execution Engine は
`RiskApprovedOrder`（Master Risk Controller の承認印付き）しか受け付けない。

コード上の強制:
- `packages/broker-adapters` は `services/decision` から import されない（invariantテストで検証）
- `ExecutionEngine.submit()` は `RiskApproval` トークンのない注文を拒否する
- Broker credential は Execution Engine のみが保持（§74: AIはBroker API keyを知らない）

## Service Boundary

| サービス | 責務 | 依存 | AI利用 |
|---|---|---|---|
| market-data | OHLCV/Quote取得・Universe・Calendar供給 | broker/data providers | 不可 |
| data-validation | Data Integrity Engine (§11) | market-data | 不可 |
| quant | Scanner / Backtest / Replay | market-data, feature | 不可 |
| news / institutional | Event Driven情報収集 (§17-20) | 外部API | 分析のみ可 |
| feature-manager | Feature Store / Lifecycle (§22,59) | 全上流 | 不可 |
| alpha-factory / regime | Alpha生成・Regime分類 (§23,26) | feature | Builderのみ可 |
| decision | Decision AI / Skeptic / Calibration (§27-31) | 上流全部 | 可（提案のみ） |
| loss-control | Stop Plan設計 (§33-34) | proposal | 不可 |
| position-sizing | サイズ決定 (§35) | stop plan, portfolio | 不可 |
| capital-allocation | 資金配分 (§36) | sized proposals | 不可 |
| risk | Master Risk Controller (§42) | 全状態 | **禁止** |
| execution | 注文執行・Order State Machine (§44-47) | risk-approved orders | **禁止** |
| reconciliation | Broker照合 (§48) | broker, ledger | 不可 |
| supervisor | Heartbeat監視・Recovery (§66-69) | 全サービス | 異常時のみ可 |
| pdca | Post-Trade / Review (§50-55, 63-65) | 全履歴 | 分析のみ可 |
| cost-manager | Operating Cost / Two P&L (§80-83) | ledger | 不可 |

## 環境分離 (§73)

`Environment = RESEARCH | PAPER | LIVE` を全コンポーネントが保持。
- credential・DB namespace・execution endpoint を環境ごとに分離
- `live_environment != paper_environment` を invariant テストで強制
- UI に環境バッジを常時表示

## Two P&L (§81)

- **Trading P&L**: Broker口座内の運用結果（Ledgerから算出）
- **Project Net P&L**: Trading P&L − System Operating Costs（cost-manager が控除表示）
