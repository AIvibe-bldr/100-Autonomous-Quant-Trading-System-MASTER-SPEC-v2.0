# Architecture / Code Quality Rules

このプロジェクトでは、単に「動くコード」を作るのではなく、「長期的に保守可能で、責務と依存関係が
明確で、安全に変更できる一貫したコードベースを維持すること」を最優先の開発原則とする。ソースは
「Architecture / Code Quality Rules」指示書（2026-09投入、2本）。新規機能追加・バグ修正・
リファクタリング・API追加・DB変更・AI機能追加すべてに恒久ルールとして適用する。

このファイルは**規約**であって仕様ではない。同じルールを複数ファイルへコピーして内容が乖離する
構造は作らない。

## 0. 関連ドキュメント（正典）

指示書は `docs/ARCHITECTURE.md` / `docs/ENGINEERING_RULES.md` / `docs/SAFETY.md` /
`docs/INVARIANTS.md` の整備を求めているが、このプロジェクトには既に同等の役割を果たす文書が
存在するため、新規に重複した文書は作らず、以下を正典として扱う。

| 求められている文書 | このプロジェクトでの正典 |
|---|---|
| ARCHITECTURE（構造・Layer・Domain・Dependency Rule） | `docs/architecture.md`（Core Pipeline / Service Boundary表） |
| ENGINEERING_RULES（コード品質・責務分離・変更ルール） | 本ファイル（CLAUDE.md） |
| SAFETY（安全設計） | `docs/invariants.md`（Safety Invariants）＋ `docs/risk.md`（Risk Pipeline）＋ `docs/execution.md`（Order State Machine） |
| INVARIANTS（絶対に破ってはいけない条件） | `docs/invariants.md`（INV-1〜21）＋ `docs/MASTER_SPEC.md`（Requirement Map） |
| 監査結果・既知のGap | `docs/SAFETY_AUDIT.md`（P0〜P3、2026-09時点の未解決事項） |

新しい安全設計・構造ドキュメントが必要になった場合も、まずこれらへの追記を優先し、本当に別文書が
必要な場合のみ新規作成し、ここに追記する。

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

循環依存を作らない。上位Layerが下位Layerを利用する形を基本とし、下位Layerが上位Layerの実装詳細へ
依存しないようにする。禁止例（このプロジェクトで特に注意する経路、すべて自動テストで機械的に
強制済み — §19参照）：

- `services/decision` → `packages/broker_adapters` 直接アクセス
  （`tests/unit/test_invariants.py::test_ai_cannot_reach_broker_imports`）
- `apps/api`（UI/API層）→ `packages/broker_adapters` 直接アクセス
  （`tests/unit/test_architecture_boundaries.py::test_api_layer_cannot_reach_broker_adapters`）
- `services/pdca` / `services/alpha_factory`（Learning）→ `packages/common/risk_config.py` や
  `services/risk/master_controller.py` の状態を直接書き換え
  （`tests/unit/test_architecture_boundaries.py::test_learning_layers_cannot_import_risk_state`）
- `packages/broker_adapters` → `services/decision` 等の上位ロジックへの依存
  （`tests/unit/test_architecture_boundaries.py::test_broker_adapters_cannot_depend_on_services`）
- AIがExecution Engineを迂回して外部Actionを直接実行する経路（構造的に不可能: AIはBroker
  credentialを持たず、`RiskApproval`のHMAC署名なしにExecutionへ到達できない）

## 5. レイヤーを飛び越えない

正式パイプライン（`docs/architecture.md`）を必ず通す。ショートカット経路を追加しない。

```
Decision AI → Skeptic AI → Loss Control → Position Sizing → Capital Allocation
→ Independent Audit AI → Master Risk Controller（deterministic・最終防壁）
→ Immutable Approved Order Snapshot（hash固定）→ Execution Engine → Broker
```

禁止例: UIからDBへ直接アクセス／UIからExternal APIへ直接アクセス／StrategyからBroker APIへ
直接アクセス／Domain LogicからUIへ依存／MonitoringからBusiness Logicを書き換える。

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

比較的大きな変更の場合、実装前に簡潔に次を示す：どのDomain・どのLayerに実装するか／既存の
どのModuleを再利用するか／新規Moduleが本当に必要か／Dependency Flow／DB・API・外部Serviceへの
影響／Safetyへの影響。場当たり的にファイルを追加しない。

## 24. 実装後報告

変更した責務／新規Module／再利用した既存Module／Dependency変更／Architecture上の影響／
技術的負債を増やしていないかを報告する。

## 25. 安全性をArchitectureの一部として扱う

安全性を追加機能として扱わず、Architectureの一部として設計する。重要な処理については
Business Logic / Safety・Validation / Execution を必要に応じて分離する。AIやUIなど上位Layerから
Safety Layerを迂回できない構造にする——このプロジェクトでは既に達成されている（`services/decision`
はBroker/RiskConfigへ到達不能、`services/risk/master_controller.py`が唯一のRiskApproval発行者、
§4参照）。新しい機能を足すたびにこの境界を意図せず壊していないか、実装後に必ず確認する。

## 26. AI出力を直接Actionへ接続しない

AIを利用する箇所では次の経路を基本とする（既にこのプロジェクトの正式パイプラインそのもの）。

```
AI Output → Schema Validation（pydantic StrictModel） → Deterministic Validation
→ Business/Safety Rule（Master Risk Controller） → Execution
```

AI OutputをそのままDB変更・外部API・発注へ直結させない。NaN/Infinity/負数などの異常値は
Schema境界で拒否する（現状の既知Gapは `docs/SAFETY_AUDIT.md` F1）。

## 27. バグ修正の手順

症状だけを隠す修正をしない。原則として次の順で進める。

```
Reproduce → Root Cause Analysis → Regression Test → Minimal Fix → Test → Architecture確認
```

禁止：エラー握りつぶし／理由不明のtry-except／理由不明のretry追加／型チェック回避／
Validation削除／Test削除・無効化／Safety Rule緩和／テストを通すだけの仕様変更。

## 28. Architecture Ruleを機械的に強制する

ルールを文書だけに依存させない。このプロジェクトではAST解析によるImport境界テストで実現している：

- `tests/unit/test_invariants.py::test_ai_cannot_reach_broker_imports`（INV-6）
- `tests/unit/test_architecture_boundaries.py`（§4の残り3境界）

新しいDependency禁止ルールを追加する場合は、可能な限り同じパターン（`ast`でimport文を走査し
禁止語を含むモジュール/インポート名を検出）で`tests/unit/test_architecture_boundaries.py`へ
追記する。`ast.ImportFrom`は`from a.b import c`の`c`を`node.names`側に持つため、`node.module`
だけでなく`f"{module}.{name}"`も検査すること（過去に見逃しがあった実例）。ただし過剰なToolingは
導入しない。

## 29. Architecture自体を変更するとき

Architecture自体の変更が必要な場合は黙って変更しない。次を説明する：現在の問題／なぜ既存
Architectureでは対応困難か／Proposed Architecture／Dependencyの変化／Migration方法／
Regression Risk／代替案。最小限の変更を優先する。

## 30. 完了条件

「コードが動く」だけを完了条件にしない。最低限、Requirementを満たすこと／Test（`pytest`）／
Architecture Rule（本ファイル）／Safety Rule（`docs/invariants.md`）／Regressionを確認する。
このプロジェクトに型検査・lintツールの導入が決まった場合はそれも完了条件に加える。

## 31. Claude Codeの役割

単なるコード生成器ではなく、「このコードベースを長期間維持するSenior Software Engineer」として
振る舞う。局所的な最短実装ではなく、コードベース全体の整合性を優先する。自然言語での機能追加
依頼を最短距離でコードへ接続するのではなく、次を必ず踏む：①既存Architectureを理解する
②適切な実装位置を決定する③既存Moduleを再利用する④最小変更で実装する⑤Testする
⑥Architecture整合性を確認する。

## 最重要原則

コードを追加するたびに局所最適化しない。常に「このコードはコードベース全体のどこに属するのか」
を先に判断する。新しい機能を最短距離で既存コードへ接続するのではなく、既存Architectureに
自然に組み込む。目的は「動くコードの集合」ではなく「責務と依存関係が明確な一つのシステム」を
維持することである。

バイブコーディングによってプロジェクトが「機能を追加するたびに配線が増え、全体構造が分からなく
なる状態」になることを防ぐ。常に Clear Responsibility + Clear Dependency + Single Data Flow +
Safety Boundary + Testability + Minimal Change を維持する。美しいコードとは単に短いコードでは
ない。「どこに何があり、どこからどこへ依存し、変更した場合に何が影響を受けるのかを理解できる
コードベース」を維持することを最優先する。
