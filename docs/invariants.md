# Safety Invariants (§2, §102)

安全原則は文章ではなく **実行可能なテスト** として維持する。
実体: `tests/unit/test_invariants.py` ＋ `packages/common/safety.py`（実行時ガード）。

## Immutable Safety Rules（AIから変更不能 §2）

| ID | 不変条件 | 実行時ガード | テスト |
|---|---|---|---|
| INV-1 | `leverage <= 1.0` | MasterRiskController.check_leverage | test_leverage_never_exceeds_one |
| INV-2 | `liabilities_from_margin == 0` | Ledgerがmargin負債を型レベルで持たない | test_no_margin_liabilities |
| INV-3 | 空売り禁止（保有数量を超えるSELL拒否） | MasterRiskController.check_no_short | test_no_short_selling |
| INV-4 | `every_entry_has_stop_plan` | LossControlEngineがStopPlan無しProposalを拒否 | test_every_entry_has_stop_plan |
| INV-5 | `order_value <= allowed_cash`（Settled Cashのみ） | MasterRiskController.check_cash | test_order_value_within_settled_cash |
| INV-6 | `live_ai_cannot_call_broker` | RiskApprovalトークン必須＋import禁止 | test_ai_cannot_reach_broker |
| INV-7 | `duplicate_client_order_id_impossible` | BrokerAdapterのidempotency登録 | test_duplicate_client_order_id_rejected |
| INV-8 | `human_override_cannot_bypass_risk` | Override結果もMasterRiskControllerを通過 | test_human_override_cannot_bypass_risk |
| INV-9 | `live_environment != paper_environment` | Environment型 + credential分離 | test_environment_separation |
| INV-10 | Martingale禁止（損失後のサイズ増加検知） | PositionSizingEngineのanti-martingaleチェック | test_no_martingale_sizing |
| INV-11 | Total Exposure <= 100% | CapitalAllocationEngine | test_total_exposure_cap |
| INV-12 | MASTER STOP中もProtective Stop/Risk-reducing SELLは通す (§43) | MasterRiskControllerの状態別許可表 | test_master_stop_allows_protective_exit |
| INV-13 | Drawdown閾値はAI変更不可（設定ファイル＋Human Approval） | RiskConfigはfrozen dataclass、変更はファイル経由のみ | test_risk_config_immutable_at_runtime |
| INV-14 | AI出力はSchema Validation必須・Malformed Reject (§28,74) | DecisionOutputのpydantic検証 | test_malformed_decision_rejected |
| INV-15 | Stop OrderなしのOvernight持ち越し禁止。約定した建玉には必ずBrokerに保護Stop注文が存在する | TradingPipeline.place_protective_stop（BUY約定直後にRisk経由でSTOP SELL発注） | test_overnight_requires_stop |
| INV-16 | Decision方向とOrder方向の不一致はAuditでREJECT（ADDENDUM A3） | IndependentAuditor.audit | test_decision_buy_order_sell_rejected |
| INV-17 | Risk承認後のField変更はHash mismatchでREJECT・再Audit必須（A4） | ApprovedOrderSnapshot.hash + ExecutionEngine照合 | test_hash_mismatch_rejected |
| INV-18 | Execution Engineは注文を自ら変更しない（A4-2） | Broker requestはSnapshotのみから構築 | test_execution_cannot_modify_order |
| INV-19 | Audit AI不能時、強制Audit対象（High-risk）Tradeは発注禁止（A3-6） | pipeline audit step | test_audit_unavailable_blocks_high_risk |
| INV-20 | Malformed Audit出力はREJECT扱い（A3-3） | validate_audit_output | test_malformed_audit_rejected |
| INV-21 | Decision Snapshotは書換不能・変更は新Decision ID必須（A1-1） | DecisionQualityEngine.record | test_decision_snapshot_immutable |

## MASTER STOP Semantics (§43)

| 状態 | 新規Entry | Entry注文Cancel | Protective Stop | Risk-reducing SELL | 緊急清算 |
|---|---|---|---|---|---|
| NORMAL | ✅ | - | ✅ | ✅ | ✅ |
| HALT_NEW_ENTRIES | ❌ | ✅自動 | ✅ | ✅ | ✅ |
| SAFE_EXIT | ❌ | ✅自動 | ✅ | ✅ | ✅（縮小のみ） |
| FULL_BROKER_DISCONNECT | ❌ | - | ❌(送信不能) | ❌(送信不能) | ❌ |

## Drawdown Throttling (§39)

| Drawdown (from HWM) | 状態 | 効果 |
|---|---|---|
| < 10% | NORMAL | 通常Risk Budget |
| 10–20% | REDUCED_SIZE | Risk Budget 50% |
| 20–30% | NO_NEW_ENTRY | 新規Entry禁止 |
| >= 30% | REVIEW_SAFE_MODE | SAFE_EXIT + Human Review要求 |

閾値は `packages/common/risk_config.py` の設定値。変更は Human Approval + Versioning + Test 必須（§39）。
AIエージェントは実行時にこの値を書き換えるAPIを持たない。
