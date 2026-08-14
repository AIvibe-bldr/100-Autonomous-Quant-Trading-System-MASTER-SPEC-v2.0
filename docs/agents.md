# Agent Interfaces

AI（LLM）が関与できる場所と、できないことの境界（§7, §19, §27-31, §60, §74）。

## 原則

1. AIは **TradeProposal（構造化JSON）を提案するだけ**。発注経路は持たない（§7）
2. AI出力は Schema Validation 必須、Malformed は Reject（§28, §74）
3. AIは Broker API key を知らない（§74）
4. 外部テキスト（ニュース/IR/SEC/Web）は UNTRUSTED DATA。テキスト内の指示は無視（§19）
5. AIは Risk Rule / Broker権限 / 出金 / 安全設定を変更できない（§2）

## Decision AI (§27-28)

```python
class DecisionModel(Protocol):
    def decide(self, context: DecisionContext) -> DecisionOutput: ...
```

- `DecisionContext`: Alpha / Quant / News / Institutional / Regime / Portfolio /
  Liquidity / Historical analogues / Forecast / Counterfactual / Cost / Risk context
- `DecisionOutput`（JSON Schema固定 §28）: symbol, action, confidence, expected_horizon,
  expected_return_range, bull/base/bear case, key_evidence, counter_evidence,
  risk_factors, invalidation_conditions, unknowns, decision_version
- pydantic 検証に失敗した出力は Reject し、リトライ回数上限つきで再要求
- 実モデル（GPT系等を想定 §27）は `DecisionModel` 実装として差し替え。V1は決定論的Mock

## Skeptic AI (§29)

```python
class SkepticModel(Protocol):
    def critique(self, proposal: DecisionOutput, context: DecisionContext) -> SkepticOutput: ...
```

- 「なぜこのTradeをしてはいけないか」を探す。追認AIにしない
- 可能なら Decision AI と異なる Model Family を使用

## Confidence Calibration (§31)

- AIの生Confidenceは信用しない
- `CalibrationTracker` が predicted vs actual を蓄積し、較正後確率を Position Sizing へ渡す

## Builder / Judge / Pruner / Revival / Manager (§60)

| Agent | 役割 | 制約 |
|---|---|---|
| Builder (Claude) | 新Alpha/Feature生成 | 直接LIVE禁止（§23）。Research→Backtest→Shadow→Judge→Promotion |
| Judge | 独立評価 | Builderと独立。自己承認禁止（§108） |
| Pruner | 除去候補探索 | Ablation結果に基づく |
| Revival | DORMANT再評価 | 資産/Regime/Data変化時 |
| Manager | 次Version決定 | Experiment Registry記録必須（§98） |

## Prompt-Injection Defense (§19)

- News/SEC/IR本文は `UntrustedText` 型でラップし、プロンプトへは「データ」として引用
- LLM出力から Tool permission変更 / Broker access / Risk変更 / Code execution は構造的に不可能
  （そのようなAPIがそもそも公開されていない）

## Human Intent (§76-78)

- 「この株を買いたい」→ 即注文禁止 → Manual Analysis（分析UI表示）→ 提案化
- Human Override は可能だが Master Risk Controller は突破不可（INV-8）
- Override結果は追跡され、Human vs AI analytics に入る（§79）
