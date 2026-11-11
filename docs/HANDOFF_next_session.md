# 引継ぎ指示書（次のセッション用）

作成: 2026-11-11 / ブランチ: `claude/trusting-allen-7wp8ha`（最新コミットは `git log -1` で確認）
対象: 自律型クオンツ売買システム（**ペーパー専用**）。ユーザーは日本語。1回の「進めて」＝実装・テスト・コミット・push。

## 0. 守ること（常時）
- 発注は **PaperBroker のみ**。実ブローカーへは何も送らない。
- **LLM の実行は、ユーザーが明示的に指示したときだけ**。上限は既定 $1.00/実行（`QUANT_LLM_BUDGET_USD` は未設定のまま。自分で上げない）。
  - 注意: `scripts/run_llm_paper_demo.py` は環境に Anthropic のキーがあると、`--real-data` なしでも**実LLMを呼ぶ**。確認用に動かすときは `env -u MY_ANTHROPIC_API_KEY -u ANTHROPIC_API_KEY -u OPENAI_API_KEY HOME=/nonexistent …` でキーを外す（前回、これを忘れて意図せず実LLMが起動した。費用は未確認）。
- **PR はユーザーの指示があるまで作らない**（PR #4 が `main` 向けに開いている。本文は永続化・UI・データ品質の追加分が未反映）。
- 秘密の値は出力しない（有無だけ確認: `[ -n "${VAR}" ] && echo set`）。キーをチャットに貼らせない。
- `CLAUDE.md` に従う（§23 実装前報告、§24 実装後報告、§29 設計変更の説明、1コミット1責務）。python は `/usr/bin/python3`。
- コミットは「テスト全件通過」を確認してから（`pytest -q` が約15秒）。`;` でなく `&&` で繋ぐ。

## 1. 最初にやること
1. `git status -sb` と `/usr/bin/python3 -m pytest -q`（全件通過を確認）。
2. **21st.dev の MCP が見えるか確認**（ユーザーが `API_KEY_21ST` を登録済み。UI UX Pro Max のスキルは取り込み済み）。
   - 見えれば: コンポーネント探索を開始（対象: ゲージ行・盤のウィンドウ・詳細シート）。外部部品は**そのままコピーせず** `docs/DESIGN_SYSTEM.md` に合わせて作り直す。
   - 見えなければ: ①`.mcp.json` のヘッダー（今は `x-api-key: ${API_KEY_21ST}`。以前は Bearer だった）②環境変数が入っているか（値は出さず有無だけ）③`curl` で `https://21st.dev/api/mcp` の到達性、の順で切り分ける。キー発行先は https://21st.dev/mcp（旧Magicの鍵は失効済み）。
3. ユーザーに「21st.dev のツールは見えます」と報告してから、次の作業を確認する。

## 2. 現在の状態（要点）
### 実装済み・push済み
- **永続化（A-1）**: 口座と台帳を整合点で保存し、検証つきで復元（`--resume` / `--resume-if-exists` / `--clear-halt --operator --reason`）。HALT は再起動後も残る。
- **運用基盤（A-3/A-4）**: 通知（Webhook/メール/履歴ファイル）、ランナー状態ファイル、外部ウォッチドッグ、読み取り専用の運用ダッシュボード（別プロセス）。手順は `docs/operations.md`。
- **ダッシュボードの再設計**: 方向2＋3の併用。タブ＝今日／銘柄（`#all`）／詳細（`#full`）。
  - 今日: 資産・状態の一文・「気にしてほしい銘柄」（損切り〜目標のゲージ行）・推移・直近にしたこと・今日の流れ。
  - 銘柄: 全銘柄を**同じ大きさのウィンドウ**で並べる「盤」（緑＝利益、赤＝損失）と、ゲージ付きの「表」を切替。並べ替え・系統フィルタ。
  - 銘柄を押すと詳細シート（なぜ持っているか・損切り・目標・保有の目安・やめる条件）。
  - 表示は `textContent` のみ、外部読み込みなし、読み取り専用（`tests/unit/test_ops_dashboard.py` が機械的に確認）。
  - 設計は `docs/DESIGN_SYSTEM.md`、比較検討は `docs/ui_directions/`。画面見本は `docs/ui_mockups/`。
- **銘柄の会社名・系統・事業説明**: 手書きの表 `packages/common/symbol_profiles.py`（20銘柄、日本語名つき）→ 表にない銘柄は SEC の会社記録から英語名＋業種を**自動取得**（`services/market_data/edgar_company.py`、`--real-data` のときのみ）。自動取得は「事業内容の説明ではありません」と明記。AIによる翻訳・要約は費用が出るので入れていない。
- **データの質**: 日足を SIP（全取引所合算）に変更（`QUANT_ALPACA_BARS_FEED`、既定 `sip`、拒否されたら IEX に自動で戻り注意書き）。取得範囲の終わりを前日にしている（当日を含むと無料プランは403）。**気配は無料プランでは IEX のみ**で、見かけのスプレッドが広く出る（JPM 6〜10%など）→ スプレッド上限は緩めていない。
- `scripts/run_llm_paper_demo.py`: データ・安全状態・各観測者の初回出力を表示するようにした。
- UI UX Pro Max スキル（`.claude/skills/ui-ux-pro-max/`）と `.mcp.json`（21st.dev）を `brave-carson` ブランチから取り込み済み。`CLAUDE.md` に §32 UI/UX ルールを追記。

### 実LLM実行（月曜 2026-11-09、1回のみ）
- 結果: スキャン20 → 候補4 → Decision 1（PFE を BUY）→ Skeptic が拒否 → **発注なし**。費用 $0.133（上限 $1.00）。
- スプレッド除外: AAPL 1.15% / CVX 1.04% / JPM 6.73%（IEX 気配の見かけの広がりの可能性）。
- Skeptic はニュース・13G・決算カレンダー・時刻を踏まえて拒否。決算日の扱いは課題として残る。
- 1日分では優劣・利益の根拠にならない。旧予約 `trig_0111…` は無効化済み。残っている予約はない。

## 3. 未決・待ち（ユーザーの判断／外部）
| 項目 | 状態 |
|---|---|
| Alpaca サポートの返答 | 待ち（口座の適格性・現金口座）。返答後に本物のブローカー・アダプタ（既定は dry-run）へ |
| 有料のデータプラン | 未判断（最新の SIP 気配が取れる＝スプレッド判定が正確になる）。費用の判断 |
| Paper→Live の基準数値 | `docs/paper_live_gate.md` は**草案** |
| 現金口座の前提 | 未確認 |
| 通知の宛先 | 未指定 |
| 資本セル B/C（サイジング・配分に効く） | 未承認（A は観測のみで稼働中） |
| Audit の drift しきい値 / CalibrationTracker のフィードバック（サイジングに効く） | 未判断 |
| LLM の1日あたり予算（A-2） | 未判断（上限は現在プロセス単位） |
| PR #4 の本文更新・マージ | 指示待ち。差分が大きい（スキルのデータ約5万行を含む。レビューでは `.claude/skills/ui-ux-pro-max/` を外部由来として除外して見る） |

## 4. 既知の限界（報告を続けること）
- 実LLMの効果は1日分しか見ていない（検証不足）。通知の到達はローカルサーバーでしか試していない。systemd ユニットは未検証。
- 観測者の累積統計は再開すると初期化される。永続化は PaperBroker のみ。
- ダッシュボードの値は**直近セッション時点**でリアルタイムではない。
- 変換表（業種→日本語の系統）は主要な約100コード。足りないものは大分類か英語になる。
- 取引カレンダーは 2025〜2028 のみ（2029 以降は失敗側＝停止）。
- 意図せず1回起動した実LLMの費用は未確認（数セント程度の見込み）。

## 5. よく使うコマンド
```bash
/usr/bin/python3 -m pytest -q                               # 全テスト（約15秒）
/usr/bin/python3 scripts/run_paper_runner.py --show-schedule  # 次のセッション予定
env -u MY_ANTHROPIC_API_KEY -u ANTHROPIC_API_KEY -u OPENAI_API_KEY HOME=/nonexistent \
  /usr/bin/python3 scripts/run_llm_paper_demo.py --days 1   # モックAIでの確認（LLM費用ゼロ）
# 実LLM: ユーザーが明示したときだけ（--real-data、市場が開いている時間 = 日本時間 23:30〜06:00）
```
ダッシュボードの見た目確認は、Playwright（導入済み）でスクリーンショットを撮る。実データ状態で見たいときは `scripts/run_ops_dashboard.py --state-dir <dir>`。

## 6. 主なファイル
- 画面: `apps/web/ops.html`、API: `apps/api/ops.py`、状態: `services/supervisor/ops_state.py`（`holdings()`、注目順位 `attention_of()`）
- 保有の記録: `services/supervisor/session_record.py`（`holdings_view`、自動プロフィール）
- 会社情報: `packages/common/symbol_profiles.py`、`services/market_data/edgar_company.py`、`edgar_client.py`（`company_info`）
- データ: `services/market_data/alpaca_provider.py`
- 永続化: `services/checkpoint.py`、`packages/common/durable_store.py`
- 規約: `CLAUDE.md`、設計: `docs/DESIGN_SYSTEM.md`、運用: `docs/operations.md`、安全: `docs/invariants.md` / `docs/SAFETY_AUDIT.md`
