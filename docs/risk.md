# Risk Pipeline

## 処理順序（§7, §33-43）

```
TradeProposal (Decision AI / Human Intent — どちらも同じ入口)
   ↓
LossControlEngine (§33)
   - Entry / Stop / Stop Reason / Horizon / Thesis / Invalidation / Profit taking / Gap risk
   - StopPlanの無い提案はここで死ぬ (INV-4)
   ↓ StopPlannedProposal
PositionSizingEngine (§35)
   - risk_amount = equity × per_trade_risk_budget × throttle_factor × confidence_scaler
   - qty = risk_amount / stop_distance
   - 制約: liquidity cap (ADV%), spread cost, existing/theme exposure, correlation, gap risk
   - Anti-Martingale: 直近損失後のサイズ増加を拒否 (INV-10)
   ↓ SizedProposal
CapitalAllocationEngine (§36)
   - 複数候補に限られたSettled Cashを配分
   - Total Exposure <= 100% (INV-11)
   ↓ AllocatedOrderIntent
MasterRiskController (§42)  — deterministic / AI禁止
   チェック（全PASSで承認、1つでも失敗なら理由付きREJECT）:
     1. leverage <= 1.0
     2. cash: order_value <= settled_cash
     3. settlement: 未決済資金の使用禁止
     4. exposure: total <= 100%
     5. position size: liquidity cap以下
     6. stop existence
     7. correlation / theme concentration
     8. gap risk score
     9. broker state (接続・reconciliation一致)
    10. market state (open / halt)
    11. data health (Data Integrity Engineの状態)
    12. risk state (Drawdown throttle / MASTER STOP)
   ↓ RiskApprovedOrder (承認トークン付き)
ExecutionEngine
```

## Drawdown Throttling (§39) — `RiskConfig` (frozen)

NORMAL → REDUCED_SIZE → NO_NEW_ENTRY → REVIEW_SAFE_MODE
（閾値は invariants.md 参照。AIは実行時に変更できない。）

## MASTER STOP (§43)

- `HALT_NEW_ENTRIES`: 新規Entry停止 + Open Entry Orders cancel。Protective Stop / Risk-reducing SELL / 緊急清算は**継続**。
- `SAFE_EXIT`: 新規禁止 + ポジション縮小のみ許可。
- `FULL_BROKER_DISCONNECT`: 全送信不能（接続断の記録状態）。

## Position Sizing 詳細 (§35)

入力: Portfolio Equity / Stop Distance / Max Risk Budget / Volatility / Liquidity /
Spread / Existing Exposure / Theme Exposure / Correlation / Gap Risk / Confidence / Cost

デフォルト設定（`risk_config.py`、Human承認でのみ変更）:
- per_trade_risk_budget: equity の 1.0%
- max_position_pct: equity の 25%
- max_theme_exposure_pct: 40%
- max_adv_participation: 日次出来高の 1%（それ以上は流動性キャップ）
- correlation_penalty: 既存保有との相関 > 0.7 でサイズ 50% 減

## Gap / Overnight Risk (§40)

Stop Orderは価格保証ではない。Overnight持ち越しには:
- イベントカレンダー照会（Earnings等 §16）
- gap_risk_score = f(過去ギャップ分布, イベント有無, volatility)
- スコアが閾値超過なら持ち越し拒否 or サイズ減
