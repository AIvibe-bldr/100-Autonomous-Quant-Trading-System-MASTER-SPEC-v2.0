"""Check an Alpaca PAPER account against what this system assumes (docs/HANDOFF_next_session.md §3b, step 2).

Run it on the operator's machine with the PAPER trading keys in the environment
(QUANT_ALPACA_TRADING_KEY_ID / QUANT_ALPACA_TRADING_SECRET). The adapter it uses can only connect to the paper
endpoint, so this script cannot send a real order. Nothing it prints contains the keys.

  python scripts/alpaca_paper_check.py                       # read-only: account, settings, positions, open orders
  python scripts/alpaca_paper_check.py --set-cash-config     # also set cash-style + long-only, then read it back
  python scripts/alpaca_paper_check.py --orders fractional   # PAPER orders (US market hours): buy 0.01 of SPY, try a
                                                             # GTC protective stop on it, cancel the stop
  python scripts/alpaca_paper_check.py --orders whole        # the same with 1 whole share of --whole-symbol
  python scripts/alpaca_paper_check.py --cleanup             # sell what the checks bought (run it the NEXT day: a
                                                             # same-day buy and sell is a day trade)

What the order checks answer (the system holds fractional shares and requires a GTC protective stop, INV-15):
does Alpaca accept a GTC stop on a fractional quantity, are the fills complete, does the cash move as expected."""
from __future__ import annotations

import argparse
import json
import sys
import time
import uuid
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from packages.broker_adapters.alpaca import AlpacaBroker, AlpacaConfigurationError
from packages.broker_adapters.base import BrokerDisconnectedError, BrokerTimeoutError, DuplicateClientOrderIdError
from packages.schemas.core import Action, BrokerOrderRequest, OrderState, OrderType, TimeInForce

sleep = time.sleep                    # replaced in tests
FILL_TIMEOUT_SECONDS = 60.0


def say(text: str = "") -> None:
    print(text, flush=True)


def show_account(broker: AlpacaBroker) -> bool:
    info = broker.describe_account()
    say("== 口座（ペーパー）")
    for key, value in info.items():
        say(f"  {key}: {value}")
    ok = True
    try:
        broker.verify_account_configuration()
        say("  設定の検証: OK（現金のみ・空売りなし）")
    except AlpacaConfigurationError as e:
        ok = False
        say(f"  設定の検証: NG — {e}")
        say("    → --set-cash-config で設定できます（ペーパー口座で設定が保存されない不具合の報告があるため、読み戻して確認します）")
    positions, orders = broker.get_positions(), broker.get_open_orders()
    say(f"  保有: {[(p.symbol, p.qty) for p in positions] or 'なし'}")
    say(f"  未約定の注文: {len(orders)} 件")
    return ok


def set_cash_config(broker: AlpacaBroker) -> None:
    say("== 現金のみ・空売りなしの設定（max_margin_multiplier=1, no_shorting=true）を書き込みます")
    broker.configure_cash_style()
    try:
        broker.verify_account_configuration()
        say("  読み戻し: OK（設定は保存されました）")
    except AlpacaConfigurationError as e:
        say(f"  読み戻し: NG — 保存されていません: {e}")


def _wait_for(broker: AlpacaBroker, cid: str, wanted: tuple, timeout: float):
    end = time.monotonic() + timeout
    while True:
        st = broker.get_order_status(cid)
        if st.state in wanted or time.monotonic() >= end:
            return st
        sleep(2.0)


def _record(state_dir: Path, symbol: str, qty: float) -> None:
    path = state_dir / "alpaca_check.json"
    state_dir.mkdir(parents=True, exist_ok=True)
    items = json.loads(path.read_text(encoding="utf-8")) if path.exists() else []
    items.append({"symbol": symbol, "qty": qty})
    path.write_text(json.dumps(items), encoding="utf-8")


def check_orders(broker: AlpacaBroker, kind: str, symbol: str, qty: float, state_dir: Path) -> dict:
    """Buy a tiny quantity at the market, try a GTC protective stop on it, cancel the stop. Returns the findings."""
    findings: dict = {"kind": kind, "symbol": symbol, "qty": qty}
    clock = broker.market_clock()
    if not clock["is_open"]:
        say(f"米国市場が閉じています（次の開場: {clock['next_open']}）。市場が開いている時間（日本時間の夜）に、もう一度実行してください。")
        findings["skipped"] = "market closed"
        return findings
    tag = uuid.uuid4().hex[:6]
    cash_before = broker.get_cash().settled_cash
    say(f"== 注文の確認（{kind}）: {symbol} を {qty} 株、成行で買います（ペーパーのお金です）")
    buy_id = f"check-{kind}-buy-{tag}"
    ack = broker.submit_order(BrokerOrderRequest(client_order_id=buy_id, symbol=symbol, side=Action.BUY, qty=qty,
                                                 order_type=OrderType.MARKET, time_in_force=TimeInForce.DAY))
    findings["buy_accepted"] = ack.accepted
    if not ack.accepted:
        say(f"  買い注文が拒否されました: {ack.reason}")
        return findings
    st = _wait_for(broker, buy_id, (OrderState.FILLED, OrderState.REJECTED, OrderState.CANCELLED, OrderState.EXPIRED), FILL_TIMEOUT_SECONDS)
    findings["buy_state"] = st.state.value
    if st.state is not OrderState.FILLED:
        say(f"  {FILL_TIMEOUT_SECONDS:.0f} 秒以内に約定しませんでした（状態 {st.state.value}）。注文を取り消します。")
        broker.cancel_order(buy_id)
        return findings
    _record(state_dir, symbol, st.filled_qty)
    fills = broker.get_order_fills(buy_id)
    filled_sum = round(sum(f.qty for f in fills), 9)
    findings["fills_complete"] = abs(filled_sum - st.filled_qty) < 1e-6
    avg = sum(f.qty * f.price for f in fills) / filled_sum if filled_sum else 0.0
    say(f"  約定: {st.filled_qty} 株、約定の記録 {len(fills)} 件（合計 {filled_sum} 株、平均 {avg:.4f} ドル）"
        f" — 約定の記録は{'そろっています' if findings['fills_complete'] else '【不足しています】'}")
    findings["cash_change"] = round(broker.get_cash().settled_cash - cash_before, 4)
    say(f"  現金の変化: {findings['cash_change']} ドル（約定額 {st.filled_qty * avg:.4f} ドルとの差が、手数料や丸めの目安です）")

    stop_id = f"check-{kind}-stop-{tag}"
    stop_price = round(avg * 0.80, 2)
    say(f"  保護の損切り注文（GTC・{stop_price} ドル）を出します")
    stop_ack = broker.submit_order(BrokerOrderRequest(client_order_id=stop_id, symbol=symbol, side=Action.SELL, qty=st.filled_qty,
                                                      order_type=OrderType.STOP, stop_price=stop_price, time_in_force=TimeInForce.GTC))
    findings["gtc_stop_accepted"] = stop_ack.accepted
    if stop_ack.accepted:
        say("  → 受け付けられました（この数量で、GTC の損切りが使えます）")
        try:
            cancel = broker.cancel_order(stop_id)
            final = _wait_for(broker, stop_id, (OrderState.CANCELLED,), 15.0)
            findings["stop_cancelled"] = final.state is OrderState.CANCELLED
            say(f"  損切り注文の取り消し: {'成功' if findings['stop_cancelled'] else '【未確認】ダッシュボードで注文 ' + stop_id + ' を取り消してください'}"
                f"（応答: {cancel.accepted}）")
        except (BrokerDisconnectedError, BrokerTimeoutError) as e:
            findings["stop_cancelled"] = False
            say(f"  取り消しに失敗しました（{e}）。ダッシュボードで注文 {stop_id} を取り消してください。")
    else:
        say(f"  → 拒否されました: {stop_ack.reason}")
        say("    （小数株では、GTC の損切りが使えない可能性があります。INV-15 に関わるため、結果を教えてください）")
    return findings


def cleanup(broker: AlpacaBroker, state_dir: Path) -> None:
    path = state_dir / "alpaca_check.json"
    items = json.loads(path.read_text(encoding="utf-8")) if path.exists() else []
    if not items:
        say("売る対象の記録がありません（先に --orders を実行すると記録されます）。")
        return
    if not broker.market_clock()["is_open"]:
        say("米国市場が閉じています。開いている時間に、もう一度実行してください。")
        return
    held = {p.symbol: p.qty for p in broker.get_positions()}
    remaining = []
    for item in items:
        qty = min(float(item["qty"]), held.get(item["symbol"], 0.0))
        if qty <= 0:
            say(f"  {item['symbol']}: 保有がないので、記録だけ消します")
            continue
        cid = f"check-cleanup-{uuid.uuid4().hex[:8]}"
        ack = broker.submit_order(BrokerOrderRequest(client_order_id=cid, symbol=item["symbol"], side=Action.SELL, qty=qty,
                                                     order_type=OrderType.MARKET, time_in_force=TimeInForce.DAY))
        st = _wait_for(broker, cid, (OrderState.FILLED, OrderState.REJECTED), FILL_TIMEOUT_SECONDS) if ack.accepted else None
        done = st is not None and st.state is OrderState.FILLED
        say(f"  {item['symbol']} {qty} 株の売り: {'約定' if done else '未約定・拒否 ' + (ack.reason if not ack.accepted else '')}")
        if not done:
            remaining.append(item)
    path.write_text(json.dumps(remaining), encoding="utf-8")


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("--set-cash-config", action="store_true")
    parser.add_argument("--orders", choices=["fractional", "whole"])
    parser.add_argument("--whole-symbol", default="F", help="a low-priced stock for the whole-share check")
    parser.add_argument("--fractional-symbol", default="SPY")
    parser.add_argument("--cleanup", action="store_true")
    parser.add_argument("--state-dir", default="var/alpaca_check")
    parser.add_argument("--base-url", default=None, help="for tests only: a loopback address")
    args = parser.parse_args(argv)
    try:
        sys.stdout.reconfigure(errors="replace")          # a console that cannot show Japanese must not stop the check
    except (AttributeError, ValueError):
        pass
    try:
        kw = {"base_url": args.base_url} if args.base_url else {}
        broker = AlpacaBroker.from_env(**kw)
        say(f"接続先の環境: {broker.environment.value}（ペーパー専用のアダプタです。実注文は送れません）")
        if args.set_cash_config:
            set_cash_config(broker)
        show_account(broker)
        state_dir = Path(args.state_dir)
        if args.orders:
            symbol, qty = (args.fractional_symbol, 0.01) if args.orders == "fractional" else (args.whole_symbol, 1.0)
            findings = check_orders(broker, args.orders, symbol, qty, state_dir)
            say("== まとめ")
            for key, value in findings.items():
                say(f"  {key}: {value}")
            say("  ※ 買った分は、翌日以降に --cleanup で売ってください（同じ日の売買は、日計り取引になります）")
        if args.cleanup:
            cleanup(broker, state_dir)
    except (BrokerDisconnectedError, BrokerTimeoutError, DuplicateClientOrderIdError, AlpacaConfigurationError,
            ValueError, KeyError) as e:
        say(f"エラー: {type(e).__name__}: {e}")
        return 1
    return 0


if __name__ == "__main__":
    sys.exit(main())
