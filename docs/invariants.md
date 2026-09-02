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

## §28 必須テスト対応表

役割分離改訂で要求された18件。実装は `tests/unit/test_role_separation_spec.py`
（重複するものは既存スイートにも残してある。どの層が止めるのかを役割分離の
観点から直接主張するために再掲している）。

| # | ケース | 止める層 | テスト |
|---|---|---|---|
| 1 | Decision BUY / Order SELL → REJECT | Pre-Trade Audit AI | `test_decision_buy_but_order_sell_is_rejected` |
| 2 | Symbol変更 → REJECT | Pre-Trade Audit AI | `test_symbol_swap_between_decision_and_order_is_rejected` |
| 3 | 未保有銘柄SELL → REJECT | Master Risk Controller | `test_sell_of_unheld_symbol_is_rejected` |
| 4 | Position 10株でSELL 11株 → REJECT | Master Risk Controller | `test_sell_exceeding_held_quantity_is_rejected` |
| 5 | Position 10株でSELL 10株 → PASS可能 | Master Risk Controller | `test_sell_of_exactly_held_quantity_can_pass` |
| 6 | 未保有SELL判断はAVOID化（発注に至らない） | Pipeline | `test_pipeline_converts_unheld_sell_decision_into_avoid` |
| 7 | Leverage > 1 → REJECT | Master Risk Controller | `test_leverage_above_one_is_rejected` |
| 8 | Margin requirement発生 → REJECT | Master Risk Controller | `test_any_margin_requirement_is_rejected` |
| 9 | Insufficient Cash → REJECT | Master Risk Controller | `test_insufficient_settled_cash_is_rejected` |
| 10 | Missing Stop → REJECT | Master Risk Controller | `test_entry_without_stop_plan_is_rejected` |
| 11 | Duplicate Order ID → REJECT | Master Risk Controller | `test_duplicate_client_order_id_is_rejected` |
| 12 | 承認後のQuantity変更 → HASH MISMATCH | Approved Order Snapshot | `test_quantity_changed_after_approval_fails_hash_check` |
| 13 | 承認後のSymbol変更 → HASH MISMATCH | Approved Order Snapshot | `test_symbol_changed_after_approval_fails_hash_check` |
| 14 | Audit Timeout → LIVE新規Trade REJECT | Audit fail-closed | `test_audit_timeout_blocks_new_live_trade` |
| 15 | Audit malformed JSON → REJECT | Audit fail-closed | `test_audit_malformed_json_never_passes` |
| 16 | Broker Position mismatch → HALT_NEW_ENTRIES | Reconciliation | `test_broker_position_mismatch_halts_new_entries` |
| 17 | External NewsにSystem Prompt攻撃文 → 挙動不変 | 構造的（経路なし） | `test_injected_news_does_not_change_risk_configuration` 他2件 |
| 18 | BuilderがLIVE Strategy変更要求 → DENIED | Promotion Gate | `test_builder_cannot_change_a_live_strategy` |
| 19 | JudgeがPROMOTE → 自動LIVE昇格しない | Promotion Gate | `test_judge_recommends_but_never_promotes` 他2件 |
| 20 | Human Override → Master Risk突破不可 | Master Risk Controller | `test_human_override_cannot_bypass_master_risk` 他2件 |

## セキュリティ／正当性レビューで発見・修正した欠陥

3方向のレビュー（安全境界・新規コード正当性・長期実行の会計整合性）で発見し、
すべて再現確認のうえ修正した。回帰は `tests/unit/test_security_review_regressions.py`。

| # | 重大度 | 欠陥 | 修正 |
|---|---|---|---|
| SEC-1 | CRITICAL | `IndependentAuditor.environment` がPAPER既定でPipelineと非連動 → LIVEで監査なし発注 | environment必須化＋`__post_init__`で環境混在を構築時拒否 |
| SEC-2 | CRITICAL | 署名が`order_type`/価格を含まず、engineがsnapshotを自作 → 承認済MARKETがLIMIT/STOPに化ける | `intent_hash`を全執行フィールドに拡張し署名対象化。snapshotは呼出側必須＋approval紐付け検証 |
| SEC-3 | HIGH | 無価格MARKET注文で`order_value=0` → 金額系4チェックが自明に通過 | 価格不明は`priceable_order`でREJECT |
| SEC-4 | HIGH | `check_stale_orders`が保護Stopを取消（INV-15違反） | `is_protective_exit`を除外 |
| SEC-5 | HIGH | Drawdownスロットルが解除不能ラッチ（HWM単調＋建玉消滅でequity固定） | 人間承認必須の`rebase_high_water_mark`＋`/health`にthrottleとロックアウトを露出 |
| SEC-6 | HIGH | `settled_cash`に未決済金が混入（§14がPaperBroker任せ） | Brokerの`settled_cash`を参照。切断時は0扱い |
| — | HIGH | `Ledger._append`が検証前に変更しロールバックなし → INV-2違反状態を残す | 全レグを検証してからコミット |
| — | HIGH | `outcome_class`は生リターン、`outcome_score`はbenchmark調整後 → BADかつ100点が発生 | 両者を同一尺度（alpha_ret）に統一 |
| — | MED | MAE/MFEが方向未調整 → SELLで有利方向がリスク分母に | 方向調整後の符号で算出 |
| — | MED | `expected`horizonが後続horizonのMAEを継承（先読み） | `observed_at <= due`のみ参照 |
| SEC-7 | MED | `open_stops`のsymbolキー上書きでアンチマルチンゲール入力が13%乖離 | realizedをLedgerの`avg_cost`から算出、riskは累積 |
| SEC-8 | MED | `adv_shares`がリテラル1,000,000で§41が形骸化 | 実ADVを配線 |
| SEC-9 | MED | `_final_theses`がセッション跨ぎで蓄積（18セッションで177件表示） | `run_session`冒頭でクリア |
| SEC-10 | HIGH | Mockの`get_quote`と`get_bars`が別価格過程 → Stopが一度も発火せず関連テストが空振り | 共通エポックからの単一ウォークに統合＋ボラティリティドラッグ補正 |

### 変異テストで露呈した無効テスト

「本番コードを壊しても全テストが通る」箇所が複数あった。特に:

- Master Risk Controller のチェック15 (`stale_order`) / 16 (`spread`) は**テストが1件も存在しなかった**
- `test_pipeline_populates_snapshot_skeptic_id` は `assert any(sid for sid in seen)` で、
  `skeptic_id` にリテラルを入れても通っていた
- `Ledger._append` のガードは `record_fill` 側の検証追加で到達不能になっていたため、
  `deposit` 経由の負残高テストを別途追加

現在は上記すべてに対して変異注入で kill を確認済み。
