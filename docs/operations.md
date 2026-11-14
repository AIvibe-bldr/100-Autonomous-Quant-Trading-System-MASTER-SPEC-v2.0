# 運用手順書（ペーパー運用）

> 対象: `scripts/run_paper_runner.py` による長期ペーパー運用。**実注文は送れない**（模擬ブローカーのみ）。
> 設計の背景は `docs/MASTER_SPEC.md`（「実行基盤」「セッション成績の記録」「学習・観測ループ」）、安全装置は
> `docs/invariants.md` / `docs/SAFETY_AUDIT.md`。**この手順書の systemd の設定例は書式を `systemd-analyze verify` で確認した
> が、実機では動かしていない。cron の例は未検証。** 初回は手元で動作確認してから本番に置くこと。

## 1. 全体像

| プロセス | 役割 | 起動 |
|---|---|---|
| **runner** | 取引日ごとに1回セッションを実行。毎 tick（60秒）、状態ファイルとハートビートを更新 | `scripts/run_paper_runner.py` |
| **watchdog** | runner の**外**から状態を見て、異常を通知（runner が死んでも検知できる） | `scripts/watchdog.py`（cron / timer / `--loop`） |
| **ops dashboard** | 状態を**読むだけ**の画面（書き込み・操作の経路なし） | `scripts/run_ops_dashboard.py` |
| CLI | 人の操作（名前付き・理由つき・追記専用の監査ログ） | `kill_switch.py` / `incidents.py` / `human_intent.py` / `live_gate.py` |

状態ディレクトリ（`--state-dir`、既定 `var/runner`）の中身:
`runner_status.json`（心拍）、`sessions.db`（成績）、`orders.db`・`audit.db`（注文・監査）、`pit.db`（判断の入力と出力）、
`alerts.jsonl`（通知の履歴）、`watchdog_state.json`（通知済みの状態）。
状態ディレクトリの**外**（口座をまたぐ運用ファイル）: `var/kill_switch.json`、`var/incidents.jsonl`、
`var/human_intents.jsonl`、`var/live_gate.json`。

## 2. 環境変数

| 変数 | 用途 | 必須 |
|---|---|---|
| `APCA_API_KEY_ID` / `APCA_API_SECRET_KEY` | Alpaca の市場データ・ニュース（`--real-data`） | 実データ時 |
| `QUANT_ALPACA_BARS_FEED` | 日足の取得元: `sip`（既定。全取引所合算の出来高。無料プランで取れる）／`iex`（IEX のみ。出来高は市場の約5%）。SIP が拒否されたら自動で IEX に戻り、画面・出力に注意書きが出る。**気配（スプレッド）は無料プランでは IEX のみ** | 任意 |
| `QUANT_SEC_USER_AGENT` | SEC EDGAR（`"アプリ名 連絡先メール"`） | 任意（無ければ EDGAR 無し） |
| `QUANT_ALPACA_TRADING_KEY_ID` / `QUANT_ALPACA_TRADING_SECRET` | Alpaca の**ペーパー口座の取引用キー**（データ用の `APCA_*` とは別）。ブローカー・アダプタ用。**まだ起動には配線していない**（手順3で `--broker alpaca-paper` を追加予定）。本番のホストは受け付けない | 将来 |
| `QUANT_ALERT_WEBHOOK_URL` | Slack / Discord / Teams の受信 Webhook（**https のみ**） | 通知する場合 |
| `QUANT_ALERT_EMAIL_TO`, `QUANT_SMTP_HOST`, `QUANT_SMTP_FROM`（＋`_PORT`・`_USER`・`_PASSWORD`・`_STARTTLS`） | メール通知 | 任意 |
| `QUANT_ALERT_PREFIX` | 通知の先頭に付ける名前（例: `paper-1`） | 任意 |
| `QUANT_WATCHDOG_PING_URL` | 外部の死活監視サービス（healthchecks.io 型）。**マシンごと止まる場合の備え** | 推奨 |
| `QUANT_OPS_TOKEN` | ダッシュボードの合言葉（ループバック以外で公開する時は**必須**） | 公開時 |
| `QUANT_KILL_SWITCH_FILE` / `QUANT_INCIDENT_LOG` / `QUANT_INTENT_LOG` / `QUANT_LIVE_GATE_FILE` | 運用ファイルの場所（既定は `var/`） | 任意 |

秘密（Webhook URL・SMTP パスワード・API キー）は**環境変数だけ**に置く。リポジトリ・ログ・エラーメッセージには出ない作り。
LLM のキー（`MY_ANTHROPIC_API_KEY`・`OPENAI_API_KEY`）は、この実行器では**使わない**（実LLMは別の明示操作。`docs/MASTER_SPEC.md` 参照）。

## 3. 起動

```bash
# 1. スケジュールの確認（取引はしない。日本時間の冬は 23:45 JST）
python3 scripts/run_paper_runner.py --show-schedule
# 2. 新しい口座として起動（空のディレクトリ）
python3 scripts/run_paper_runner.py --real-data --state-dir var/runner-20261110 --model-tag mock-ai
#    落ちた後・再起動後は同じディレクトリで続ける（保存を復元して検証する）
python3 scripts/run_paper_runner.py --real-data --state-dir var/runner-20261110 --resume
# 3. 別のターミナル／プロセスで監視役
python3 scripts/watchdog.py --state-dir var/runner-20261110 --loop
# 4. 画面（http://127.0.0.1:8080/）
python3 scripts/run_ops_dashboard.py --state-dir var/runner-20261110
```

**口座は保存され、再開できる（2026-11-10）**: ペーパー口座（模擬ブローカー＋台帳＋損切り登録簿）は、整合点
（注文の約定と保護ストップが揃った時点・決済約定の後・各セッションの前後）で `checkpoints.db` に保存される。再起動時は:

| 状態ディレクトリ | 指定 | 動作 |
|---|---|---|
| 空（新規） | なし | 新しい口座を開始 |
| 口座あり | なし | **拒否**（`--resume` か新しい `--state-dir` を促す） |
| 口座あり | `--resume` | 保存を**復元して検証**し、継続（検証に失敗すれば起動せず人の判断） |
| 空 | `--resume` | 拒否（再開するものがない） |
| どちらでも | `--resume-if-exists` | あれば再開、なければ新規（サービス管理用） |

**復元は信頼せず検証する**: 台帳は記録から再計算し、台帳と模擬ブローカーの照合が一致し、保有する全銘柄に生きた保護ストップがあることを確認する。
どれかが崩れていれば `cannot resume` で止まる（その口座では取引しない）。破損した最新の保存は、前の世代へ自動で退避する。
- **途中で落ちた場合**: 最後の整合点までを復元する。その後・次の保存の前に動いていた取引（エントリーと保護ストップの2本）は
  **一緒に巻き戻され**（記録上 REJECTED）、「ストップの無い保有」は残らない。軽微な障害として記録される。
- **セッション中に落ちた日**は再実行しない（途中で止まった日の二重取引を避ける）。成績の記録は欠ける。
- **リスク停止（HALT）は再起動をまたいで残る**。解除は名前付きで: `--resume --clear-halt --operator 名前 --reason "確認したこと"`（インシデントログに記録）。
- 復元されないもの: 観測者の**累積統計**（予測の較正・シャドー・損益分解などは再開時に0から）、資本セルの「AI／人」の内訳
  （再開時点の保有は AI セルに入る）、サーキットブレーカーの窓（空から）。口座そのものは完全に復元される。

### systemd の例（書式は `systemd-analyze verify` で確認済み。実機への導入は未実施）
**systemd は行末コメントを使えない**（`Restart=on-failure   # 説明` は値が壊れて**無視される**）。コメントは必ず別の行に書く。
`StartLimitIntervalSec` / `StartLimitBurst` は `[Unit]` 節に置く（`[Service]` 節では無視される）。
```ini
# /etc/systemd/system/quant-runner.service
[Unit]
Description=Quant paper runner
After=network-online.target
Wants=network-online.target
# 10分に5回までの再起動で打ち切る
StartLimitIntervalSec=600
StartLimitBurst=5

[Service]
Type=simple
WorkingDirectory=/opt/quant
# 秘密はここ（権限 600）
EnvironmentFile=/etc/quant/runner.env
ExecStart=/usr/bin/python3 scripts/run_paper_runner.py --real-data --state-dir /var/lib/quant/runner-20261110 --resume-if-exists
# 異常終了（終了コード1）なら保存から再開する。SIGTERM（systemctl stop）は終了コード0で再起動しない。
# 検証に失敗・未解決の重大障害なら runner が起動を拒否する
Restart=on-failure
RestartSec=30
# 「起動を拒否」（終了コード2）は再試行しても無駄なので止める（watchdog が通知）
RestartPreventExitStatus=2
KillSignal=SIGTERM
TimeoutStopSec=30

[Install]
WantedBy=multi-user.target
```
```ini
# /etc/systemd/system/quant-watchdog.service  （常駐。cron なら * * * * * でも可）
[Unit]
Description=Quant watchdog
After=network-online.target

[Service]
Type=simple
WorkingDirectory=/opt/quant
EnvironmentFile=/etc/quant/runner.env
ExecStart=/usr/bin/python3 scripts/watchdog.py --state-dir /var/lib/quant/runner-20261110 --loop
Restart=always
RestartSec=10

[Install]
WantedBy=multi-user.target
```
確認済み（2026-11-11、模擬AI・キー無し）: SIGTERM で終了コード0・状態 `STOPPED`／口座のある `--state-dir` を `--resume` 無しで起動すると
終了コード2で拒否／`--resume-if-exists` で保存から再開／口座のない `--state-dir` に `--resume` は終了コード2で拒否。
**未確認**: 実機の systemd での起動・自動再起動、`EnvironmentFile` の権限、実ブローカー用の復元。

### Windows で動かす場合（テスト済みの範囲と未確認）
- **確認済み**: 全テストが GitHub Actions の Windows（Python 3.12）で通る（CI で毎回確認）。Discord への通知の実送信を、運用者の Windows PC で確認済み。この CI で、Windows 固有の不具合を2件直した（`tzdata` が無く取引カレンダーが読めない／UTF-8 のファイルを既定の cp1252 で読もうとして失敗）。
- **起動**（PowerShell。環境変数は§2の表。`$env:` はそのウィンドウの中だけ有効）:
```powershell
pip install -e .
python scripts/run_paper_runner.py --state-dir C:\quant-state\runner-20261112 --resume-if-exists
```
- **止め方**: ウィンドウで **Ctrl+C**（または Ctrl+Break）。約1秒で止まり、状態は `STOPPED`・終了コード0になる（CI で、実際のプロセスに停止の合図を送って確認。Windows では待ち時間が1回分（60秒）かかっていたため、1秒ごとに区切る修正を入れた）。ウィンドウを閉じると、記録なしで止まる（再開は同じコマンド。保存から続きになるが、止めるときは Ctrl+C を使う）。watchdog も Ctrl+C で静かに終わる。
- **systemd が無い**ので、落ちたときの自動再開はない。PC のスリープでも止まる（電源設定でスリープを無効にする）。取引時間は日本時間 23:30〜06:00。常時稼働は Linux の機械が向いている。
- **運用者の PC（Windows 11・PowerShell・Python 3.12）で確認済み（2026-11-11）**: 模擬データでランナーが起動し、**キーボードの Ctrl+C で約1秒で止まり**、`--resume-if-exists` で保存から再開できる。
- **未確認**: タスクスケジューラでの自動起動、長時間（数日）の連続稼働。

### Alpaca ペーパー口座の動作確認（手順2。ペーパーのお金だけ。実注文は送れない）
`scripts/alpaca_paper_check.py` は、システムが前提にしていること（現金のみの設定、小数株の GTC 損切り、約定の記録、現金の動き）を、実際のペーパー口座で確かめる。
キーはダッシュボードでペーパー口座を選び、API キーを発行する（**シークレットは発行時にしか表示されない**）。**画面に出さずに**入れる（PowerShell）:
```powershell
$s = Read-Host "Key ID" -AsSecureString
$env:QUANT_ALPACA_TRADING_KEY_ID = [System.Net.NetworkCredential]::new("", $s).Password
$s = Read-Host "Secret" -AsSecureString
$env:QUANT_ALPACA_TRADING_SECRET = [System.Net.NetworkCredential]::new("", $s).Password
python scripts/alpaca_paper_check.py                       # 読み取りだけ（いつでも）
python scripts/alpaca_paper_check.py --set-cash-config     # 現金のみ・空売りなしを設定し、読み戻して確認
python scripts/alpaca_paper_check.py --orders fractional   # 米国市場が開いている時間に: SPY を 0.01 株買い、GTC の損切りを試し、取り消す
python scripts/alpaca_paper_check.py --orders whole        # 同じことを、1株（既定は F）で
python scripts/alpaca_paper_check.py --cleanup             # 翌日以降に: 買った分を売る（同じ日の売買は日計り取引になる）
```
市場が開いている時間は、日本時間の夜（夏時間は 22:30〜05:00、冬時間は 23:30〜06:00）。結果の「gtc_stop_accepted」が False なら、小数株では GTC の損切りが使えない（INV-15 に関わる）ので、設計の判断が要る（`docs/HANDOFF_next_session.md` §3b）。

### 外部の死活監視の設定（マシンごと止まったときの備え。healthchecks.io の例）
watchdog は runner と同じマシンで動くので、**マシンが止まる・スリープする・ネットが切れると、Discord にも何も届かない**。そこで watchdog が
健全なときだけ（CRITICAL でないとき）毎回、外部のサービスへ ping を送り、**ping が途絶えたら外部のサービスが通知する**（沈黙が警報になる）。
1. https://healthchecks.io でアカウントを作り、「Add Check」でチェックを1つ作る。名前は `quant-paper` など。
2. **Period = 1 分、Grace Time = 5 分**にする（watchdog の `--interval` の既定が60秒。心拍の古さの上限も5分）。
3. チェックの **Ping URL**（`https://hc-ping.com/…`）をコピーする。これは秘密（知っていれば誰でも「正常」を偽装できる）。
4. 通知先を付ける: 「Integrations」から Discord（または Slack 互換の Webhook）を足し、チェックに紐付ける。通知専用のチャンネルを使う。
5. watchdog を起動するウィンドウで、URL を**画面に出さずに**入れる（PowerShell）:
```powershell
$s = Read-Host "Ping URL" -AsSecureString
$env:QUANT_WATCHDOG_PING_URL = [System.Net.NetworkCredential]::new("", $s).Password
python scripts/watchdog.py --state-dir C:\quant-state\runner-20261112 --loop
```
   Linux では `/etc/quant/runner.env` に `QUANT_WATCHDOG_PING_URL=…`（権限 600）を書く。runner とは別のプロセス（別のウィンドウ／別の systemd ユニット）で動かす。
6. ping が届くか1回だけ確かめる（URL は表示されない。`200` と出て、healthchecks.io のチェックが緑になる）:
```powershell
(Invoke-WebRequest -Uri $env:QUANT_WATCHDOG_PING_URL -UseBasicParsing).StatusCode
```
7. **本当に通知が来るか確かめる**: watchdog を止めて（Ctrl+C）6分待つ。Discord に「down」の通知が来れば成功。その後、watchdog を起動し直すと「up」に戻る。
- 確認済み（2026-11-11）: 4つの主要サービスは、識別名の有無で ping の応答が変わらない（Discord のような拒否は無い）。**あなたのアカウントでの登録と、手順7の通知の到達は未確認**。

## 4. 平常運用（毎日の確認 — ダッシュボード）
- ランプが **OK**、ランナーが **WAITING**（セッション中は **RUNNING**）、心拍が数十秒前。
- 日本時間 23:45 頃（夏時間は 22:45）に「最終セッション」が当日の日付になる。
- 「成績」は **セッション数が少ない間は優劣の判断材料にならない**（基準案は 60 営業日以上）。
- 「障害」に未解決がない、「通知の履歴」に見覚えのない CRITICAL がない。

- 画面は3つのタブ: **今日**（状態・資産・気にしてほしい銘柄のゲージ・推移・直近にしたこと・今日の流れ）／**銘柄**（`#all`。全銘柄を同じ大きさのウィンドウで並べる「盤」と、ゲージ付きの「表」を切替。緑＝利益、赤＝損失。並べ替えと系統の絞り込み）／**詳細**（`#full`。従来の全項目）。デザインは `docs/DESIGN_SYSTEM.md`。
- 「気にしてほしい銘柄」は、損切りに近い・利益目標に近い・大きく動いた・資産の20%以上・計画の記録なし、のいずれかが強い順に最大10件。各行のゲージは 損切り〜目標 の中での現在位置。押すと、買った理由・損切りライン・利益目標・保有の目安・やめる条件（買う前に作った計画）が開く。この順位は表示だけの目安で、売買・損切りには影響しない。会社名・系統・事業の説明は、まず手書きの表 `packages/common/symbol_profiles.py`（日本語名つき）を使う。表にない銘柄は、`--real-data` で EDGAR が使えるとき、SEC の会社記録から**英語の会社名と業種（日本語の系統に変換、変換できなければ英語のまま）を自動で補い**、「自動取得。事業内容の説明ではありません」と明記する（SEC には事業内容の説明文がない。AI による翻訳・要約は費用が出るので入れていない）。どちらも取れなければ記号のみ。値は**直近セッション時点**（`/api/holdings`、読み取り専用）。

## 5. 通知・状態の見方と対応

| 検知（watchdog / ダッシュボード） | 意味 | まずやること |
|---|---|---|
| `no_status` | runner が一度も状態を書いていない | 起動したか、`--state-dir` が合っているか |
| `status_unreadable` | 状態ファイルが壊れている | ディスク満杯・権限を確認。runner のログを見る |
| `heartbeat_stale`（CRITICAL） | 5分以上心拍なし（セッション中は30分） | プロセスの有無（`ps`）、ログ、ディスク。**自動再起動しない**。再開は §6 |
| `runner_failed`（CRITICAL） | runner が失敗で停止（重大障害として記録済み） | 理由を読み、§6 の手順 |
| `open_incident`（CRITICAL） | 未解決の重大障害 | `scripts/incidents.py show ID` → 原因を調べて解決 |
| `runner_stopped` | 依頼により停止中 | 意図した停止か |
| `session_overdue` | 動いているのに今日のセッションが走っていない | 市場オープン中か、ログに警告がないか |
| `risk_not_normal` | リスク管理が新規建てを止めている（HALT 等） | 理由はログ・ダッシュボード。再開は人が判断（自動復帰しない） |
| `kill_switch` | 緊急停止が作動中 | 解除は §7 |
| `live_gate_closed` | LIVE で承認が無い | LIVE は未対応（`docs/paper_live_gate.md`） |
| 外部死活監視からの通知 | マシン・watchdog が止まった、または runner が CRITICAL | マシンの状態から確認 |

同じ異常の通知は**変化した時だけ**（CRITICAL は6時間ごとに再通知）。回復すると「recovered」が届く。

## 6. 重大障害からの再開（人の確認が必須: §69）
```bash
python3 scripts/incidents.py list
python3 scripts/incidents.py show INC-0003     # postmortem の雛形（TODO）を埋める材料
# 原因を調べ、直したうえで:
python3 scripts/incidents.py resolve INC-0003 --operator あなたの名前 --reason "原因と対処"
# 同じ口座を続ける（保存を復元・検証する。リスク停止が残っていれば --clear-halt を足して解除）
python3 scripts/run_paper_runner.py --real-data --state-dir var/runner-20261110 --resume
# 復元できない（cannot resume）場合は、新しい --state-dir で新しい口座を始める
```
解決の記録がない間、runner は起動しない。解決は操作者名と理由が必須で、追記専用のログに残る。

## 7. 緊急停止
```bash
python3 scripts/kill_switch.py engage --operator 名前 --reason "理由"   # 新規建てを止める（決済・保護ストップは止めない）
python3 scripts/kill_switch.py clear  --operator 名前 --reason "理由"   # 解除（止まっていた runner は再起動が必要）
```

## 8. 人の意図（買いたい銘柄がある時）
```bash
python3 scripts/human_intent.py submit NVDA --operator 名前 --reason "理由"   # 願望であって注文ではない
python3 scripts/human_intent.py show HI-...        # 市場オープン中に分析が付く。読む
python3 scripts/human_intent.py confirm HI-... --operator 名前   # 次のセッションで AI 候補と同じ関門へ（2日で失効）
```
確認しても、Audit AI・Master Risk Controller が拒否することがある。結果は `show` とダッシュボードに残る。買いのみ。

## 9. バックアップと保守
- 状態ディレクトリは**丸ごと**コピー（`sessions.db` が成績、`checkpoints.db` が口座の唯一の記録）。実行中のコピーは読み取りで安全だが、停止後が確実。
- 取引カレンダーは**2028年末まで**。以降は `packages/common/calendar.py` へ NYSE の発表で追加（無いと runner は停止する。10月以降の起動時に警告が出る）。
- ダッシュボードを外部に出す場合: `QUANT_OPS_TOKEN` ＋ TLS（リバースプロキシ）。トークンは URL の `?token=` でも渡せるが、履歴に残るので Bearer ヘッダを推奨。

## 10. 既知の限界（運用上）
- watchdog は runner と**同じファイル**を見るので、同一マシンならマシンの停止は検知できない → `QUANT_WATCHDOG_PING_URL` の外部死活監視で補う。
- 通知先（Slack 等）への**実送信は、ローカルの模擬サーバーでしか確認していない**。設定後に、わざと異常を作って（例: `--state-dir` を間違える）届くか試すこと。
- 実LLM・実データでの長期連続稼働は未経験。数日は人が見守る前提。
- 保存は**模擬ブローカー専用**。実ブローカーは相手が口座の真実を持つので、復元ではなく照合で扱う（未実装）。
