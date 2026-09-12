# Capital Cell Architecture — Critical Corrections v2

> **ステータス: §41 優先度1〜14 実装済み（優先度15 UIのみ未着手）。** 本ドキュメントが指す
> 「既存のCapital Cell / Portfolio Sleeve実装指示」は本リポジトリの他ドキュメント
> （`MASTER_SPEC.md` / `architecture.md` / `invariants.md` 等）には存在しない。したがって
> 本文中の「既存仕様を置き換える／優先する」はこのリポジトリの現行実装・仕様には適用されない。
>
> 2026-09、§41の優先度1〜14を `packages/schemas/capital_cell.py` と `services/capital_cells/`
> に実装し、9本のテストファイル・176件のテストで検証済み（内訳は末尾「実装状況」参照）。
> 新規ガードは原則すべて変異テスト（該当行をわざと壊してテストが落ちるか）で実効性を確認した。
> **`services/pipeline.py`（既存の単一Master Portfolioパイプライン）・`apps/web/dashboard.html`
> へはまだ配線していない** — スタンドアロンかつ完全にテストされたモジュール群として独立に
> 存在する。優先度15（UI）は、表示すべき実配線が存在しないため未着手。
>
> 現行アーキテクチャとの整合性メモは末尾の「整合性メモ（2026-09 レビュー）」を参照。
> 実装済みモジュールの一覧は末尾の「実装状況（2026-09）」を参照。

既存のCapital Cell / Portfolio Sleeve実装指示について再検証した結果、以下の修正を必須とする。

この修正は既存仕様を置き換える、または優先する。

---

# 1. Capital Cellの100倍目標を禁止

100× ChallengeはMaster PortfolioのReporting Goalとする。

各Capital Cellに、

「1年で100倍を達成しなければならない」

というGoalを持たせてはならない。

Cellは以下で評価する：

- Net Expected Edge
- Risk-adjusted Return
- Drawdown
- Capacity
- Correlation
- Tail Risk
- Decision Quality
- Stop Quality
- Execution Quality
- Alpha Stability

100倍目標への距離を、

Cell Position Size  
Risk Budget  
Trade Frequency  
Alpha Selection

へ入力してはならない。

Master Portfolio：

100× Challengeを管理。

Capital Cell：

Edgeを提供する運用Unit。

この責任を分離する。

---

# 2. CellのSELL定義

各CellもShort禁止。

Cell SELLは、

**そのCellが保有しているVirtual Long Positionの縮小または決済のみ**

とする。

必須Invariant：

cell_sell_qty <= cell_current_long_qty

cell_position_qty >= 0

Master PortfolioだけでなくCell単位でもShortを禁止する。

---

# 3. Capital CellはVirtual Sub-Portfolio

Capital Cellは原則としてBroker Accountではない。

1つのMaster Broker Account内の、

Virtual Portfolio / Accounting Unit

として扱う。

Broker Position：

実際の法的・経済的Position。

Cell Position：

内部Attribution用Virtual Position。

両者を混同しない。

---

# 4. Master Reconciliation Invariant

約定反映完了後、原則として：

SUM(cell_virtual_position[symbol])
=
master_broker_position[symbol]

を保証する。

また：

SUM(cell_virtual_cash)
+ unallocated_master_cash
+ adjustments
=
master_cash

となるようLedgerを設計する。

Corporate Action、Fees、FX、Dividend等のAdjustmentは明示的に管理する。

Reconciliation Error時：

HALT_NEW_ENTRIES

とする。

---

# 5. Cell発注フロー修正

正式Pipelineを以下へ統一する。

Market / News / Quant  
↓  
Cell Opportunity  
↓  
Decision AI  
↓  
Skeptic AI  
↓  
Cell Loss Plan  
↓  
Cell Position Sizing  
↓  
Cell Capital Budget Check  
↓  
Cell Order Intent  
↓  
Master Internal Netting  
↓  
Master Order Construction  
↓  
Pre-Trade Audit AI  
↓  
Master Risk Controller  
↓  
Immutable Approved Order Snapshot  
↓  
Order Hash  
↓  
Execution Engine  
↓  
Broker  
↓  
Fill Allocation  
↓  
Cell Attribution  
↓  
Reconciliation

Internal NettingをExecution直前に曖昧に配置しない。

必ずMaster Order Constructionより前に行う。

---

# 6. Netting後もGross Intentを消さない

Master Risk Controllerには、

net_broker_order

だけでなく、

gross_cell_intents

も渡す。

例：

Cell A BUY 100  
Cell B SELL 80

Broker Net Order：

BUY 20

だからといって、

「Riskは20株分しか存在しなかった」

と評価してはいけない。

以下を保存：

gross_buy_flow  
gross_sell_flow  
internal_cross_volume  
net_broker_flow

これによりCell活動量・Strategy衝突・Turnoverを正しく評価する。

---

# 7. Internal Crossing

例：

Cell A BUY 100  
Cell B SELL 60

Cell Bが実際に60株以上Virtual Longを保有している場合のみ、

60株をInternal Cross可能とする。

残り：

Broker BUY 40

とする。

Internal CrossはBrokerで実際に売買された取引ではない。

したがって、

Master Accounting上のRealized P&L  
Broker Fee  
Tax Event  
Settlement Event

を架空生成してはならない。

---

# 8. Virtual AttributionとActual Accountingを分離

Internal Crossについて、

Cell Performance評価用のVirtual Transfer Price

を記録してよい。

ただし：

Actual Master P&L

と

Virtual Cell Attribution P&L

を完全分離する。

最低限：

actual_master_pnl

virtual_cell_pnl

を区別する。

Cellの仮想取引結果をBrokerの実損益として計上してはならない。

---

# 9. Internal Transfer Price

Internal Cross価格をAIに決めさせない。

決定ルールを固定。

候補：

- Arrival Mid
- Validated Market Mid
- Master Execution Benchmark

など。

利用するBenchmarkはVersion管理する。

恣意的なTransfer PriceによってCell Performanceを良く見せない。

---

# 10. Actual CostとStandalone Cell Costを分離

Internal Nettingによって実際のBroker Tradeが減る場合、

Master側の実際のCostは減少する。

しかし、

「そのCellを単独運用した場合のExecution Cost」

も研究上重要。

したがって：

actual_master_execution_cost

と

simulated_standalone_cell_cost

を分離可能にする。

Performance評価時に混同しない。

---

# 11. Fill Allocation Engine

Internal Netting後、Brokerへ送った残余OrderがPartial Fillした場合、

どのCellへ何株割り当てるかをAIに決めさせない。

Deterministic Fill Allocation Ruleを作る。

例：

Priority  
Pro-rata  
Timestamp priority

など。

RuleはVersion管理。

Fill後：

Broker Fill  
↓  
Fill Allocator  
↓  
Cell Virtual Position Update

とする。

---

# 12. Capital Reservation Ledger

重大。

複数Cellが同時に注文すると、

各Cellが同じMaster Cashを利用可能と誤認する可能性がある。

これを禁止する。

追加：

Capital Reservation Ledger

最低限：

available_cash  
reserved_cash  
reserved_exposure  
pending_orders  
cell_reserved_capital

を管理。

Order Intent生成時またはMaster Risk審査時に、

必要CapitalをAtomicにReservationする。

二重使用を禁止。

---

# 13. Concurrency Safety

同時に多数CellがSignalを出すことを前提とする。

必須：

- Atomic reservation
- Transaction isolation
- Idempotency
- Unique order intent IDs
- Optimistic/Pessimistic locking where required

Race ConditionでExposure Limitを突破できないこと。

---

# 14. Same-Symbol Multi-Cell Stop Management

複数Cellが同じ銘柄を異なる理由で保有できる。

例：

Cell A  
NVDA Momentum  
Stop $170

Cell B  
NVDA News Alpha  
Stop $160

Master Broker Positionはまとめられている。

したがって、

StopをCell単位で追跡する。

Stop Trigger  
↓  
Cell Exit Intent  
↓  
Master Netting  
↓  
Master Risk  
↓  
Execution

とする。

Cell Stopが発動したからといって、

他CellのVirtual Positionを勝手に決済してはならない。

---

# 15. Stop Scope

Stop / InvalidationにはScopeを持たせる。

CELL

そのStrategyだけ無効。

SYMBOL

その銘柄全体について新規Entry禁止またはExit検討。

PORTFOLIO

Portfolio全体Risk Event。

例：

Momentum崩壊  
→ CELL

重大粉飾発覚  
→ SYMBOL

Market-wide emergency  
→ PORTFOLIO

とする。

---

# 16. Protective Exit Oversell防止

複数StopやManual SELLが同時発動しても、

Master Broker Positionを超えるSELLを生成してはならない。

必須：

sell_reserved_quantity

を管理。

Invariant：

open_sell_qty
+
reserved_sell_qty
<=
master_long_position_qty

Short発生を構造的に防止。

---

# 17. Opportunity Breadthは推定値

Effective Opportunity Countを絶対的真実として扱わない。

CorrelationやClusterから計算した：

ESTIMATE

として扱う。

最低限：

estimate  
confidence  
sample_size  
lookback_window  
method_version

を保存。

サンプル不足時：

INSUFFICIENT_DATA

を返す。

---

# 18. Effective Independent Cellsも推定値

Active Cells = 30

Effective Independent Cells = 11.8

の11.8は推定値である。

利用MethodをVersion管理する。

単一の計算法に永久固定しない。

候補：

- Eigenvalue based effective rank
- Correlation cluster count
- Risk-factor clustering

複数指標を比較可能にする。

---

# 19. Alpha Independenceも推定

「Alphaが別名だから独立」

は禁止。

評価：

- Feature Overlap
- Signal Correlation
- Return Correlation
- Universe Overlap
- Timing Overlap
- Factor Exposure
- Training Data Overlap

を使用。

Effective Independent Alpha CountにもConfidenceを持たせる。

---

# 20. Tail Risk Metricの重複整理

Expected ShortfallとCVaRを別々の独立指標として二重評価しない。

基本的には同系統のTail Risk指標として整理する。

Portfolio Tail Risk Dashboardでは、

主要指標を明確に定義する。

---

# 21. Correlationは時間変動する

Correlation Matrixを固定値として扱わない。

最低限：

normal_correlation  
downside_correlation  
stress_correlation

を分離。

さらに：

lookback  
decay weighting  
sample count

を保存。

古いCorrelationに基づき大規模資本配分を行わない。

---

# 22. Risk ContributionはPoint Estimateだけにしない

Risk ContributionにもModel Errorがある。

必要に応じ：

estimated_risk_contribution  
confidence_range

を持つ。

推定精度が低い場合は、

安全側のAllocation上限を使用する。

---

# 23. Capacity推定の不確実性

Estimated Capacityは確定値ではない。

最低限：

capacity_low  
capacity_base  
capacity_high  
capacity_confidence

を持たせる。

例えば：

Estimated Capacity

Low ¥5M  
Base ¥8M  
High ¥12M

とする。

Capital AllocationではBaseだけでなく不確実性を考慮する。

---

# 24. Marginal AlphaもRangeで扱う

「追加100万円で+5.4%」

のような過度に精密な推定を信用しない。

最低限：

marginal_edge_estimate  
confidence_interval  
sample_size

を持たせる。

サンプル不足：

INSUFFICIENT_DATA。

---

# 25. Scale Simulationに外挿警告

10万円運用データから、

10億円運用を高精度に予測できるとは考えない。

Scale Simulationには：

observed_range  
extrapolated_range  
confidence

を持たせる。

観測範囲を大きく超える場合：

LOW_CONFIDENCE_EXTRAPOLATION

と表示する。

この結果だけでCapital Allocationを自動変更しない。

---

# 26. Cost Allocationを修正

重大。

Data FeedやServerなどの固定費を、

各Cellへ適当に均等配賦すると、

Cellの経済価値を誤判定する可能性がある。

Costを分類：

## Variable / Avoidable Cost

- Commission
- Spread
- Slippage
- Market Impact
- Cell固有API
- Cell固有Data

## Shared Fixed Cost

- Base server
- Shared DB
- Shared News subscription
- Shared AI infrastructure

Capital Allocation判断では主に、

**Marginal / Avoidable Cost**

を使用。

Project Net P&Lでは、

Shared Fixed Costもすべて控除。

Cell ROIとProject ROIを混同しない。

---

# 27. Data ROIもMarginal Value中心

Premium News Feedが、

100Cellすべてで使われる場合、

各Cellへ単純に1/100配賦するだけでKEEP/CANCELを決めない。

評価対象：

Service ON vs OFFで、

Master Portfolio全体のIncremental Valueがどれだけ変化したか。

---

# 28. Allocation GovernorはUncertainty-adjusted Edgeを利用

単純：

Highest Expected Return

では資金配分しない。

可能な限り、

Uncertainty-adjusted Net Edge

を評価。

考慮：

- Confidence interval
- Sample size
- Regime stability
- Capacity
- Tail Risk
- Correlation
- Drawdown
- Cost

直近の勝率だけで増資禁止。

---

# 29. Allocation Reserve

Portfolio Capitalの100%をCell Budgetへ必ず割り当てる必要はない。

以下を認める：

Unallocated Capital  
Cash Reserve  
Risk Reserve

Opportunity不足時はCash保持。

---

# 30. Cell Creation Governor

AIが無制限に新Cellを作成しない。

新Cell作成には最低限：

- Distinct Alpha hypothesis
- Opportunity availability
- Independence evidence
- Expected incremental value
- Operational capacity

を要求。

Cell数最大化を目標にしない。

---

# 31. Common-Mode Failure修正

Dependency Concentrationを検出しても、

Brokerを自動で別Brokerへ切り替えてLIVE注文してはいけない。

Market Data Feed Failover：

事前検証済みなら自動可能。

Execution Broker Failover：

原則Human Approval + Reconciliation + Pre-tested Procedure必須。

Broker変更は重大Operation。

---

# 32. FX Attribution

US Stockを扱うため、

Capital Cellに必要に応じ：

base_currency  
trading_currency  
fx_pnl  
fx_exposure

を持たせる。

Cell Alpha P&LとFX P&Lを混同しない。

Master PortfolioのFX Engineと連携。

---

# 33. Corporate Action Cell Allocation

Master Broker Positionに、

Dividend  
Split  
Merger  
Symbol Change

等が発生した場合、

Cell Virtual Positionへ正確に配分する。

Corporate Action Allocatorを用意。

Cell合計とMaster Positionの整合性を維持する。

---

# 34. Master vs Cell Performance

Dashboardでは：

Master Actual Performance

と

Cell Attribution Performance

を区別する。

Cell Performanceの単純合計とMaster Actual P&Lが、

内部Netting・Shared Cost・FX等によって完全一致しない場合、

Reconciliation Bridge

で差分理由を説明可能にする。

---

# 35. Decision QualityもCell単位＋Master単位

各Cell：

Decision Quality

Master：

Portfolio Decision Quality

を持つ。

ただし同じMarket Eventに対する10個の類似Decisionを、

「独立した10判断」

として統計的Sample Sizeを水増ししない。

Cluster-adjusted sample sizeを検討する。

---

# 36. Opportunityの重複によるSample Inflation防止

同じNVDAニュースに対して、

10 Cellが類似BUY判断

を行った場合、

独立した10個の成功例として学習しない。

同一Opportunity Clusterとして紐付ける。

---

# 37. Learning Data Independence

Alpha Factory / Judgeが、

Cell数を増やしたことで見かけ上Sample Sizeが増えた

と誤認しない。

Underlying Market Event単位、

Opportunity Cluster単位、

Time Cluster単位

でもSample Independenceを評価。

---

# 38. Master RiskはNetとGross両方を見る

Master Risk Controllerは：

Net Exposure

だけではなく、

Gross Cell Intent  
Strategy Concentration  
Alpha Concentration  
Theme Concentration  
Tail Risk

も確認する。

Nettingによって隠れたStrategy Riskを無視しない。

---

# 39. Key Invariants

最低限以下をAutomated Test化する。

cell_position >= 0

cell_sell_qty <= cell_position

sum(cell_positions_by_symbol) == broker_position_after_reconciliation

sum(cell_cash) + master_unallocated_cash + adjustments == master_cash

no_cell_can_spend_reserved_capital_of_another_cell

total_open_sell_quantity <= available_long_position

approved_order_hash == submitted_order_hash

net_broker_order == deterministic_net(gross_cell_intents)

internal_cross_does_not_create_fake_master_trade

internal_cross_does_not_create_fake_tax_event

audit_failure_cannot_bypass_master_risk

target_progress_does_not_change_cell_risk_budget

---

# 40. Revised Final Architecture

MASTER PORTFOLIO

↓  

Opportunity Breadth  
Edge Lineage  
Regime  
Capacity  
Tail Risk

↓  

Capital Allocation Governor

↓  

CAPITAL CELLS

Cell A  
Cell B  
Cell C  
...

↓  

Cell Decision  
Skeptic  
Loss Plan  
Position Sizing  
Capital Reservation

↓  

CELL ORDER INTENTS

↓  

Internal Netting / Collision Engine

↓  

MASTER ORDER INTENT

↓  

Pre-Trade Audit AI

↓  

MASTER RISK CONTROLLER

↓  

Immutable Order Snapshot + Hash

↓  

Execution Engine

↓  

Broker

↓  

Fill Allocation

↓  

Cell Attribution

↓  

Master Reconciliation

↓  

Decision Quality / Stop Quality / Execution Quality / Learning

---

# 41. Implementation Priority Correction

今すぐ必要：

1. Cell Schema
2. Virtual Position / Cash Ledger
3. Master Reconciliation Invariants
4. Capital Reservation
5. Cell SELL / No Short
6. Netting
7. Fill Allocation
8. Same-Symbol Stop Management
9. Correlation / Edge Lineage
10. Opportunity Breadth
11. Capacity / Tail Risk
12. Allocation Governor
13. Scale Simulation
14. Common-Mode Dependency
15. UI

Scale Simulation等より先に、

**Ledger / Reservation / Netting / Reconciliation**

を完成させる。

資金管理が壊れている状態で高度なAI最適化を作らない。

---

# 42. Final Principle

Capital Cell方式の目的は、

「資産を100万円ずつに分けること」

ではない。

目的は、

**Master Portfolioの安全境界の中で、多数の独立Edgeを並列運用すること。**

資本が増えても、

独立Edgeが不足している場合：

Cashを保持。

Capacityが不足している場合：

追加Allocationしない。

Correlationが高い場合：

Cell数を増やしても分散と評価しない。

推定精度が不足している場合：

INSUFFICIENT_DATA。

最終原則：

**Scale by verified independent edge, not by nominal cell count.**

そして、

**Cellは100倍を追わない。  
Master Portfolioが100倍Challengeを管理する。**

---

# 整合性メモ（2026-09 レビュー）

取り込み時点の現行実装（`docs/MASTER_SPEC.md`, `docs/architecture.md`,
`docs/invariants.md`）との整合性を確認した結果。矛盾は見つかっていない —
本仕様は現行パイプラインの**手前にCellレイヤーを、後段にNetting/Attribution/
Reconciliationレイヤーを追加する拡張**であり、既存の安全原則（§2, INV-1〜21）と
矛盾する記述はない。ただし、現行実装には存在しない前提がいくつかあるため記録する。

## 整合している点

- **AIはBrokerに到達不能（§7, INV-6）**: §5/§40のCellパイプラインも
  「Decision AI → Skeptic AI → (Cell側処理) → Master Risk Controller（deterministic）
  → Execution Engine」の構造を保っており、Cell層はDecision/Skeptic/Sizingが
  提案を出すだけの既存原則をそのまま踏襲している。
- **Hash固定Approved Order Snapshot（ADDENDUM A4, INV-17/18）**: §5/§40で
  Internal NettingをMaster Order Constructionより前、Pre-Trade Audit AIより前に
  置いているため、Audit/Riskが見るのは既にNetting後の単一注文であり、
  既存のhash lock機構をそのまま適用できる。
- **Reconciliation失敗→HALT_NEW_ENTRIES（§4）**: 既存の
  `test_broker_position_mismatch_halts_new_entries`（§28テスト#16）と
  同じ振る舞いを、Cell合計とMaster Broker Positionの整合性チェックへ
  拡張しているだけで、新しい概念ではない。
- **Position Sizing/Capital Allocation/ExecutionはAI禁止（architecture.md
  Service Boundary表）**: §9/§11がInternal Transfer PriceとFill Allocationを
  「AIに決めさせない、Deterministic Rule」としているのは、既存の
  非AI決定論的執行という原則と一致する。
- **target_progress_does_not_change_cell_risk_budget（§1, §39）**: 既存の
  `docs/MASTER_SPEC.md` ISSUE-3（100×進捗をRisk計算系から分離済み）と
  同じ原則をCell単位に拡張したもので、方向性は完全に一致している。

## 現行実装に存在せず、新規に必要になる前提

- **Master Risk ControllerへのGross Cell Intent入力（§6, §38）**: 現行の
  `services/risk` はNetした単一注文のみを見る設計。`services/capital_cells/netting.py`
  の `NettingResult` はgross flow / internal cross volumeを保持しているが、
  それを`MasterRiskController`へ実際に渡す配線はまだない。
- **FX Engine（§32）**: `docs/MASTER_SPEC.md` ISSUE-1により、V1は内部会計を
  USDのみで行い、JPY換算は表示層限定としている（Master側にFX P&LやFX
  Exposureを持つ実装は現状ない）。§32が前提とする「Master PortfolioのFX
  Engine」は本仕様と合わせて新規に設計する必要がある。
- **Decision Quality / Sample Independenceのcluster補正（§35-37）**: 既存の
  `services/pdca/decision_quality.py`（ADDENDUM A1-A2）はCellやOpportunity
  Clusterの概念を持たない。cell_id / opportunity_cluster_idの付与とサンプル
  補正ロジックの追加が必要。
- **Corporate Action Cell Allocation（§33）**: 既存の
  `services/market_data/corporate_actions.py` はMaster単一口座向けで、
  Cell配分ロジックは未実装。
- **§41優先度15（UI）**: 未着手。優先度1〜14はいずれも`services/pipeline.py` /
  `apps/web/dashboard.html`へ配線されていないため、表示すべき実データの経路が
  まだ存在しない。

## 実装状況（2026-09）

§41優先度1〜14を、既存パイプライン（`services/pipeline.py`）には配線しない
スタンドアロンモジュール群として実装済み。

| 優先度 | 内容 | 実装場所 | テスト |
|---|---|---|---|
| 1 | Cell Schema | `packages/schemas/capital_cell.py`（`CapitalCell`, `CellOrderIntent`, `CellStopPlan`） | `TestCellSchema` |
| 2 | Virtual Position / Cash Ledger | `services/capital_cells/ledger.py`（`CellLedger`） | `TestCellLedger` |
| 3 | Master Reconciliation Invariant | `services/capital_cells/reconciliation.py`（`CellReconciliationEngine`） | `TestCellReconciliation` |
| 4 | Capital Reservation Ledger | `services/capital_cells/reservation.py`（`CapitalReservationLedger`） | `TestCapitalReservation` |
| 5 | Cell SELL / No Short | `services/capital_cells/ledger.py`（`CellLedger`内の空売り禁止チェック） | `TestCellLedger` |
| 6 | Internal Netting（§5-9含む: gross flow保持・Internal Crossing・Transfer Price・仮想/実会計分離） | `services/capital_cells/netting.py`（`NettingEngine`） | `TestNettingEngine` |
| 7 | Fill Allocation Engine | `services/capital_cells/fill_allocation.py`（`FillAllocationEngine`、pro-rata） | `TestFillAllocationEngine` |
| — | End-to-end（Netting→Broker Fill→Allocation→Reconciliation） | — | `TestCapitalCellEndToEnd` |
| 8 | Same-Symbol Multi-Cell Stop Management（§14-16） | `services/capital_cells/stops.py`（`CellStopRegistry`, `SellReservationLedger`） | `test_capital_cell_stops.py` |
| 9 | Correlation / Edge Lineage（§19, §21） | `services/capital_cells/correlation.py`（`CorrelationEngine`, `effective_independent_alpha_count`） | `test_capital_cell_correlation.py` |
| 10 | Opportunity Breadth / Effective Independent Cells（§17-18） | `services/capital_cells/breadth.py`（eigenvalue / cluster / factor methods） | `test_capital_cell_breadth.py` |
| 11 | Capacity / Tail Risk（§20, §22-23） | `services/capital_cells/capacity_tail_risk.py` | `test_capital_cell_capacity_tail_risk.py` |
| 12 | Allocation Governor（§28-30） | `services/capital_cells/allocation_governor.py` | `test_capital_cell_allocation_governor.py` |
| 13 | Marginal Alpha / Scale Simulation（§24-25） | `services/capital_cells/scale_simulation.py` | `test_capital_cell_scale_simulation.py` |
| 14 | Common-Mode Failure Guard（§31） | `services/capital_cells/common_mode.py`（`CommonModeFailoverGuard`） | `test_capital_cell_common_mode.py` |
| 9 (基盤) | 不確実性つき推定値の共通形（estimate/confidence/sample_size/method_version） | `services/capital_cells/estimation.py`（`PointEstimate`, `RangeEstimate`） | `test_capital_cell_estimation.py` |

優先度9で導入した`PointEstimate`/`RangeEstimate`（confidence・sample_size・
method_versionを持つ）は、優先度10〜13すべてで共通に再利用している —
§17-25全体を貫く「estimate/confidence/sample_size/method_version」という
繰り返しパターンを、モジュールごとに再実装せず1箇所にまとめた。

新規に導入した安全ガード（空売り禁止・全か無か適用・決定性・Oversell防止・
Broker Failover承認等）は、原則すべて変異テスト（該当行を意図的に壊して
テストが落ちることを確認）で実効性を検証した。

実装上の重要な取り決め:

- **Internal Crossは売り手の実保有が前提（§7）**: `NettingEngine.net()` はCell台帳を
  必須引数として受け取り、SELL Intentの合計がそのCellのVirtual Long保有を超える場合は
  プラン生成前に拒否する（分割Intentの合算で判定）。Cell単位のShortを生む
  ネッティング計画自体を作らせない。
- **適用は全か無か**: `apply_crosses_to_cells` / `apply_allocations_to_cells` は
  全legを事前検証してから書き込む。途中で失敗して一部Cellだけ更新されると、
  §4のReconciliation不変条件を「検知するはずの機構自身が破る」ことになるため。
- **Intent IDは一意（§13）**: 重複IDはネッティングの決定性を壊すため拒否する。
- **Fill Allocationの残余はNetting結果が返す**: `SymbolNetResult.residual_intents` に
  「Broker注文が各Cellへ負っている数量」を持たせ、呼び出し側が再計算しない。

`tests/unit/test_capital_cells.py` で45件のテストが通っており（優先度8〜14＋
共通基盤の8ファイルと合わせ、`test_capital_cell*.py`全体で176件、うち§39の
以下の不変条件（本スコープに該当するもの）をカバーするのは主に`test_capital_cells.py`側）:

```
cell_position >= 0
cell_sell_qty <= cell_position
sum(cell_positions_by_symbol) == broker_position_after_reconciliation
sum(cell_cash) + master_unallocated_cash + adjustments == master_cash
no_cell_can_spend_reserved_capital_of_another_cell
net_broker_order == deterministic_net(gross_cell_intents)
internal_cross_does_not_create_fake_master_trade
internal_cross_does_not_create_fake_tax_event
```

## 実装順序について

§41の優先順位（Cell Schema → Virtual Ledger → Reconciliation Invariant →
Capital Reservation → No Short → Netting → Fill Allocation → Same-Symbol
Stop Management → Correlation/Edge Lineage → Opportunity Breadth →
Capacity/Tail Risk → Allocation Governor → Scale Simulation →
Common-Mode Dependency → UI）は、現行コードベースの層構造
（Ledger → Execution → Risk → Reconciliation）とも自然に対応しており、
妥当な順序だった。優先度1〜14はこの順序をそのまま踏襲して実装済み。

残るのは優先度15（UI）のみで、これは他の14項目と質が異なる：UIはパイプラインへ
の実配線があって初めて表示すべきデータを持つため、`services/pipeline.py`（既存の
単一Master Portfolio）をCapital Cell対応に拡張するかどうかの設計判断が先に必要。
それを行わずにUIだけ作ると、表示するものがダミーデータしかない画面になる。