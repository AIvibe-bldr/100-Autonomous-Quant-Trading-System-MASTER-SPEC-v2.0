# Database Schema

V1 は PostgreSQL を想定（§100）。本実装ではスキーマ定義＋インメモリ/SQLite互換のリポジトリ層を提供し、
DDL は `packages/schemas/sql/` に配置する。全テーブル共通の原則:

- **Event Sourcing 可能な形**（§49）: 主要状態はイベント追記で再構成できる
- **Unified Clock**（§9）: `event_time / source_time / received_time / processed_time` (UTC)
- **Version Everything**（§99）: 判断・計算に関与する全行が version 列を持つ

## テーブル一覧

### market_data.bars
```
symbol TEXT, ts TIMESTAMPTZ, open NUMERIC, high NUMERIC, low NUMERIC, close NUMERIC,
volume BIGINT, vwap NUMERIC, source TEXT, received_time TIMESTAMPTZ, processed_time TIMESTAMPTZ,
data_version TEXT
PRIMARY KEY (symbol, ts, source, data_version)
```

### market_data.quotes
```
symbol, ts, bid NUMERIC, ask NUMERIC, bid_size, ask_size, halt_status TEXT, source, ...
CHECK (bid <= ask)  -- Data Integrity (§11) はアプリ層でも二重チェック
```

### pit.snapshots  (Point-in-Time Store §10)
```
key TEXT, as_of TIMESTAMPTZ, payload JSONB, source TEXT, received_time TIMESTAMPTZ
-- 「as_of時点で知り得た値」のみを返すAPIでアクセス
```

### universe.symbols (§12)
```
symbol TEXT, listed_from DATE, delisted_at DATE NULL, exchange TEXT, status TEXT
-- Delisted含む。バックテストはas_ofでフィルタ
```

### features.values (§22)
```
feature_name TEXT, symbol TEXT, value JSONB, ts TIMESTAMPTZ,
source TEXT, calculation_version TEXT, data_version TEXT
```

### decisions.provenance (§75)
```
decision_id UUID PK, created_at, environment TEXT,
input_features JSONB, news_refs JSONB, data_timestamps JSONB,
model TEXT, model_version TEXT, prompt_version TEXT,
output JSONB, skeptic_output JSONB, risk_decision JSONB,
stop_plan JSONB, position_size JSONB, order_ref UUID, fill_refs JSONB, result JSONB
```

### orders.orders (§45-46)
```
client_order_id TEXT UNIQUE NOT NULL,   -- Idempotency (§46)
order_id UUID PK, decision_id UUID, symbol, side, qty NUMERIC, order_type, limit_price,
state TEXT,  -- CREATED/RISK_APPROVED/SUBMITTED/ACKNOWLEDGED/PARTIALLY_FILLED/FILLED/
             -- CANCEL_REQUESTED/CANCELLED/REJECTED/EXPIRED/UNKNOWN
risk_approval JSONB NOT NULL,           -- Master Risk Controllerの承認記録
environment TEXT, created_at, updated_at, version INT
```

### orders.state_transitions  (Event Sourcing)
```
order_id, from_state, to_state, at TIMESTAMPTZ, reason TEXT, broker_payload JSONB
```

### fills.fills
```
fill_id UUID, order_id, qty, price, fees, ts, broker_fill_id TEXT UNIQUE
```

### ledger.entries (§85)
```
entry_id UUID, at TIMESTAMPTZ, kind TEXT,  -- CASH/TRADE/FEE/DIVIDEND/FX/CORPORATE_ACTION
symbol TEXT NULL, qty NUMERIC, amount NUMERIC, currency TEXT, cost_basis NUMERIC,
realized_pnl NUMERIC, ref JSONB
```

### portfolio.equity_snapshots (§3,5)
```
at TIMESTAMPTZ, cash NUMERIC, positions_value NUMERIC, liabilities NUMERIC,
equity NUMERIC,  -- = cash + positions_value - liabilities
high_water_mark NUMERIC, drawdown NUMERIC
```

### risk.decisions
```
at, proposal_id, checks JSONB, passed BOOLEAN, reject_reasons JSONB, risk_state TEXT
-- rejectされた提案も保存（Counterfactual §53）
```

### experiments.registry (§98)
```
experiment_id, hypothesis TEXT, change JSONB, baseline TEXT, challenger TEXT,
period, data_refs JSONB, result JSONB, decision TEXT, rollback JSONB
```

### costs.entries (§80-81)
```
at, category TEXT,  -- AI/MARKET_DATA/NEWS/SERVER/DB/BROKER_FEE/TRANSACTION/FX
amount NUMERIC, currency TEXT, note TEXT
```

### supervisor.heartbeats (§67)
```
service TEXT, at TIMESTAMPTZ, status TEXT, last_success TIMESTAMPTZ,
latency_ms NUMERIC, error_rate NUMERIC, queue_depth INT, data_age_sec NUMERIC
```

## Namespace分離 (§73)
環境ごとに schema prefix を分ける: `paper_orders.orders` / `live_orders.orders` など。
V1実装ではリポジトリ層が `Environment` を必須引数として受け取り、混在をコンストラクタで拒否する。
