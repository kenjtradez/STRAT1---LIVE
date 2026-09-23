"""
NAS100 Pivot S/R (long-only, vol-targeted) — daily forward-test signal.

Signal logic (pivot/resistance/vol-scale) is UNCHANGED from the original
standalone version - only the integration layer is new: this now calls
the shared journal.py (record_trade_close, available_risk_fraction,
current_risk_gbp) and execution.py, exactly like the other three Strat 1
strategies, instead of running its own separate state/sizing/logging.

AUDITED 2026-09-22 for lookahead bias: pivot and resistance are derived
strictly from the PRIOR day's high/low/close (prior_i = valid_idx[-2]);
today's signal only compares today's own confirmed close against those
prior-day levels. No lookahead found.

INTEGRATION NOTE: the original version priced its own "position_pct"
using vol_scale directly (0.3x-2.0x of a 100% base) - that sizing is
NOT used here. Position sizing now goes entirely through journal.py's
shared risk-budget system (STRATEGY_RISK_PCT["Pivot S/R"], the 10%
total-open-risk cap, and the 5x-starting-capital compounding cap) so
this strategy's risk is genuinely visible to and shares the same budget
as COT, Divergence-Fade, and Overnight Extension. vol_scale is no
longer used for position sizing - if you want it to influence sizing
again, that needs a deliberate decision, not a silent carry-over.

Runs once per day, after the NAS100 daily close. Requires env vars:
  TELEGRAM_BOT_TOKEN
  TELEGRAM_CHAT_ID
"""
import os
import json
import requests
import pandas as pd
from pathlib import Path

from journal import (
    record_trade_close, available_risk_fraction, current_risk_gbp, get_risk_pct,
)
from execution import execute_entry, execute_exit, EXECUTION_ENABLED

LOG_PATH = Path(__file__).parent / "nas100_signal_log.csv"
STATE_PATH = Path(__file__).parent / "nas100_state.json"

VOL_LOOKBACK = 20
MEDIAN_LOOKBACK = 500
SCALE_MIN, SCALE_MAX = 0.3, 2.0

TG_TOKEN = os.environ.get("TELEGRAM_BOT_TOKEN")
TG_CHAT_ID = os.environ.get("TELEGRAM_CHAT_ID")
SYMBOL = os.environ.get("SIGNAL_SYMBOL", "^NDX")


def fetch_latest_daily_bar(symbol):
    url = f"https://query1.finance.yahoo.com/v8/finance/chart/{symbol}"
    params = {"range": "10d", "interval": "1d"}
    headers = {"User-Agent": "Mozilla/5.0"}
    r = requests.get(url, params=params, headers=headers, timeout=15)
    r.raise_for_status()
    data = r.json()
    result = data.get("chart", {}).get("result")
    if not result:
        raise RuntimeError(f"Yahoo Finance error: {data}")
    result = result[0]
    timestamps = result["timestamp"]
    quote = result["indicators"]["quote"][0]
    closes, highs, lows = quote["close"], quote["high"], quote["low"]
    valid_idx = [i for i in range(len(closes)) if closes[i] is not None]
    if len(valid_idx) < 2:
        raise RuntimeError("Not enough confirmed daily bars returned from Yahoo Finance")
    latest_i, prior_i = valid_idx[-1], valid_idx[-2]
    latest_date = pd.to_datetime(timestamps[latest_i], unit="s").strftime("%Y-%m-%d")
    return {
        "date": latest_date, "close": float(closes[latest_i]),
        "prior_high": float(highs[prior_i]), "prior_low": float(lows[prior_i]),
        "prior_close": float(closes[prior_i]),
    }


def load_log():
    if LOG_PATH.exists():
        return pd.read_csv(LOG_PATH, parse_dates=["date"])
    raise FileNotFoundError(f"{LOG_PATH} not found - seed it before first run.")


def load_pivot_state():
    if STATE_PATH.exists():
        return json.loads(STATE_PATH.read_text())
    return {"state": 0, "entry_price": None, "risk_reference_price": None,
            "trade_id": None, "risk_fraction": 1.0}


def send_telegram(msg):
    if not TG_TOKEN or not TG_CHAT_ID:
        print("[warn] Telegram not configured, skipping alert. Message was:\n" + msg)
        return
    url = f"https://api.telegram.org/bot{TG_TOKEN}/sendMessage"
    requests.post(url, data={"chat_id": TG_CHAT_ID, "text": msg, "parse_mode": "Markdown"})


def main():
    log = load_log()
    s = load_pivot_state()
    bar = fetch_latest_daily_bar(SYMBOL)

    if pd.to_datetime(bar["date"]) in set(log["date"]):
        print(f"{bar['date']} already logged, skipping.")
        return

    pivot = (bar["prior_high"] + bar["prior_low"] + bar["prior_close"]) / 3
    resistance = 2 * pivot - bar["prior_low"]
    close = bar["close"]

    recent_closes = pd.concat([log["close"], pd.Series([close])], ignore_index=True)
    rets = recent_closes.pct_change()
    realized_vol = rets.tail(VOL_LOOKBACK).std()
    all_vols = pd.concat([
        (log["close"].pct_change()).rolling(VOL_LOOKBACK).std(),
        pd.Series([realized_vol])
    ], ignore_index=True)
    median_vol = all_vols.tail(MEDIAN_LOOKBACK).median()
    vol_scale = median_vol / realized_vol if realized_vol > 0 else 1.0
    vol_scale = min(max(vol_scale, SCALE_MIN), SCALE_MAX)

    msgs = []
    action = "HOLD"

    if s["state"] == 0 and close > pivot:
        risk_frac = available_risk_fraction("Pivot S/R")
        if risk_frac <= 0:
            action = "SIGNAL FIRED BUT SKIPPED - 10% risk budget full"
            msgs.append(f"NAS100: ENTER LONG signal fired but SKIPPED — 10% risk budget full.")
        else:
            risk_gbp = current_risk_gbp("Pivot S/R", risk_frac)
            # No natural stop - risk_reference_price is a placeholder for
            # R-multiple journaling only (see journal.py's own note on this
            # pattern for target/reversal-exit strategies); does not change
            # entry/exit logic.
            risk_reference_price = close * 0.99
            fill = execute_entry("NAS100_USD", "long", risk_gbp, risk_reference_price)
            trade_id = fill["trade_id"] if fill else None
            s.update({"state": 1, "entry_price": close, "risk_reference_price": risk_reference_price,
                       "trade_id": trade_id, "risk_fraction": risk_frac})
            action = "ENTER LONG"
            exec_note = f" [LIVE, trade {trade_id}]" if fill else (" [EXECUTION ENABLED but order failed]" if EXECUTION_ENABLED else "")
            msgs.append(f"*NAS100* — ENTER LONG @ {close:.1f} (Pivot S/R) (~£{risk_gbp:,.0f} at risk){exec_note}")

    elif s["state"] == 1 and close >= resistance:
        if s.get("trade_id"):
            execute_exit(s["trade_id"])
        pnl, new_equity = record_trade_close("NAS100_USD", "Pivot S/R", "long",
                                              s["entry_price"], s["risk_reference_price"], close,
                                              s.get("risk_fraction", 1.0))
        action = "EXIT (hit resistance)"
        msgs.append(f"*NAS100* — EXIT @ {close:.1f} (Pivot S/R, hit resistance). P&L: £{pnl:,.0f}. Equity: £{new_equity:,.0f}")
        s.update({"state": 0, "entry_price": None, "risk_reference_price": None,
                   "trade_id": None, "risk_fraction": 1.0})

    elif s["state"] == 1:
        action = "HOLD LONG"
    else:
        action = "FLAT"

    row = {
        "date": bar["date"], "close": close, "pivot": pivot, "resistance": resistance,
        "realized_vol_20d": realized_vol, "median_vol_500d": median_vol,
        "vol_scale": vol_scale, "state": s["state"],
    }
    log = pd.concat([log, pd.DataFrame([row])], ignore_index=True)
    log.to_csv(LOG_PATH, index=False)
    STATE_PATH.write_text(json.dumps(s, indent=2))

    if not msgs:
        msgs.append(f"NAS100 Pivot S/R — {bar['date']}: {action} @ {close:.1f} (pivot {pivot:.1f}, resistance {resistance:.1f})")
    for m in msgs:
        send_telegram(m)
        print(m)


if __name__ == "__main__":
    main()
