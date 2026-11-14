# 引継ぎ指示書（次のセッション用）

作成: 2026-11-11（2回目の更新）/ 基準: `main`（PR #4・#5・#6 をマージ済み。最新のマージコミット `8afaec4`）
対象: 自律型クオンツ売買システム（**ペーパー専用**）。ユーザーは日本語。1回の「進めて」＝実装・テスト・コミット・push。

## 0. 守ること（常時）
- 発注は **PaperBroker のみ**。実ブローカーへは何も送らない。LIVE ゲートの承認ファイルは無い。
- **LLM の実行は、ユーザーが明示的に指示したときだけ**。上限は既定 $1.00/実行（`QUANT_LLM_BUDGET_USD` は未設定のまま。自分で上げない）。
  - 注意: `scripts/run_llm_paper_demo.py` は環境に Anthropic のキーがあると、`--real-data` なしでも**実LLMを呼ぶ**。確認用に動かすときは `env -u MY_ANTHROPIC_API_KEY -u ANTHROPIC_API_KEY -u OPENAI_API_KEY HOME=/nonexistent …` でキーを外す。`scripts/run_paper_runner.py` も同様にキーを外して試す。
- **PR はユーザーの指示があるまで作らない・マージしない**。PR #4・#5・#6 はマージ済みなので、今後の変更は**新しい PR** になる。作業ブランチ `claude/confident-planck-uiiidk` は `main` から作り直した（マージ済みの履歴に積み足さない。ブランチを変えるときも指定の許可を確認する）。
- 秘密の値は出力しない（有無だけ確認: `[ -n "${VAR}" ] && echo set`）。キーをチャットに貼らせない。
- `CLAUDE.md` に従う（§23 実装前報告、§24 実装後報告、§29 設計変更の説明、§32 UI/UX、1コミット1責務）。
- コミットは「テスト全件通過」を確認してから（`pytest -q` が約15秒）。`;` でなく `&&` で繋ぐ。
- **運用者が決めていない数値・方針を、自分で確定しない**（§3）。推奨を出して判断を仰ぐ。

## 1. 最初にやること
1. `git status -sb` と、テストの実行。環境に pytest が無ければ `pip install -e '.[dev,api]'` のあと `python -m pytest -q`（全件通過を確認）。
2. **21st.dev の MCP が見えるか確認**（`API_KEY_21ST` は登録済み。`.mcp.json` はリポジトリにある）。
   - 前回は、`.mcp.json` の無いブランチで始めたときは見えず、取り込んだ後に `mcp__21st__search` などが見えるようになった（原因の切り分けはしていない）。
   - 見えなければ: ①`.mcp.json` のヘッダー（`x-api-key: ${API_KEY_21ST}`）②環境変数の有無（値は出さない）③`curl` で `https://21st.dev/api/mcp` の到達性（前回は HTTP 405 ＝到達はできる）、の順で切り分ける。キー発行先は https://21st.dev/mcp。
   - 見えても、コード取得（`get_component`）は**有料枠を消費する**。必要な1件に絞る。
3. ユーザーに結果を報告してから、次の作業を確認する。

## 2. 現在の状態（要点）
### main に入っているもの（PR #4）
- **永続化（A-1）**: 口座と台帳を整合点で保存し、検証つきで復元（`--resume` / `--resume-if-exists` / `--clear-halt --operator --reason`）。HALT は再起動後も残る。
- **運用基盤**: 通知（Webhook/メール/履歴ファイル）、ランナー状態ファイル、外部ウォッチドッグ、読み取り専用の運用ダッシュボード（別プロセス）。手順は `docs/operations.md`。
- **ダッシュボード**（`apps/web/ops.html`）: タブ＝今日／銘柄（`#all`）／詳細（`#full`）。銘柄の盤（同じ大きさのウィンドウ）と表の切替、ゲージ行、詳細シート。表示は `textContent` のみ・外部読み込みなし・読み取り専用（`tests/unit/test_ops_dashboard.py` が機械的に確認）。設計は `docs/DESIGN_SYSTEM.md`。
- **銘柄の会社名・系統・事業説明**: `packages/common/symbol_profiles.py`（20銘柄）。表にない銘柄は `--real-data` のときだけ SEC から英語名＋業種を自動取得（`services/market_data/edgar_company.py`）。
- **データの質**: 日足は SIP（`QUANT_ALPACA_BARS_FEED`、拒否されたら IEX に自動で戻る）。**気配は無料プランでは IEX のみ**で、見かけのスプレッドが広く出る（JPM 6〜10%）→ スプレッド上限は緩めていない。
- UI UX Pro Max スキル（`.claude/skills/ui-ux-pro-max/`、外部由来・約6.9万行）、`.mcp.json`（21st.dev）、`CLAUDE.md` §32。

### この回（2026-11-11）に追加して main に入ったもの
- **詳細シートのフォーカス**: Tab がシートの外に出ないようにした（Esc で閉じて元の位置へ戻る）。`--muted`（未定義のトークン）を直し、使うトークンが全て定義済みであることをテストで確認。
- **画面の磨き込み**: スマホの「今日の流れ」の切れ、状態文と所見の重複、観測者パネルの余白、約定表の桁。
- **日本語表示**: サービスが英語で書く文（監視の所見・見送り理由・データ警告・日次レビュー・注記）を、表示側の純粋関数 `ja()` で日本語にする。**元の文（通知・CLI・テスト）は変えない**。ルールのない文、AI や例外が書いた自由文はそのまま表示（意味を推測しない）。新しい英語の文がバックエンドに増えたら、`ops.html` の `JA_RULES`（`<ja-translator>` ブロック）にルールを足す。テストは `node` でページ自身の翻訳器を動かす（`node` が無ければスキップ）。
- **systemd の設定例の修正**（`docs/operations.md`）: 行末コメントのせいで `Restart=on-failure` と `RestartPreventExitStatus=2` が無視されていた（`StartLimitIntervalSec` も置き場所違い）。`systemd-analyze verify` が警告なしで通る。runner の SIGTERM 停止（終了コード0）・`--resume-if-exists` での再開・起動拒否（終了コード2）は実際に動かして確認済み。
- **Paper→Live 基準の草案**（`docs/paper_live_gate.md`）: 判断項目ごとに推奨と理由を付けた。**運用者が確定したのは2項目だけ**: 許容ドローダウン＝資本の10%、現金口座・無レバレッジ。
- **21st.dev の探索**: ゲージ・ヒートマップ（treemap）・シート系の部品を検索した。どれも React/Tailwind で、このダッシュボードの制約（1ファイル・外部読み込みなし・`textContent` のみ）に合わないため**コードは取り込んでいない**（既存の部品が `DESIGN_SYSTEM.md` を満たしている）。

- **通知の不具合の修正**（`packages/common/alert_sinks.py`）: Discord への最初の実送信が HTTP 403（Cloudflare エラー 1010）で失敗した。原因は、Python 標準の識別名（`Python-urllib/x.y`）を Discord が拒否すること（ダミーの Webhook で再現: 既定の識別名は 403、独自の識別名は 404 Unknown Webhook）。`User-Agent: quant-alerts/1.0`（連絡先を含まない一般的な名前）を付けて修正、回帰テストあり。**ローカルの模擬サーバーでは見つからず、本物の宛先で初めて分かった**。この修正は PR #5 で `main` に入っている。**運用者の PC の ZIP は修正前のコードなので、`main` の ZIP を取り直す必要がある**。
- **Windows 対応**: 運用者の PC は Windows（PowerShell）で、`git` は未導入、リポジトリは ZIP で展開した。**CI に Windows（Python 3.12）を追加**し、Windows 固有の不具合を2件直した: ①`tzdata` が無く `ZoneInfo("America/New_York")` が import で失敗（pyproject に Windows 限定の依存を追加）②UTF-8 のファイルを既定の cp1252 で読み書きして失敗（本体18か所に `encoding="utf-8"`。ダッシュボードの配信・緊急停止・状態ファイル・障害ログ・人の意図の受付箱・通知の履歴が対象）。AST のテストで、文字コードなしのファイル入出力を今後は拒否する。両 OS の CI が通る。**runner の Windows での停止は実プロセスで確認済み**（Windows の CI が、起動したランナーに Ctrl+Break を送り、1秒ほどで終了コード0・`STOPPED` になることを確認。修正前は `Event.wait(60)` が信号で起こされず58秒かかった → 待ちを1秒ごとに区切って直した。`tests/unit/test_runner_process_stop.py`）。**運用者の Windows PC でも、キーボードの Ctrl+C で約1秒で止まり、`--resume-if-exists` で保存から再開できることを確認済み（2026-11-11、模擬データ）**。**タスクスケジューラでの自動起動、長時間（数日）の稼働は未確認**（`docs/operations.md` の「Windows で動かす場合」）。外部の死活監視の登録手順は同じ手順書に書いた（あなたのアカウントでの登録と、通知の到達は未確認）。この修正と手順書の追記は PR #6 で `main` に入っている。**運用者の PC の ZIP は修正前のコードなので、`main` の最新の ZIP を取り直す必要がある**。Linux の常時稼働機が向く。

### 実LLM実行（月曜 2026-11-09、1回のみ）
- スキャン20 → 候補4 → Decision 1（PFE を BUY）→ Skeptic が拒否 → **発注なし**。費用 $0.133（上限 $1.00）。
- スプレッド除外: AAPL 1.15% / CVX 1.04% / JPM 6.73%（IEX 気配の見かけの広がりの可能性）。決算日の扱いは課題として残る。1日分では優劣・利益の根拠にならない。残っている予約はない。

## 3. 未決・待ち（ユーザーの判断／外部）
| 項目 | 状態 |
|---|---|
| Alpaca サポートの返答 | **届いた（2026-11-13）が、日本の適格性は未確認**（「日本が対象国として表示されている場合は申請できる」という条件付きの返答）。分かったこと: ①現金のみは、口座設定 `max_margin_multiplier=1`・`no_shorting=true`（`PATCH /v2/account/configurations`）で Alpaca 側が強制できる ②PDT は 2.5 万ドル未満の**証拠金**口座に適用、現金口座は T+1 決済の制限 ③入金は**米ドルの国際送金**（日本円の入金は対象国のみ）。手数料と最低入金額は未記載 ④自動売買は可、レート制限は 1 口座あたり **200 リクエスト/分**。**次の確認**: Alpaca のライブ口座の申込み画面で、国に日本を選べるか（申込みを送信せず、入金もしない）。結論が曖昧なので、「日本在住の個人が、いま、ライブ口座の申込みを完了できるか」を はい/いいえ で再質問する |
| 証券会社の選定 | 未承認（ただし前向き）。**運用者がライブ口座の申込み画面（2026-11-14）で確認: 納税地の国に Japan を入力でき、電話は +81、日本専用の Tax ID 種別「kojin bango（個人番号）」がある**＝日本居住者を想定した申込みの流れ。承認ではない（最後の KYC と審査が残る。申込みはまだ送信していない）。ペーパー口座向けのアダプタは、日本の適格性に依らず着手できる。**ライブ口座を開けないと分かった場合は IBKR**（仕様）。Alpaca の公式資料の検索では、日本居住の個人が Trading API のライブ口座を直接開けるかは**確認できなかった**（古いスタッフ発言は「米国外の個人は不可」、日本では子会社の AlpacaJapan や SBI 証券・Woodstock 経由のサービスがある） |
| ~~Paper→Live 基準の残り~~ | **確定（2026-11-13）**: 運用者が「最初の案どおり。期間が長くなっても短縮しない」と回答。検証60営業日・約定30件以上、資本ランプ 0.05→0.10→0.25→0.50→1.00（各20営業日以上）。LIVE の最短は検証開始から約6.5か月。**未決の論点**: 資本10万円では段階1・2で注文が出ない可能性があり、段階ごとの最低約定件数を条件にするか（`docs/paper_live_gate.md`）。証券会社（#1）のみ未承認 |
| ~~許容ドローダウン／口座の前提~~ | **確定（2026-11-11）**: 資本の10%、現金口座・無レバレッジ（Alpaca での適格性は返答待ち） |
| 有料のデータプラン | 未判断（最新の SIP 気配が取れる＝スプレッド判定が正確になる）。費用の判断 |
| ~~通知の宛先~~ | **確定・実送信確認済み（2026-11-11）**: Discord の Webhook。運用者の Windows PC から送信し、チャンネルに届いた。運用者の報告（2026-11-11）: テスト中にチャンネルへ投稿してしまった旧 Webhook は作り直し済み、通知専用のチャンネルも分離済み。残り: 外部の死活監視（`QUANT_WATCHDOG_PING_URL`）は未設定 |
| 実機の systemd での起動・自動再起動 | 未確認（書式検証と runner 単体の動作確認までは済み）。実機が要る |
| 資本セル B/C（サイジング・配分に効く） | 未承認（A は観測のみで稼働中）。`docs/capital_cell_architecture.md` |
| Audit の drift しきい値 / CalibrationTracker のフィードバック（サイジングに効く） | 未判断 |
| LLM の1日あたり予算（A-2） | 未判断（上限は現在プロセス単位） |

## 3b. Alpaca ブローカー・アダプタの計画（CLAUDE.md §23 の実装前報告。**未承認・未着手**）
- **置き場所**: `packages/broker_adapters/alpaca.py`（`BrokerAdapter` の実装を1ファイル。HTTP は標準の `urllib`＋UTF-8＋`User-Agent` を付ける）。配線は `services/composition.py` で、既定は今のまま `PaperBroker`、`--broker alpaca-paper` のときだけ切り替える。実行エンジン・リスク・決定層は変えない。境界テスト（決定層・API 層から `broker_adapters` に届かない）は、同じパッケージなので自動で効く。
- **安全の設計**: アダプタは**接続先のホストから環境を決める**（`paper-api.alpaca.markets`→PAPER。`api.alpaca.markets`（本番）と未知のホストは**構築を拒否**＝この段階では LIVE の経路を作らない）。鍵は環境変数のみ（取引用は `QUANT_ALPACA_TRADING_KEY_ID` / `_SECRET`。データ用の `APCA_*` と混ぜない）。`client_order_id` は Alpaca が重複を拒否するので、二重発注の防止がそのまま使える。タイムアウトは `BrokerTimeoutError`（→UNKNOWN→照合）、切断は `BrokerDisconnectedError`。
- **テスト**: ネットワーク無し（ローカルの模擬サーバーで Alpaca の応答形式を再現。通知の Webhook と同じやり方）。`tests/unit/test_broker_contract.py` の契約に合格させる。
- **進め方（1コミット1責務）**: ①アダプタ＋模擬サーバーのテスト（**完了 2026-11-14**: `packages/broker_adapters/alpaca.py`、`tests/unit/test_alpaca_broker.py` 61件。実装では、価格を取引所の刻み（1セント。1ドル未満は1/100セント）に「承認された価格より不利にならない向き」で丸める［売りは切り上げ、買いは切り下げ］、数量は9桁までの小数文字列、受け付けられなかった注文は `accepted=False`＋理由、送信は再試行せず結果不明は `BrokerTimeoutError`、読み取りは429/5xxを2回まで再試行して失敗なら `BrokerDisconnectedError`、未知の注文状態は `UNKNOWN`、現金は `cash`（`buying_power` ではない）、起動時の口座設定の検証 `verify_account_configuration()`。**まだ配線していない**）→ ②**ペーパー口座の動作確認スクリプト**（運用者の PC で実行。ペーパーのお金だけ）で、下の未確認事項を実測 → ③配線（`--broker alpaca-paper`）と起動時の照合 → ④検証期間の開始。
- **設計上の重大な論点（①の前に決める／②で実測する）**:
  1. **小数株と保護ストップ（GTC）**: 現在の Alpaca の公式資料は、小数株の注文を「時間条件 DAY のみ」と読める（GTC の記載は無い）。このシステムは小数株を買い、保護ストップは GTC 必須（INV-15）。→ 小数株の GTC ストップが通らなければ、**整数株だけで買う**（資金 670 ドルでは買える銘柄が減り、`買える数が0株` の見送りが増える）か、設計変更（§29）が要る。②の実測で確定する。
  2. **口座の資金**: Alpaca のペーパー口座は既定で 10 万ドル。システムの初期資金（670 ドル）と台帳が食い違うと、照合が不一致で停止する。→ ペーパー口座を 670 ドルにリセットできるか（要確認）。できなければ、資金の上限をシステム側で持つ設計が要る（§29）。
  3. **現金口座**: 公式の資料で、ペーパー口座を現金のみに切り替える方法は見つからなかった。ただし 2,000 ドル未満の口座は買付余力が現金と同じ（1倍）に制限される。アダプタは `buying_power` ではなく `cash` を現金として返す。受渡日（T+1）の扱い（T4）は未確認。
  4. **PDT（パターン・デイ・トレーダー）**: 2.5 万ドル未満の口座では、同じ日に買って損切りされると日計り取引に数えられ、回数の制限に当たる可能性（ペーパーにも適用）。アダプタは `trading_blocked` を口座状態として返し、既存の照合が新規の買いを止める。
  5. **再起動**: 実ブローカーでは、口座の状態はブローカー側にある。`--resume` は台帳を復元したあとブローカーと照合し、不一致なら止まる（既存の照合）。起動時の扱いの詳細は③で設計する。
- **Alpaca の返答（2026-11-13）を受けた追加の設計**: ①アダプタは起動時に口座設定を**読み取って検証**し、`max_margin_multiplier` が 1 でない／`no_shorting` が false なら**起動を拒否**する（書き込みは動作確認スクリプトで運用者が1回だけ行う。自動では変えない）。ペーパー口座で設定が保存されない不具合の報告があるため、②で実測する。②HTTP 429（レート制限 200/分）は、再試行できる一時的な失敗として扱う（注文の送信は再試行せず、`UNKNOWN`→照合）。③資金は日本円を米ドルの**国際送金**で入れるため、10 万円（約 670 ドル）に対する送金手数料の比率が大きくなる可能性がある。**入金の前に、手数料と最低入金額を確認する**（未記載）。
- **ライブ口座の返答**（Alpaca サポート）は、ペーパー口座向けのアダプタには要らない。返答が否で IBKR に変えるときは、アダプタの部分だけ書き直す（枠組みは使える）。

## 4. 既知の限界（報告を続けること）
- 実LLMの効果は1日分しか見ていない（検証不足）。通知は **Discord への実送信は確認済み**（Slack・Teams・メールは未確認）。外部の死活監視への ping は、4つのサービス（healthchecks.io・Dead Man's Snitch・Cronitor・Better Stack）にダミーのトークンで試し、識別名の有無で応答が変わらないことを確認した（Discord のような識別名の拒否は無い）。実際の監視の登録と、相手先を選んだ後の実送信は未確認。
- 観測者の累積統計は再開すると初期化される。永続化は PaperBroker のみ。実ブローカー用の復元（照合）は未実装。
- ダッシュボードの値は**直近セッション時点**でリアルタイムではない。
- 日本語表示は既知の文型のみ。識別子（ランナー・リスクの状態、売買、評価項目、シャドーの変種など）は `jaWord()`（`JA_WORDS`）で日本語にした（状態と重大度は「通常（NORMAL）」のように元の英語を併記）。バックエンドの enum の値が増えると、テストが失敗して追加を促す。AI の自由文はそのまま英語。変換表（業種→日本語の系統）は主要な約100コードで、足りないものは大分類か英語になる。
- スマホの盤で長い会社名は2行で切れる（詳細シートで全文が読める）。
- 取引カレンダーは 2025〜2028 のみ（2029 以降は失敗側＝停止）。
- 画面の見た目の確認は**模擬データ**で行った。実データ状態での見た目は未確認。

## 5. 次にできること（運用者の判断が要らないもの）
- 実データ状態（`--real-data` を使った状態ディレクトリ）でのダッシュボードの見た目確認。※実LLMは使わない。モック AI で十分。
- 取引カレンダーの 2029 以降（公式発表が出てから）。
- 上記以外（§3）は、運用者の判断を待つ。

## 6. よく使うコマンド
```bash
pip install -e '.[dev,api]'                                  # 環境に pytest が無いとき
python -m pytest -q                                          # 全テスト（約15秒）
python scripts/run_paper_runner.py --show-schedule           # 次のセッション予定
env -u MY_ANTHROPIC_API_KEY -u ANTHROPIC_API_KEY -u OPENAI_API_KEY HOME=/nonexistent \
  python scripts/run_llm_paper_demo.py --days 1              # モックAIでの確認（LLM費用ゼロ）
# 実LLM: ユーザーが明示したときだけ（--real-data、市場が開いている時間 = 日本時間 23:30〜06:00）
systemd-analyze verify ./quant-runner.service ./quant-watchdog.service   # 設定例の書式検証
```
ダッシュボードの見た目確認は、Playwright（導入済み）でスクリーンショットを撮る。状態ディレクトリは `scripts/run_ops_dashboard.py --state-dir <dir> --port 8765` で読む。模擬データの状態ディレクトリは、`tests/unit/test_ops_dashboard.py` の `state` fixture と同じ手順（`build_pipeline` → `SessionRunner.run_forever`）で作れる。

## 7. 主なファイル
- 画面: `apps/web/ops.html`（日本語表示は `<ja-translator>` ブロック）、API: `apps/api/ops.py`、状態: `services/supervisor/ops_state.py`（`holdings()`、注目順位 `attention_of()`）
- 保有の記録: `services/supervisor/session_record.py`（`holdings_view`、自動プロフィール）
- 会社情報: `packages/common/symbol_profiles.py`、`services/market_data/edgar_company.py`、`edgar_client.py`（`company_info`）
- データ: `services/market_data/alpaca_provider.py`
- 永続化: `services/checkpoint.py`、`packages/common/durable_store.py`
- 規約: `CLAUDE.md`、設計: `docs/DESIGN_SYSTEM.md`、運用: `docs/operations.md`、安全: `docs/invariants.md` / `docs/SAFETY_AUDIT.md`、LIVE の基準: `docs/paper_live_gate.md`（草案）
