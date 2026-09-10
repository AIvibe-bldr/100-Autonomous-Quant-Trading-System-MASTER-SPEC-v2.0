# Architecture / Code Quality Rules

このプロジェクトでは、単に「動くコード」を作るのではなく、長期的に保守可能で一貫したコードベースを
維持することを優先する。ソースは「Architecture / Code Quality Rules」指示書（2026-09投入）。

このファイルは**規約**であって仕様ではない。パイプライン構成・Domain境界・不変条件の正典は
`docs/architecture.md`（Service Boundary表）/ `docs/MASTER_SPEC.md`（Requirement Map）/
`docs/invariants.md`（INV-1〜21）であり、ここではそれらと矛盾する記述を書かない。

## 1. 変更前に全体構造を確認する

実装前に必ず以下を確認する。

- 現在のディレクトリ構造（`packages/` は共有ドメインモデル・Utility、`services/*` は
  §7のパイプラインの各段、`apps/` はAPI/UI）
- 関連モジュール・依存関係（`docs/architecture.md`のCore PipelineとService Boundary表）
- 既存の類似実装（同じ責務のクラス/関数がすでにないか検索してから作る）
- 共通Utility（`packages/common/*`）
- Domain Model（`packages/schemas/*`）
- API boundary（`apps/api/main.py` は read-only。書き込みを追加する場合はそれ自体が
  Architecture上の大きな変更として§23の事前報告対象）
- DB boundary（現状、実データベースは存在しない。`docs/SAFETY_AUDIT.md` F3/F4/F7/F9参照）
- External service boundary（`packages/broker_adapters/`, LLM Provider = `packages/common/llm_client.py`）

同じ役割のコードが既に存在する場合、新しい仕組みを重複して作らない。

## 2. 既存アーキテクチャを優先する

新機能は既存アーキテクチャ（`docs/architecture.md`のCore Pipeline）に自然に組み込む。
場当たり的に新しいService、Helper、Manager、Utilを追加しない。
既存構造が明確に問題である場合のみ、理由を示した上で変更する。

## 3. 責務を分離する（このプロジェクトでのDomain対応）

| 責務 | このプロジェクトでの実装場所 |
|---|---|
| 売買判断（Strategy） | `services/decision`（Decision AI / Skeptic AI）、`services/alpha_factory` |
| リスク制限（Risk Engine） | `services/risk`（Master Risk Controller、deterministic・AI禁止） |
| 注文実行（Execution） | `services/execution`（Order State Machine含む、deterministic・AI禁止） |
| 外部Broker通信（Broker Adapter） | `packages/broker_adapters`（現状 `PaperBroker` のみ） |
| 永続化（DB Repository） | 未実装（`docs/SAFETY_AUDIT.md` F9）。追加する際は本ルールに従い専用層として切る |
| 観測（Logging / Monitoring） | `services/supervisor`（Heartbeat）。構造化ログは未整備（同F7） |

1つのModule/Class/Functionに複数の責務を詰め込まない。既存の分離（AIは提案のみ・Riskは
deterministic・Executionはdeterministic、`docs/architecture.md`のAI利用列）を壊さない。

## 4. 依存方向を一方向にする

循環依存を作らない。禁止例（このプロジェクトで特に注意する経路）：

- `services/decision` → `packages/broker_adapters` 直接アクセス（AST強制テストで既に禁止：
  `tests/unit/test_invariants.py::test_ai_cannot_reach_broker_imports`）
- `apps/api`（UI/API層）→ `packages/broker_adapters` 直接アクセス
- `services/pdca` / `services/alpha_factory`（Learning）→ `packages/common/risk_config.py` や
  `services/risk/master_controller.py` の状態を直接書き換え（現状ゼロ件、維持すること）
- `packages/broker_adapters` → `services/decision` 等の上位ロジックへの依存

## 5. レイヤーを飛び越えない

正式パイプライン（`docs/architecture.md`）を必ず通す。ショートカット経路を追加しない。

```
Decision AI → Skeptic AI → Loss Control → Position Sizing → Capital Allocation
→ Independent Audit AI → Master Risk Controller（deterministic・最終防壁）
→ Immutable Approved Order Snapshot（hash固定）→ Execution Engine → Broker
```

## 6. Single Source of Responsibility

同じBusiness Ruleを複数箇所にコピーしない。特に：

- Position Limit / Exposure Limit の計算は `services/risk`（`MasterRiskController`）に集約する
  （`services/position_sizing`・`services/capital_allocation`が独自にRisk上限を再定義しない）
- Broker固有ロジックは `packages/broker_adapters` に集約する

## 7-9. 重複コード・過剰な抽象化・関数サイズ

意味的に同じ責務の場合のみ共通化する（数行似ているだけで抽象化しない）。実際に複数実装や
差し替えが必要な場合のみFactory/Manager/Provider/Wrapper/BaseClassを追加する。将来使うかも
しれないという理由だけで抽象化しない。1つのFunctionにvalidation/DB access/external API/
business logic/loggingを同時に持たせない。

## 10-12. データフロー・Side Effect・Pure Logic分離

重要なBusiness Flow（入力→Validation→Business Logic→State Change→External Effect→Result）は
追跡可能にする。DB write / Broker API / HTTP / filesystem / notification 等のSide Effectは
境界Layer（`services/execution`, `packages/broker_adapters`, 将来のDB Repository層）に集約し、
Domain Logic（`services/risk`の判定ロジック等）内に直接書かない。可能な範囲でPure Function
（例：`packages/common/risk_config.py`のthrottle計算）とSide Effectを分離し、テスト容易性を
保つ。

## 13. Naming

名前から責務が理解できるようにする。`utils`/`helpers`/`manager`/`common`/`misc`のような
汎用名は避け、より具体的な名前を優先する（例: `calculatePositionSize`, `validateOrderRisk`,
`reconcileBrokerPosition`, `submitBrokerOrder`）。ただし既存の`packages/common/`はこの
プロジェクトで確立された命名（真に横断的な基盤: Clock/Ledger/Environment等）であり、
中身のない汎用置き場にしないことが条件。

## 14-15. ファイル肥大化・変更範囲

巨大ファイルは責務単位で分割を検討する（行数だけを理由に機械的分割はしない）。1つの機能追加で
無関係なファイルまで変更しない。既存コードを整理したくなっても、今回の変更に直接関係しない
大規模Refactorは別タスクにする（`docs/SAFETY_AUDIT.md`の推奨順序も参照— 1コミット1責務）。

## 16-17. 検索・循環依存禁止

新しいFunction/Service/Repository/Schemaを作る前に既存コードベースを検索し、類似実装が
あれば再利用する。Circular Dependencyが発生する場合は責務分離またはInterface Boundaryを
見直す。

## 18. Domain Boundaryを維持する

Domain一覧はこのプロジェクトでは`docs/architecture.md`のService Boundary表がそれにあたる
（market-data / data-validation / quant / news・institutional / feature-manager /
alpha-factory・regime / decision / loss-control / position-sizing / capital-allocation /
risk / execution / reconciliation / supervisor / pdca / cost-manager）。別Domainの内部実装へ
直接依存せず、明確なInterface（関数シグネチャ・pydantic Schema）を通す。

## 19. Public Interfaceを小さく保つ

Moduleの外部公開APIを必要最小限にする。内部実装をむやみにexportしない。

## 20-22. Architecture Drift防止・Review・Refactor基準

新しい変更が既存Architecture Rule（本ファイルおよび`docs/architecture.md`）に違反していないか
確認する。違反が必要な場合は「なぜ必要か／他の方法はないか／長期的影響」を説明する。
実装後は次を確認する: 新しい循環依存がないか／同じBusiness Logicが重複していないか／
Layer越境がないか／不要なUtilityが増えていないか／巨大Function・巨大Moduleが増えていないか／
External APIがDomain Layerへ漏れていないか／DB LogicがBusiness Logicへ混ざっていないか。
Refactorは「きれいに見えるから」ではなく、重複/責務混在/循環依存/テスト困難/変更時の複数箇所
修正/Architecture Rule違反がある場合に行う。

## 23. 実装前報告

比較的大きな変更の場合、実装前に簡潔に次を示す：どのLayerに実装するか／既存のどのModuleを
再利用するか／新規Moduleが必要か／Dependency Flow／DB・APIへの影響。

## 24. 実装後報告

変更した責務／新規Module／再利用した既存Module／Dependency変更／Architecture上の影響／
技術的負債を増やしていないかを報告する。

## 最重要原則

コードを追加するたびに局所最適化しない。常に「このコードはコードベース全体のどこに属するのか」
を先に判断する。新しい機能を最短距離で既存コードへ接続するのではなく、既存Architectureに
自然に組み込む。目的は「動くコードの集合」ではなく「責務と依存関係が明確な一つのシステム」を
維持することである。
