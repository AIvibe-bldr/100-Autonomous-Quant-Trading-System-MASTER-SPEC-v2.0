# MASTER SPEC v2.0 — Requirement Map

ソース: `100× Autonomous Quant Trading System MASTER SPEC v2.0` (PDF, 111 sections)

## Mission
初期資金 ¥100,000 で米国株現物の自動売買を行う。Stretch Goal は 1年で ¥10,000,000（100×）だが、
100倍を保証するシステムではなく、本質は **Edge の発見・検証・安全な執行・追跡・自己改善が可能な
Quant Research & Trading Platform** の構築である。

## Optimization Objective (§1)
Return 単独最大化は禁止。評価軸: Expected Return / Expected Log Growth / P(100×) /
Drawdown / Ruin probability / OOS robustness / Transaction cost / Liquidity / Operating cost。
100×のために安全制約を解除してはならない。

## Requirement Map（仕様セクション → 実装モジュール）

| 仕様§ | 要件 | 実装場所 | Phase |
|---|---|---|---|
| 2 | Immutable Safety Rules（レバレッジ/信用/空売り/Martingale等の禁止） | `packages/common/safety.py` + invariantテスト | 0 |
| 3-5 | Capital定義 / Challenge終了条件 / High-Water Mark | `packages/common/ledger.py` | 1 |
| 6-7 | Core Architecture / AIのBroker直接アクセス禁止 | パイプライン全体構成 | 0 |
| 8 | Market Data Service | `services/market-data` | 1 |
| 9 | Unified Clock（4 timestamps, UTC） | `packages/common/clock.py` | 1 |
| 10 | Point-in-Time Data | `packages/common/pit_store.py` | 1 |
| 11 | Data Integrity Engine | `services/data-validation` | 1 |
| 12 | Universe Manager（Survivorship Bias防止） | `services/market-data/universe.py` | 1 |
| 13 | Trading Calendar（US Regular、ハードコード禁止） | `packages/common/calendar.py` | 1 |
| 14 | Settlement Manager（Cash Account） | `packages/common/settlement.py` | 1 |
| 15 | Corporate Action Engine | `services/market-data/corporate_actions.py` | 5 |
| 16 | Event Calendar | `services/market-data/event_calendar.py` | 6 |
| 17-19 | News Engine / Source Hierarchy / Prompt-Injection防御 | `services/news` | 6 |
| 20 | Institutional Flow Engine | `services/institutional` | 6 |
| 21 | Quant Scanner（LLM前段のPythonスキャン） | `services/quant` | 5 |
| 22 | Feature Store（value/timestamp/source/versions） | `services/feature-manager/store.py` | 7 |
| 23-24 | Alpha Factory / Anti-Overfitting | `services/alpha-factory` | 7 |
| 25 | Backtest Execution Simulator | `services/quant/backtest.py` | 5 |
| 26 | Market Regime Engine | `services/regime` | 7 |
| 27-28 | Decision AI（Broker権限なし、JSON Schema固定出力） | `services/decision` | 8 |
| 29-31 | Skeptic AI / Disagreement / Confidence Calibration | `services/decision` | 8 |
| 32 | Forecast Tracker | `services/pdca/forecast_tracker.py` | 10 |
| 33-34 | Loss Control Engine / Stop Types（Entryより先に設計） | `services/loss-control` | 3 |
| 35 | Position Sizing Engine（AIが自由に決めない） | `services/position-sizing` | 3 |
| 36-37 | Capital Allocation（Total Exposure<=100%）/ No Martingale | `services/capital-allocation` | 3 |
| 38 | Correlation / Factor Risk | `services/risk/correlation.py` | 3 |
| 39 | Drawdown Throttling（AI変更不可の閾値） | `services/risk/throttle.py` | 3 |
| 40 | Gap / Overnight Risk | `services/risk/gap_risk.py` | 3 |
| 41 | Liquidity / Capacity Engine | `services/risk/liquidity.py` | 3 |
| 42-43 | Master Risk Controller（deterministic・AI禁止）/ MASTER STOP semantics | `services/risk` | 3 |
| 44-47 | Execution Engine（deterministic）/ Order State Machine / Idempotency / Stale Order | `services/execution` | 2,4 |
| 48-49 | Broker Reconciliation / Single Source of Truth | `services/reconciliation` | 2 |
| 50-53 | Post-Stop / Post-Profit / Exit Optimizer / Counterfactual | `services/pdca` | 10 |
| 54-55 | P&L Attribution / Profit Quality Score | `services/pdca/attribution.py` | 10 |
| 56-59 | Shadow Portfolio / Ablation / Champion-Challenger / Feature Lifecycle | `services/feature-manager` | 11 |
| 60-61 | Builder/Judge/Pruner/Revival/Manager / Drift監視 | `services/alpha-factory`, `services/feature-manager` | 7,11 |
| 62 | Replay Engine | `tests/replay` + `services/quant/replay.py` | 5 |
| 63-65 | Daily/Weekly/Monthly PDCA | `services/pdca` | 10 |
| 66-69 | System Supervisor / Heartbeat / Recovery | `services/supervisor` | 9 |
| 70-71 | Red Team / Chaos / Incident Postmortem | `tests/chaos`, `services/supervisor` | 9 |
| 72-74 | Security Boundary / Paper-Live分離 / AI Output Security | `packages/common/security.py` | 0,9 |
| 75 | Decision Provenance（完全再現） | `packages/common/provenance.py` | 4 |
| 76-79 | Human Intent Analysis / Manual UI / Override / Human vs AI | `apps/api` | 13 |
| 80-83 | Operating Cost / Two P&L / Data ROI / Budget | `services/cost-manager` | 12 |
| 84 | Capital Scaling | `services/risk/liquidity.py` | 12 |
| 85 | Accounting Ledger | `packages/common/ledger.py` | 1 |
| 86-97 | UI（Simple Mode / Chart / Holdings / Theme / News Top3 / Feature Center / Health） | `apps/web`, `apps/api` | 13 |
| 98-99 | Experiment Registry / Version Everything | `packages/common/versioning.py` | 0 |
| 100-101 | Technology / Repository構成 | 本リポジトリ | 0 |
| 102 | Executable Invariants | `tests/unit/test_invariants.py` | 0 |
| 103 | Development Phases | `docs/phases.md` | 0 |
| 104-106 | Live Capital Ramp / Paper→Live Gate / Live Deployment Rule | `docs/paper_live_gate.md` | 14,15 |
| 107 | V1 Success Definition | E2E Paperフロー完走＋監査 | 4 |
| 108-110 | Claude役割 / First Task / Conflict指摘 | `docs/` 一式 | 0 |

## V1 スコープ（本実装）
仕様 §103 Phase 0〜4 ＋ Phase 5 の一部（Quant Scanner）を実装:

1. **Phase 0**: アーキテクチャ / スキーマ / 不変条件 / セキュリティ境界 / リポジトリ
2. **Phase 1**: Clock / Calendar / Ledger / Settlement / Market Data / Validation / Universe
3. **Phase 2**: Broker Adapter / Paper Account / Order State Machine / Reconciliation
4. **Phase 3**: Loss Control / Position Sizing / Capital Allocation / Master Risk
5. **Phase 4**: Paper Execution end-to-end
6. **Phase 5(一部)**: Quant Scanner（Python前段）

Decision AI / News / Institutional 等の LLM 依存部分は **インターフェースのみ** 定義し、
決定論的な Mock 実装を同梱する（§27 の Decision AI は外部モデルを想定するため）。

## ADDENDUM v2.1 — Decision Quality / Pre-Trade Independent Audit

2026-08 追加指示による正式仕様。既存機能への**統合**であり、新規系統の乱立はしない。

| 追加§ | 要件 | 統合先 | 実装場所 |
|---|---|---|---|
| A1 | Decision Quality Engine（全判断種別の Immutable Snapshot・追跡・Outcome/Process分離採点） | 既存 Post-Stop/Post-Profit/Counterfactual Tracker（§50-53）と Forecast Tracker（§32）を包含する上位機構 | `services/pdca/decision_quality.py` |
| A2 | Monthly Decision Quality Report（種別/Confidence/Regime別、EV/MAE/MFE、月別Trend） | Daily/Weekly/Monthly PDCA（§63-65）の月次集計に統合 | `services/pdca/decision_quality.py` |
| A3 | Pre-Trade Independent Audit AI（意味的監査、PASS/REJECT/REVIEW） | Skeptic AI（§29）とは別役割（Skeptic=判断段階の反証、Audit=発注直前の整合性）。最終防壁は従来通り deterministic Master Risk Controller（§42） | `services/decision/audit.py` |
| A4 | Immutable Approved Order Snapshot（hash固定・変更時は再Audit+再Risk） | 既存 RiskApproval HMAC署名（§42実装）の拡張。Execution Engine は hash 一致注文のみ送信 | `packages/schemas/audit.py`, `services/execution/engine.py` |
| A5 | Pre-Trade Audit Log（Decision→Audit→Risk→Snapshot→Broker→Fill の完全記録） | Decision Provenance（§75）の発注側拡張 | `services/pdca/audit_log.py` |
| A6 | Near-Miss学習（Audit/Risk REJECTを防止済み誤発注として集計） | Incident Postmortem（§71）/ Counterfactual（§53）に統合 | `services/pdca/audit_log.py` |
| A7 | UI: 判断品質パネル + 発注安全性パネル | 既存 Dashboard（§86-97）へ追加 | `apps/api`, `apps/web` |
| A8 | 自動テスト（BUY/SELL不一致・Hash mismatch・Audit不能時等） | Executable Invariants（§102）へ追加（INV-16〜20） | `tests/unit/test_pretrade_audit.py` ほか |

### 正式発注パイプライン（A3-1、§7を置換更新）

```
Decision AI → Structured Trade Proposal → Loss/Exit Plan → Position Sizing
→ Independent Audit AI → Master Risk Controller（deterministic・最終防壁）
→ Immutable Approved Order Snapshot（hash固定）→ Execution Engine → Broker
```

- Decision AI / Audit AI とも Broker 直接アクセス禁止
- 誤発注防止の**最終責任はAIに持たせない**: Cash/Leverage/Exposure/Duplicate/Session/
  Stop存在/Reconciliation/Data health 等は Master Risk Controller の固定コードで確認（A3-4）
- Audit AI は Decision と Order Intent の**意味的整合**（方向・数量・Stop・Horizon・Thesis）を監査
- Risk承認後に重要Field（symbol/side/qty/price/stop）が変われば Approval 無効化 → 再Audit+再Risk（A4）
- Execution Engine は承認済み Snapshot Hash と一致する注文のみ送信し、数量等を自ら変更しない（A4-2）

### Decision Quality の評価原則（A1-5）

- **Outcome Score**（その後の値動き）と **Process Score**（当時の情報で判断プロセスが妥当だったか）を分離
- ルール遵守の NO TRADE が結果的に上昇 → Outcome=MISSED_OPPORTUNITY / Process=GOOD
- ルール違反の BUY が偶然利益 → Outcome=GOOD / Process=BAD（**偶然の利益を学習させない**）
- 正解率のみを KPI にしない: EV per Decision / Average GOOD Return / Average BAD Loss / MAE / MFE を併記
- GOOD率の月次上昇を「AI自身の進化」と即断せず Regime 変化を併せて分析（A2-5）

## 仕様上の指摘事項（§110 に基づく ISSUE 提示）

### ISSUE-1: 通貨単位の混在
- **ISSUE**: 資本は円建て（¥100,000）だが対象は米国株（USD建て）。仕様は FX 換算・FX Settlement に言及するが基準通貨を明示しない。
- **WHY**: P&L・Exposure・Risk Budget 計算に基準通貨が必須。
- **RISK**: 通貨換算の曖昧さによる Risk 計算誤り。
- **PROPOSED FIX**: V1 は内部会計を USD で行い、初期資金は開始時レートで USD 換算して固定記録。表示層で JPY 換算。
- **AFFECTED**: Ledger / Settlement / UI。

### ISSUE-2: Cash Account の T+1 決済と回転売買
- **ISSUE**: Cash Account 前提（§14）で未決済資金の再利用（Good Faith Violation）の扱いが未定義。
- **WHY**: 米国 Cash Account では未決済資金での売買にペナルティがある。
- **RISK**: Broker 制裁 / 口座凍結。
- **PROPOSED FIX**: Settlement Manager が Settled Cash のみを Buying Power として Master Risk Controller に渡す（実装済み、安全側）。
- **AFFECTED**: Settlement / Risk / Execution。

### ISSUE-3: 100×目標と Leverage 1.0 制約
- **ISSUE**: 現物・レバ無しで年間100×は極めて低確率。仕様自身が「保証しない」と明記しているため矛盾ではないが、目標未達を理由とする Risk 増加の誘因になり得る。
- **WHY**: §63/§65 が明示的に「未達だから Risk を上げる」を禁止している。
- **RISK**: 目標progress表示が Risk-taking バイアスを生む。
- **PROPOSED FIX**: 進捗表示は参考値とし、Risk 計算系から完全に分離（実装済み: PDCA は Risk 入力を持たない）。
- **AFFECTED**: PDCA / UI / Risk。

## 実装状況の監査（2026-11-07）

要件表の各行を実コードと突き合わせた結果。判定方法は、(1) 記載パスの存在確認、(2) `ast` による
import 解析（他の本番コードから参照されるか／テスト・スクリプトからのみか）、(3) `grep` による
リテラル値・未配線の確認。**動作の正しさそのものの保証ではない**（テストの範囲は別）。

### A. 要件表のパス訂正（記載のファイルが存在しない）

| 仕様§ | 記載 | 実体 |
|---|---|---|
| 2 | `packages/common/safety.py` | 専用ファイルなし。不変条件は `packages/common/risk_config.py`（`__post_init__`）＋ `tests/unit/test_invariants.py` |
| 32 | `services/pdca/forecast_tracker.py` | `services/decision/forecast.py` |
| 38 | `services/risk/correlation.py` | `services/capital_cells/correlation.py`、`services/position_sizing/engine.py`、`services/risk/master_controller.py` |
| 39 | `services/risk/throttle.py` | `packages/common/risk_config.py`（`throttle_level` / `throttle_factor`） |
| 41, 84 | `services/risk/liquidity.py` | `services/risk/master_controller.py`、`services/position_sizing/engine.py`、`services/capital_cells/capacity_tail_risk.py` |
| 72-74 | `packages/common/security.py` | 専用ファイルなし。HMAC署名＝`services/risk/master_controller.py`、未信頼テキスト＝`services/decision/models.py`（`UntrustedText`） |
| 98-99 | `packages/common/versioning.py` | 専用モジュールなし。version は各スキーマ／ログの列として保持（`packages/common/experiments.py` 等） |
| 103 | `docs/phases.md` | 未作成 |
| 104-106 | `docs/paper_live_gate.md` | **案（未承認）**。LIVEゲート／環境紐付けはコード済み。移行基準の数値は運用者の判断待ち |

### B. 実装済みだがライブのパイプラインに配線されていないもの

テストまたはスクリプトからのみ使われ、`run_session` の流れには入っていない。

| 仕様§ | モジュール | 影響 |
|---|---|---|
| 17-19 | `services/news` | ~~Decision AI にニュースが渡らない~~ → 2026-11-07 から接続（下記「ニュース接続」）。実行は `--real-data` のデモのみ |
| 20 | `services/institutional` | 同上（機関投資家フロー未使用） |
| 16 | `services/market_data/event_calendar.py` | ~~未接続~~ → 2026-11-07 から接続（下記「イベントカレンダー接続」）。決算日は EDGAR による推定のみ（下記「SEC EDGAR 接続」） |
| 15 | `services/market_data/corporate_actions.py` | 価格調整は不要（Alpaca の日足は `adjustment=all` で調整済み）。今後の分割・配当等は上記イベントカレンダー経由で接続済み。エンジン自体は未使用のまま |
| 10 | `packages/common/pit_store.py` | Point-in-Time ストア未使用 |
| 14 | `packages/common/settlement.py` | 決済はブローカー報告（`get_cash().settled_cash`）を権威として使用。Settlement Manager 自体は未使用 |
| 23-24, 56-61 | `services/alpha_factory`, `pdca/shadow.py` ほか | 推奨のみで設計上、Risk へ書き込まない（意図的）。ただし本番フローには未接続 |
| 30 | `services/decision/disagreement.py` | Skeptic/Decision の不一致集計が未使用 |
| 32 | `services/decision/forecast.py` | Forecast Tracker 未接続 |
| 50-55 | `pdca/attribution.py`, `exit_optimizer.py` | 損益分解・出口最適化が未接続（`review.py` はスクリプトのみ） |
| 68 | `services/supervisor/recovery.py` | 復旧処理が未接続 |
| 76-79 | `services/decision/human_intent.py` | 人の意図解析が未接続 |
| 48-49 | `services/reconciliation/engine.py` | ~~起動時のみ~~ → 2026-11-07 から毎セッション `run_session` 冒頭で実行（配線済み） |
| — | `services/capital_cells/*` | 資本セル設計（`docs/capital_cell_architecture.md`）。ライブには未接続 |

### C. パイプライン内のリテラル値 — 解消（2026-11-07）

| 箇所 | 当初の問題 | 対応 |
|---|---|---|
| `reconciliation_ok=True`（Risk View） | 監査証跡が常に PASS。セッション中の定期照合なし | `run_session` 冒頭（約定同期の直後）で `ReconciliationEngine.reconcile()` を毎回実行し、結果を Risk View に反映。不一致なら `HALT_NEW_ENTRIES` のうえ、Decision AI を呼ばずに終了（LLM費用を使わない）。照合前は `False`（未検証を一致と見なさない） |
| `correlation_to_book=0.0` | §38 の相関ペナルティが発動しない | `services/position_sizing/correlation.py`（純関数）で候補と保有銘柄の日次リターンの最大絶対相関を計算し、サイジングと Risk View に渡す。測定不能な保有銘柄（データ不足・系列が平坦）は 1.0（分散効果なし）として扱う |

### D. 実運用（LIVE）に向けて未着手

- 実ブローカーアダプタ（現状 `PaperBroker` のみ。接続前に解決すべき課題 T1〜T8 は `docs/paper_live_gate.md` §2）。LIVEゲートは実装済み（2026-11-07）。Paper→Live 基準は案（`docs/paper_live_gate.md`、運用者の承認待ち）
- ~~緊急停止（F5）~~ 完了（ファイル方式、`scripts/kill_switch.py`）。異常スリッページ・ハートビート異常のアラート（F6/F10 の残り）は未着手
- 複数日のペーパー成績の記録・モデル比較（永続化は注文／監査ログのみ）

### 優先して直すべき順（LLM不要）

1. ~~C の `reconciliation_ok` の実値化と毎セッション照合~~ 完了。
2. ~~C の `correlation_to_book` の計算~~ 完了（`capital_cells` の相関機構は設計が別系統のため再利用せず、純関数を新設）。
3. B のうち Decision の質に直結する news / event_calendar / corporate_actions の接続（LLM入力が変わるため、実行前に §23 報告）。
4. D（LIVE準備）。判断事項は F5 の方式と Paper→Live 基準。

### ニュース接続（2026-11-07）

- 取得元: `services/news/alpaca_news.py`（Alpaca ニュース API、市場データと同じ `APCA_*` キー。データのみ）。HTTP は注入可能でテストはオフライン。
- 流れ: 取得（セッションごとに1回、全候補をまとめて）→ `NewsEngine`（記事ごとに処理。重複排除・信頼度・プロンプトインジェクション検出）→ `services/news/selection.py`（銘柄ごとに選別）→ `DecisionContext.news`（`UntrustedText`）→ プロンプト。
- 選別: 注入疑いは隔離（件数を `PipelineResult.data_warnings` に出力）／7銘柄以上にタグ付けされた記事は除外／タグ数が少ない記事ほど優先／最大3件・本文500文字。
- 先読み防止: 決定時刻より後に公開された記事は渡さない（取得元とエンジン入力の両方）。
- 出典の格付け: `benzinga` のみ MAJOR_MEDIA。未分類の出典は SNS（最低位）。
- 失敗時: セッションは止めず、プロンプトに「News: not available」と明示（「ニュースなし」とは別の文言）。`data_warnings` に記録。
- 追加した防御: 記事本文に `</untrusted_external_data>` 等があっても枠を破れないよう `< > "` を無害化。
- 既知の限界: `NewsEngine` のクラスタリング（銘柄の組＋日付）は粗く、パイプラインでは記事ごとに処理して回避している。方向性（direction）・影響度（impact）はキーワードによる粗い推定で、Decision AI には渡していない（見出し・要約・出典・時刻のみ）。Alpaca ニュースには集約サイトの再掲が混ざり、出典の信頼度は粗い。ニュースは助言的入力であり、売買の可否は引き続き Risk 側が決める。

### イベントカレンダー接続（2026-11-07）

- 取得元: `services/market_data/alpaca_events.py`（Alpaca corporate-actions API、市場データと同じ `APCA_*` キー、データのみ）。配当落ち日・分割・併合・スピンオフを `EventCalendar` の `RiskEvent` に変換（`EX_DIVIDEND` / `STOCK_SPLIT` / `MERGER` / `COMPANY_EVENT`）。
- 流れ: セッションごとに1回、候補全銘柄を取得 → `EventCalendar` → ①`DecisionContext.upcoming_events`（今後14日、プロンプトに表示）、②`gap_risk_score(has_event_before_horizon=…)`（今後7日に該当イベントがあればスコア +0.35、上限1.0。従来はこの入力がパイプラインから一度も渡されていなかった）。
- `EX_DIVIDEND` は情報表示のみでギャップリスクには数えない（落ち幅が配当額で予測可能なため）。分割・併合などは数える。
- 失敗時: セッションは止めず、プロンプトに「Upcoming events: not available」と明示（「イベントなし」とは別の文言）、`data_warnings` に記録。
- **決算日は取得元が無い。** プロンプトは常に「決算日はこのカレンダーの対象外で、不明は“近くに無い”の証拠ではない」と明記する。決算日の取得は EDGAR（過去の 8-K Item 2.02 からの推定）または別プロバイダの契約が必要で、後者は判断事項。
- 接続範囲: `--real-data` のデモのみ（ニュースと同じ）。

### SEC EDGAR 接続（2026-11-07）

- 認証: APIキーは不要。SEC のルールで全リクエストに「アプリ名＋連絡先メール」の `User-Agent` が必須（毎秒10リクエスト上限、実装は約6.6/秒に抑制）。**環境変数 `QUANT_SEC_USER_AGENT`（例: `QuantPaper you@your-domain.com`）で運用者が設定する。** コードへの埋め込み・既定値・補完はしない。未設定／形式不正なら EDGAR は無効（フェイルクローズ。他の取得元で続行）。
- `services/market_data/edgar_client.py`: ティッカー→CIK、提出一覧（キャッシュ付き）。失敗は `EdgarUnavailable`。
- `services/news/edgar_news.py`: **8-K** を `SEC_PRIMARY`（§18 最上位）のニュースに。SEC 索引の事実（様式・受理時刻・項目番号）のみを自前の項目名表で文章化し、提出者が書いた本文は取得も受け渡しもしない（注入経路なし）。決定時刻より後の提出は除外。
- `services/market_data/edgar_earnings.py`: 過去の 8-K **Item 2.02**（決算発表）の提出日の364日後を**次回決算日の推定**として `EARNINGS` イベント化（`uncertainty_days=7`、説明文に「estimated」と明記）。`EventCalendar` は不確実性の幅だけ判定窓を広げる（ギャップリスクは保守側）。
- 合成: `CompositeNewsSource` / `CompositeEventSource`。1つが失敗しても他は使い、`PipelineResult.data_warnings` に「partial … failure」を出す。全滅した場合のみ「未取得」扱い。
- プロンプト: 決算日は「推定のみ。無い＝近くに無い、ではない」と常に明記。
- 配線範囲: `--real-data` のデモのみ。確認用: `QUANT_SEC_USER_AGENT=... python3 scripts/check_edgar.py`（LLM不使用、実際の応答形式の確認）。
- **実サービス確認済み（2026-11-07）**: `check_edgar.py` で `company_tickers.json` / `submissions` / Form 4 XML の形が想定どおりであることを確認。
- **決算日推定の四半期フォールバック（2026-11-07）**: 8-K 履歴が約1年未満（`past[0] > start-300日`）の銘柄は「364日前」が存在しない。例: XOM は 2026-07 の持株会社再編で CIK が新規になり履歴が約4か月、JPM は提出が2.6万件で `recent` が約1年に切り詰められる。この場合は直近の Item 2.02 + 91日を推定とし、`uncertainty_days=14`（通常7）、説明文に方法を明記。年次決算（Q4）は四半期より遅れる傾向があり、14日で吸収しきれない場合がある（推定である点は変わらない）。再編前の旧CIKの履歴は連結していない。
- **Form 4（インサイダー取引）**: `services/news/edgar_insider.py`。`SEC_PRIMARY` ニュース。XML の構造化項目（取引コード・株数・価格・取得/処分フラグ・役員/取締役/10%保有者フラグ・10b5-1 計画フラグ）のみ読み、氏名・役職名・脚注・自由記述は読まず渡さない（注入経路なし）。**非デリバティブの公開市場買い(P)・売り(S)のみ**を事実として文章化し、付与・権利行使・税源泉・贈与・オプション等は捨てる。価格不明／非有限／負数の取引は採用しない。DOCTYPE/ENTITY 宣言のある XML、256KB超は拒否。銘柄あたり最大8件/回（SEC リクエスト抑制）。独立したニュース源なので Form 4 の失敗が 8-K を巻き込まない。
- **Form 4 の限界**: 売りの多くは 10b5-1 計画に基づく定期売却で（AAPL 役員の実例）、方向性のシグナルとしては弱い。本文では計画フラグを事実として併記するのみで、方向/影響度の推定はしない。訂正報告 4/A、Form 3/5、デリバティブ表は対象外。判断への寄与は単日では検証できない。

### Audit AI への気配の受け渡し（2026-11-07）

- `AuditContext` に `quote`（監査直前に取得した気配。取得不能なら `None`）と `quote_as_of`（その経過時間を測る決定時刻。Decision と同じ `_decision_time` で気配時刻ずれを補正）を追加。`services/pipeline.py` が発注ごとに `market_data.quote` を取得して渡す。
- 描画は Decision と同一の `render_quote_line`（`services/decision/prompts.py`、単一の実装）。Audit のプロンプトには、気配（bid/ask/サイズ/スプレッド/経過秒）に加え、**決定時の想定エントリー価格（ask）と現在 ask の乖離を私たちが計算して**記載する（LLM に算術をさせない）。MARKET 買いは ask 付近で約定する旨も併記。
- システムプロンプト：気配の欠落・古さ・乖離は「判断材料」であり、気配が無いこと自体は拒否理由にならない、と明記。
- **限界**: モックの監査（`MockAuditModel`）は気配を使わない（決定的ルールを増やしていない）。乖離の定量ルール（例: 想定価格から X% 超で REVIEW）は閾値の判断が要るため未実装。Skeptic への気配は対象外。実LLMでの指摘内容の変化は未検証（次回の実行で確認）。
