"""
Overnight Extension BUY strategy - XAUUSD, NAS100_USD, SPX500.

Mechanic (matches the backtested/validated version exactly):
- Anchor at 22:00 UTC each day.
- Measure the maximum extension (largest absolute move from the anchor
  price, in EITHER direction) over the following ~24h window (this
  anchor to the next one).
- On the NEXT day, from the NEW 22:00 anchor, set:
    - a buy-stop at anchor_price + extension_distance
    - a sell level at anchor_price - extension_distance (used as the
      stop-loss for the buy position, not traded as its own entry -
      the backtest's separately-tested SELL setup did not validate and
      is NOT part of what's being deployed here)
- If the buy-stop triggers before the next anchor, hold until the
  EARLIER of: the sell level being hit (stop-out), or the next 22:00
  anchor (time-based exit).

Validated results (34-instrument sweep, this 3-instrument BUY-only
subset): PF 1.502 pooled, 100th-percentile randomization test, cost-
stress robust (PF 1.502->1.025 at 5x cost), bootstrap worst-case
drawdown -0.6%, essentially zero correlation (-0.02 to 0.02) with
every other live strategy. This needs HOURLY data (not the daily bars
the other three strategies use) because the 22:00 anchor and intraday
extension tracking can't be reconstructed from daily OHLC alone.

Runs on its own hourly-cadence workflow, sharing the same journal.py,
equity.json, and 10% total-risk budget as everything else.

AUDITED 2026-09-22 for the exact lookahead-bug pattern that broke
ADX+Supertrend and QM+CISD+SBR in the original repo: confirmed the
entry check (elif) never fires on the same bar that sets buy_level (if
cur_hour==ANCHOR_HOUR) - a level derived from a bar's own close is only
ever checked against later bars' highs, never that same bar's. No
lookahead found.
"""
import json
from pathlib import Path

import numpy as np
import pandas as pd
import requests

from journal import (
    record_trade_close, available_risk_fraction, current_risk_gbp, get_risk_pct,
)
from execution import execute_entry, execute_exit, EXECUTION_ENABLED

BASE = Path(__file__).parent
OVERNIGHT_LOG = BASE / "overnight_extension_log.csv"
OVERNIGHT_STATE_PATH = BASE / "overnight_extension_state.json"

OVERNIGHT_INSTRUMENTS = {
    'XAUUSD': 'GC=F', 'NAS100_USD': '^NDX', 'SPX500': '^GSPC',
}
ANCHOR_HOUR = 22  # UTC


def fetch_latest_hourly_bar(symbol):
    url = f"https://query1.finance.yahoo.com/v8/finance/chart/{symbol}"
    params = {"range": "5d", "interval": "1h"}
    headers = {"User-Agent": "Mozilla/5.0"}
    r = requests.get(url, params=params, headers=headers, timeout=15)
    r.raise_for_status()
    data = r.json()
    result = data.get("chart", {}).get("result")
    if not result:
        raise RuntimeError(f"Yahoo error: {data}")
    result = result[0]
    timestamps = result["timestamp"]
    quote = result["indicators"]["quote"][0]
    closes, highs, lows, opens = quote["close"], quote["high"], quote["low"], quote["open"]
    valid = [i for i in range(len(closes)) if closes[i] is not None]
    if len(valid) < 2:
        raise RuntimeError("Not enough confirmed hourly bars")
    latest_i = valid[-1]
    dt = pd.to_datetime(timestamps[latest_i], unit="s", utc=True)
    return {
        "datetime": dt, "close": float(closes[latest_i]),
        "high": float(highs[latest_i]), "low": float(lows[latest_i]),
        "open": float(opens[latest_i]),
    }


def load_hourly_log(path):
    if not path.exists():
        raise FileNotFoundError(f"{path} not found - seed it before first run.")
    df = pd.read_csv(path, parse_dates=["datetime"])
    if df["datetime"].dt.tz is None:
        df["datetime"] = df["datetime"].dt.tz_localize("UTC")
    return df


def load_overnight_state():
    if OVERNIGHT_STATE_PATH.exists():
        return json.loads(OVERNIGHT_STATE_PATH.read_text())
    return {inst: {"state": 0, "entry_price": None, "buy_level": None, "sell_level": None,
                    "anchor_price": None, "trade_id": None, "risk_fraction": 1.0}
            for inst in OVERNIGHT_INSTRUMENTS}


def compute_extension(inst_log, anchor_time_prev, anchor_time_cur):
    """Max absolute move from the PRIOR anchor's price over the window
    strictly between the prior and current anchor - fully known by the
    time the current anchor is reached."""
    window = inst_log[(inst_log["datetime"] > anchor_time_prev) & (inst_log["datetime"] <= anchor_time_cur)]
    if len(window) < 2:
        return None
    anchor_row = inst_log[inst_log["datetime"] == anchor_time_prev]
    if len(anchor_row) == 0:
        return None
    anchor_price = anchor_row["close"].iloc[0]
    max_up = window["high"].max() - anchor_price
    max_down = anchor_price - window["low"].min()
    return max(max_up, max_down)


def process_overnight_extension_all(state, msgs, open_counter):
    log = load_hourly_log(OVERNIGHT_LOG) if OVERNIGHT_LOG.exists() else pd.DataFrame(columns=["instrument", "datetime", "open", "high", "low", "close"])
    overnight_state = state.get("overnight_extension", load_overnight_state())

    for inst, yahoo_symbol in OVERNIGHT_INSTRUMENTS.items():
        bar = fetch_latest_hourly_bar(yahoo_symbol)
        inst_log = log[log["instrument"] == inst]

        if len(inst_log) and (inst_log["datetime"] == bar["datetime"]).any():
            continue

        row = {"instrument": inst, "datetime": bar["datetime"], "open": bar["open"],
               "high": bar["high"], "low": bar["low"], "close": bar["close"]}
        log = pd.concat([log, pd.DataFrame([row])], ignore_index=True)
        inst_log = log[log["instrument"] == inst].sort_values("datetime")

        s = overnight_state.get(inst, {"state": 0, "entry_price": None, "buy_level": None,
                                        "sell_level": None, "anchor_price": None,
                                        "trade_id": None, "risk_fraction": 1.0})
        cur_hour = bar["datetime"].hour
        close, high, low = bar["close"], bar["high"], bar["low"]

        if s["state"] == 1:
            if low <= s["sell_level"]:
                if s.get("trade_id"):
                    execute_exit(s["trade_id"])
                pnl, new_equity = record_trade_close(inst, "Overnight Extension", "long", s["entry_price"], s["sell_level"], s["sell_level"], s.get("risk_fraction", 1.0))
                msgs.append(f"*{inst}* — STOPPED OUT @ {s['sell_level']:.5f} (Overnight Extension). P&L: £{pnl:,.0f}. Equity: £{new_equity:,.0f}")
                s.update({"state": 0, "entry_price": None, "buy_level": None, "sell_level": None, "trade_id": None, "risk_fraction": 1.0})
            elif cur_hour == ANCHOR_HOUR:
                if s.get("trade_id"):
                    execute_exit(s["trade_id"])
                pnl, new_equity = record_trade_close(inst, "Overnight Extension", "long", s["entry_price"], s["sell_level"], close, s.get("risk_fraction", 1.0))
                msgs.append(f"*{inst}* — EXIT (next anchor) @ {close:.5f} (Overnight Extension). P&L: £{pnl:,.0f}. Equity: £{new_equity:,.0f}")
                s.update({"state": 0, "entry_price": None, "buy_level": None, "sell_level": None, "trade_id": None, "risk_fraction": 1.0})

        if cur_hour == ANCHOR_HOUR:
            anchors = inst_log[inst_log["datetime"].dt.hour == ANCHOR_HOUR]["datetime"].tolist()
            if len(anchors) >= 2:
                prev_anchor, this_anchor = anchors[-2], anchors[-1]
                extension = compute_extension(inst_log, prev_anchor, this_anchor)
                if extension is not None and extension > 0:
                    s["anchor_price"] = close
                    s["buy_level"] = close + extension
                    s["sell_level"] = close - extension
                    msgs.append(f"{inst}: new anchor @ {close:.5f}, buy-stop {s['buy_level']:.5f}, sell-level {s['sell_level']:.5f}")
                else:
                    s["buy_level"] = None
            else:
                msgs.append(f"{inst}: building anchor history, not enough data yet.")

        elif s["state"] == 0 and s.get("buy_level") is not None:
            if high >= s["buy_level"]:
                risk_frac = available_risk_fraction("Overnight Extension", open_counter[0])
                if risk_frac <= 0:
                    msgs.append(f"{inst}: BUY signal fired but SKIPPED — 10% risk budget full.")
                else:
                    entry_price = s["buy_level"]
                    risk_gbp = current_risk_gbp("Overnight Extension", risk_frac)
                    fill = execute_entry(inst, "long", risk_gbp, s["sell_level"])
                    trade_id = fill["trade_id"] if fill else None
                    s.update({"state": 1, "entry_price": entry_price, "trade_id": trade_id, "risk_fraction": risk_frac})
                    open_counter[0] += get_risk_pct("Overnight Extension")
                    exec_note = f" [LIVE, trade {trade_id}]" if fill else (" [EXECUTION ENABLED but order failed]" if EXECUTION_ENABLED else "")
                    frac_note = f" [{risk_frac:.0%} of full slice]" if risk_frac < 1.0 else ""
                    msgs.append(f"*{inst}* — ENTER LONG @ {entry_price:.5f} (Overnight Extension), stop {s['sell_level']:.5f} (~£{risk_gbp:,.0f} at risk){exec_note}{frac_note}")

        overnight_state[inst] = s

    state["overnight_extension"] = overnight_state
    trimmed = [log[log["instrument"] == inst].sort_values("datetime").tail(240) for inst in OVERNIGHT_INSTRUMENTS]
    log = pd.concat(trimmed, ignore_index=True)
    return state, log


def main():
    state = load_overnight_state()
    full_state = {"overnight_extension": state}
    msgs = []
    open_counter = [0]
    full_state, log = process_overnight_extension_all(full_state, msgs, open_counter)
    log.to_csv(OVERNIGHT_LOG, index=False)
    OVERNIGHT_STATE_PATH.write_text(json.dumps(full_state["overnight_extension"], indent=2))
    for m in msgs:
        print(m)


if __name__ == "__main__":
    main()
