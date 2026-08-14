# Experiments / Testing Strategy / Failure Matrix / Paper→Live Gate

## Testing Strategy (§100, §102)

| 層 | 場所 | 内容 |
|---|---|---|
| unit | `tests/unit` | 各エンジンの決定論的ロジック |
| invariants | `tests/unit/test_invariants.py` | §102 の実行可能不変条件（INV-1〜15） |
| property | `tests/property` | サイズ計算・状態遷移の性質テスト |
| integration | `tests/integration` | Proposal→Risk→Execution→Reconciliation E2E |
| replay | `tests/replay` | 過去データのpoint-in-time再生（§62） |
| chaos | `tests/chaos` | 故障注入（§70）: feed down / stale feed / broker disconnect / invalid JSON / duplicate callback / partial fill / order timeout |
| failure | `tests/failure` | 障害シナリオの回帰テスト（Incident Postmortem由来 §71） |

## Failure Matrix (§70)

| 故障 | 検知 | 対応 | テスト |
|---|---|---|---|
| feed down | Heartbeat data_age超過 | HALT_NEW_ENTRIES | chaos/test_feed_down |
| stale feed | DataIntegrityEngine | Entry停止 | chaos/test_stale_feed |
| wrong price (extreme) | DataIntegrityEngine | 当該銘柄Entry停止 | chaos/test_extreme_price |
| Bid > Ask | DataIntegrityEngine | 当該quote破棄+警告 | unit/test_data_integrity |
| Broker disconnect | Adapter例外/timeout | FULL_BROKER_DISCONNECT記録 | chaos/test_broker_disconnect |
| LLM timeout / invalid JSON | DecisionOutput検証 | 提案Reject（発注なし） | unit/test_malformed_decision |
| DB unavailable | Repository例外 | SAFE側停止 + Human review | chaos/test_db_down |
| duplicate callback | fill_id/broker_fill_id unique | 冪等に無視 | chaos/test_duplicate_callback |
| partial fill | Order State Machine | PARTIALLY_FILLED追跡 | integration/test_partial_fill |
| order timeout | UNKNOWN状態 | Reconciliation + Entry停止 | chaos/test_order_timeout |
| market halt | Calendar/halt status | 新規停止 | unit/test_market_halt |

## Experiment Registry (§98)

全変更は Experiment として登録: Hypothesis / Change / Baseline / Challenger / Period /
Data / Result / Decision / Rollback。実装: `packages/common/experiments.py`。

## Champion / Challenger (§58) ・ Shadow (§56) ・ Ablation (§57)

- 現行=Champion、新候補=Challenger を Shadow Portfolio で並走
- 統計的・実務的優位が確認された場合のみ Promotion
- Feature Lifecycle: ACTIVE → REDUCED → SHADOW → DORMANT → RETIRED（§59, §61）

## Paper → Live Gate (§105)

期間だけでは昇格しない。全条件必須:

- [ ] Critical safety violations = 0
- [ ] Leverage violation = 0
- [ ] Duplicate execution = 0
- [ ] Reconciliation consistency（全照合一致）
- [ ] Failure scenarios pass（Failure Matrix全項目）
- [ ] Stop tracking functional
- [ ] Replay pass
- [ ] Data integrity pass
- [ ] Audit completeness（全注文にProvenance）
- [ ] Shadow system functional

具体的必要数（Phase 0 定義値、Human承認で変更可）:
- Paper注文 ≥ 200件、うちStop発動 ≥ 20件
- Replay検証 ≥ 60営業日分
- Chaosシナリオ 11種 全PASS

## Live Capital Ramp (§104)

同一口座内で Exposure Cap を段階解放: 10% → 25% → 50% → 100%。
昇格条件は上記Gateのサブセット＋直近期間の safety violation 0。

## Live Deployment Rule (§106)

PR → Tests → Review → Human approval → Deployment。
Builder Agent が直接 Production を書き換えることは禁止。
