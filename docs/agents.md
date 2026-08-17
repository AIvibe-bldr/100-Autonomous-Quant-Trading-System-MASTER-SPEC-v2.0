# Agent Interfaces

AI（LLM）が関与できる場所と、できないことの境界（§7, §19, §27-31, §60, §74）。

## 原則

1. AIは **TradeProposal（構造化JSON）を提案するだけ**。発注経路は持たない（§7）
2. AI出力は Schema Validation 必須、Malformed は Reject（§28, §74）
3. AIは Broker API key を知らない（§74）
4. 外部テキスト（ニュース/IR/SEC/Web）は UNTRUSTED DATA。テキスト内の指示は無視（§19）
5. AIは Risk Rule / Broker権限 / 出金 / 安全設定を変更できない（§2）

## Authority Hierarchy

上位が下位を常に上書きする。AIが全員PASSでも、上位がREJECTなら発注されない。

| 順位 | 主体 | 実装 |
|---|---|---|
| 1 | Immutable Safety Invariants | `tests/unit/test_invariants.py`（実行可能な不変条件） |
| 2 | Master Risk Controller | `services/risk/master_controller.py`（決定論・18チェック） |
| 3 | Execution Safety Rules | `services/execution/engine.py` |
| 4 | Approved Order Snapshot / Hash | `packages/schemas/audit.py` |
| 5 | Pre-Trade Audit AI | `services/decision/audit.py` |
| 6 | Loss Control / Position Sizing | Python決定論。AIは最終Sizeを決めない |
| 7 | Decision AI + Skeptic AI | 提案と反証のみ |
| 8 | Builder / Research | Production Strategyを直接変更できない |

## 役割別モデル割当（§25）

| 役割 | モデル | Provider |
|---|---|---|
| Decision AI | GPT-5.6 Sol | OpenAI（`DECISION_PROVIDER_TARGET`） |
| Skeptic AI | Claude Opus | Anthropic |
| Pre-Trade Audit AI | Claude Opus | Anthropic |
| Monitor AI | Claude Opus | Anthropic |
| Judge / PDCA / Decision Quality | GPT-5.6 Sol | OpenAI |
| Builder / Research | Claude Fable5 | Anthropic |

Skeptic と Pre-Trade Audit は**同じモデルを共有するが別Agent**である:
別Prompt・別実行タイミング・別Audit ID・別 `agent_id`
（`packages/common/llm_client.py` の `AgentModel.agent_id`）。
一方が他方の代わりを務めることはできない。

Scanner / Loss計算 / Position Sizing / Capital Allocation / Risk Controller /
AI Orchestrator / Order Hash / Execution / Reconciliation / Settlement /
Supervisor / Outcome数値採点は**Pythonの決定論コード**であり、LLMは関与しない。

## 固定パイプライン（§27）

```
Decision AI → Skeptic AI → Final Trade Thesis → Loss Control
  → Position Sizing (Python) → Capital Allocation (Python)
  → Pre-Trade Audit AI → Master Risk Controller (Python)
  → Immutable Approved Order Snapshot → Hash
  → Execution → Broker → Reconciliation
```

## Decision の6値（§2）

`DecisionAction` = BUY / SELL / HOLD / WAIT / NO_TRADE / AVOID。
このうち注文になるのは BUY / SELL のみ（`DecisionAction.is_order`）。

**SELL は既存Longの縮小・手仕舞いのみ**を意味する。空売りは禁止であり、
`sell_quantity <= current_long_quantity` を Python 側で必ず検証する
（`master_controller.py` の `no_short` チェックと、`pipeline.py` の
未保有SELLガードの二重防御）。未保有銘柄に対しては AVOID / WAIT / NO_TRADE
を用いる。

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
- 実モデル（GPT系等を想定 §27）は `DecisionModel` 実装として差し替え。V1は決定論的Mockが既定。
  実LLM実装は `services/decision/claude_adapters.py`（Claude API、`client.messages.parse`による
  Structured Outputs）。認証情報の有無で自動的にMock/実LLMを切替可能（`packages/common/llm_client.py`）

## Skeptic AI (§29)

```python
class SkepticModel(Protocol):
    def critique(self, proposal: DecisionOutput, context: DecisionContext) -> SkepticOutput: ...
```

- 「なぜこのTradeをしてはいけないか」を探す。追認AIにしない
- 反証対象は**投資判断そのもの**（Thesis・根拠・想定シナリオ・Regime適合）。
  注文機構（Quantity桁・Symbol一致・Stop方向）は Pre-Trade Audit AI の担当であり、
  Skeptic はそこに労力を割かない
- Decision AI と異なる Model / Provider を使用（Decision=GPT-5.6 Sol, Skeptic=Claude Opus）
- Broker アクセス禁止

## Independent Audit AI (ADDENDUM A3)

```python
class AuditModel(Protocol):
    def audit(self, decision: DecisionOutput, sized: SizedProposal,
              intent: OrderIntent, context: AuditContext) -> dict: ...
```

- 発注**直前**の意味的整合監査: Symbol/方向一致、Quantity合理性、Stop存在と方向、
  Horizon vs Stop幅、Thesis/Risk説明との整合、Stale Signal、不自然な桁
- 出力は `AuditOutput`（PASS/REJECT/REVIEW + reasons + detected_conflicts + severity）。
  Schema検証失敗 = REJECT扱い（INV-20）
- **最終責任は持たない**: Cash/Leverage/Duplicate等の絶対条件はMaster Risk Controllerの
  固定コードが担う（A3-4）。AuditはRiskの**前段**であり、Riskを代替しない
- Decision AIと**別Model Family**を推奨（A3-5）。Skeptic AI（§29）とは役割が異なる:
  Skepticは判断段階の反証、Auditは発注段階のDecision↔Order整合
- Conditional Audit（A3-6）: 大きいSize/High Vol/イベント直前/Disagreement大/新Alpha/
  初回LIVE/低流動性等では強制。Audit不能時、強制対象Tradeは発注禁止（INV-19）
- Audit AIはBrokerへアクセス不可（Decision AIと同様）

### LIVE は fail-closed（§9）

`Environment.LIVE` では `audit_all` や trigger条件に関係なく**全注文が強制Audit**であり、
Timeout / API失敗 / Malformed JSON / Schema失敗 / モデル不在 / 内部エラーの
いずれも `AuditUnavailableError` を送出して**発注を止める**。
「Auditが行われなかった」と「AuditがREJECTと言った」は別の事実なので、
前者をREJECT扱いに丸めず例外にしている。監査を飛ばしてBrokerへ送る設定は存在しない。

`REVIEW` 判定はLIVE自動発注に進まず、Human Review キュー
（`services/pdca/audit_log.py` の `queue_human_review`）へ送られる。

## Monitor AI

- 保有中ポジションとシステム状態の監視・異常の言語化
- 禁止: Broker発注 / Position変更 / Risk Rule変更 / System設定変更
- 出力は通知と Human Review キューへの投入のみ

## Confidence Calibration (§31)

- AIの生Confidenceは信用しない
- `CalibrationTracker` が predicted vs actual を蓄積し、較正後確率を Position Sizing へ渡す

## Decision Quality: Outcome と Process の分離（§15）

- **Outcome Score は Python が客観計算する**。Benchmark調整後リターン（同期間の
  指数リターンを差し引く）を MAE ベースの実負担リスクで割ってスケールするため、
  上昇相場に居ただけの判断が高得点にならない。LLMの自己申告
  `expected_return_range` には依存しない
- **Process Score のみ LLM が採点**し、その際に将来情報は渡さない
- 両者は独立に保持される: ルール違反のBUYがたまたま+50%なら
  Outcome=GOOD / Process=BAD、`overall_score` は Process 側に抑えられる

## Builder / Judge / Pruner / Revival / Manager (§60)

| Agent | 役割 | 制約 |
|---|---|---|
| Builder (Claude Fable5) | 新Alpha/Feature生成 | 直接LIVE禁止（§23）。Research→Backtest→Shadow→Judge→Promotion |
| Judge (GPT-5.6 Sol) | 独立評価 | Builderと独立。自己承認禁止（§108）。**PROMOTEを決定できない** |
| Pruner | 除去候補探索 | Ablation結果に基づく |
| Revival | DORMANT再評価 | 資産/Regime/Data変化時 |
| Manager | 次Version決定 | Experiment Registry記録必須（§98） |

### Judge は推薦するだけ（§14）

`JudgeRecommendation` = PROMOTE_RECOMMENDED / SHADOW / RESEARCH_MORE / DORMANT / REJECT。
`PROMOTE` という値は存在しない。Judgeが到達しうる最良の結果は
`recommendation=PROMOTE_RECOMMENDED, stage=SHADOW` であり、
`AlphaStage.PROMOTED` は `AlphaJudge.judge()` からは到達不能。
昇格は統計検証 → OOS → Walk-forward → Shadow → 安全テスト → Promotion Gate →
**人間承認**という別ゲートが担う（`ExitOptimizer.promote()` は
`decided_by` に人間の名前を要求する）。

## Prompt-Injection Defense (§19)

- News/SEC/IR本文は `UntrustedText` 型でラップし、プロンプトへは「データ」として引用
- LLM出力から Tool permission変更 / Broker access / Risk変更 / Code execution は構造的に不可能
  （そのようなAPIがそもそも公開されていない）

## Human Intent (§76-78)

- 「この株を買いたい」→ 即注文禁止 → Manual Analysis（分析UI表示）→ 提案化
- Human Override は可能だが Master Risk Controller は突破不可（INV-8）
- Override結果は追跡され、Human vs AI analytics に入る（§79）
