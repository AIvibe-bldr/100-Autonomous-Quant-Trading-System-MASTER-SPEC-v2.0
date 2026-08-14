# Execution Model

## Order State Machine (§45)

```
            ┌──────────┐
            │ CREATED  │
            └────┬─────┘
                 │ Master Risk PASS
            ┌────▼──────────┐     REJECT→ ┌──────────┐
            │ RISK_APPROVED │             │ REJECTED │
            └────┬──────────┘             └──────────┘
                 │ ExecutionEngine.submit
            ┌────▼──────┐
            │ SUBMITTED │──timeout/不明──────────┐
            └────┬──────┘                        │
                 │ broker ack                    ▼
            ┌────▼─────────┐              ┌─────────┐
            │ ACKNOWLEDGED │─────────────▶│ UNKNOWN │→ Reconciliation必須
            └────┬─────────┘              └─────────┘
        ┌────────┼──────────────┐
        ▼        ▼              ▼
┌──────────────────┐ ┌────────┐ ┌─────────┐
│ PARTIALLY_FILLED │ │ FILLED │ │ EXPIRED │
└───────┬──────────┘ └────────┘ └─────────┘
        │ cancel要求
┌───────▼──────────┐   ┌───────────┐
│ CANCEL_REQUESTED │──▶│ CANCELLED │
└──────────────────┘   └───────────┘
```

許可される遷移は `services/execution/state_machine.py` の遷移表で定義し、
不正遷移は例外。全遷移は `state_transitions` にイベントとして追記（Event Sourcing §49）。

**UNKNOWN は重大状態**: 到達したら新規Entry停止 + Broker reconciliation 実行（§45）。

## Idempotency (§46)

- 全注文に Unique `client_order_id`（`{env}-{decision_id}-{seq}` 形式）
- BrokerAdapter は同一 client_order_id の再送を**構造的に**拒否（登録テーブルで検知）
- Timeout後は無条件再送せず、`get_order_status()` で Broker に状態確認してから Retry

## Stale Order Control (§47)

- Entry注文は `stale_after_sec`（既定300s）経過で再評価対象
- Cancel/Replace は新 client_order_id ＋ version increment で行う

## Reconciliation (§48-49)

タイミング: 起動時 / 定期 / 異常時（UNKNOWN発生時）。
比較対象: Cash / Position / Open Orders / Fills / Buying Power。
不一致 → `HALT_NEW_ENTRIES`（新規Entry停止）。
実売買では Broker が外部 Source of Truth（§49）。内部 Ledger は照合対象として全イベントを保持。

## Broker Adapter Interface (§100)

```python
class BrokerAdapter(Protocol):
    def submit_order(self, order: BrokerOrderRequest) -> BrokerAck: ...
    def cancel_order(self, client_order_id: str) -> BrokerAck: ...
    def get_order_status(self, client_order_id: str) -> BrokerOrderStatus: ...
    def get_positions(self) -> list[BrokerPosition]: ...
    def get_cash(self) -> BrokerCashBalance: ...
    def get_open_orders(self) -> list[BrokerOrderStatus]: ...
    def get_fills(self, since: datetime) -> list[BrokerFill]: ...
```

V1実装: `PaperBroker`（決定論的な約定シミュレーション: spread/slippage/partial fill/fees を模擬 §25）。
V1候補の実Broker: IBKR Paper → Live（§100）。Adapter追加は本Interfaceの実装のみで足りる。

## Execution Engine (§44)

- deterministic。LLM呼び出しコードを含まない（invariantテストでimport検査）
- 入力は `RiskApprovedOrder` のみ。RiskApproval トークン検証に失敗した注文は例外
- Broker credential は ExecutionEngine の構築時にのみ注入され、他サービスへ渡らない
