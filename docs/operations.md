# 運用手順書（ペーパー運用）

> 対象: `scripts/run_paper_runner.py` による長期ペーパー運用。**実注文は送れない**（模擬ブローカーのみ）。
> 設計の背景は `docs/MASTER_SPEC.md`（「実行基盤」「セッション成績の記録」「学習・観測ループ」）、安全装置は
> `docs/invariants.md` / `docs/SAFETY_AUDIT.md`。**この手順書の systemd / cron の設定例は、このリポジトリのテスト環境では
> 動かして確認していない（書式の例）。** 初回は手元で動作確認してから本番に置くこと。

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
| `QUANT_SEC_USER_AGENT` | SEC EDGAR（`"アプリ名 連絡先メール"`） | 任意（無ければ EDGAR 無し） |
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

### systemd の例（未検証の書式例）
```ini
# /etc/systemd/system/quant-runner.service
[Service]
WorkingDirectory=/opt/quant
EnvironmentFile=/etc/quant/runner.env          # 秘密はここ（権限 600）
ExecStart=/usr/bin/python3 scripts/run_paper_runner.py --real-data --state-dir /var/lib/quant/runner-20261110 --resume-if-exists
Restart=on-failure                             # 落ちたら保存から再開（検証に失敗・未解決の重大障害なら起動しない）
RestartSec=30
RestartPreventExitStatus=2                     # 「起動を拒否」(終了コード2)は再試行しても無駄なので止める（watchdog が通知）
StartLimitIntervalSec=600
StartLimitBurst=5
KillSignal=SIGTERM
TimeoutStopSec=30
```
```ini
# /etc/systemd/system/quant-watchdog.service  （常駐。cron なら * * * * * でも可）
[Service]
WorkingDirectory=/opt/quant
EnvironmentFile=/etc/quant/runner.env
ExecStart=/usr/bin/python3 scripts/watchdog.py --state-dir /var/lib/quant/runner-20261110 --loop
Restart=always
```

## 4. 平常運用（毎日の確認 — ダッシュボード）
- ランプが **OK**、ランナーが **WAITING**（セッション中は **RUNNING**）、心拍が数十秒前。
- 日本時間 23:45 頃（夏時間は 22:45）に「最終セッション」が当日の日付になる。
- 「成績」は **セッション数が少ない間は優劣の判断材料にならない**（基準案は 60 営業日以上）。
- 「障害」に未解決がない、「通知の履歴」に見覚えのない CRITICAL がない。

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
