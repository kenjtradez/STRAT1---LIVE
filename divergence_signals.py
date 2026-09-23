"""
Yield Divergence-Fade Strategy - AUDUSD, USDJPY, GBPUSD, NZDUSD.

Mechanic (matches the validated backtest exactly):
- Rolling regression (60-day lookback) of each pair's daily return on the
  2-year Treasury yield's daily change gives the 'normal' relationship
  (beta) between that currency and short-term US rates.
- The residual (actual return minus what the yield relationship predicts)
  measures real-time divergence from that normal relationship.
- A 20-day rolling sum of residuals, Z-scored against its own trailing
  252-day history, flags genuine extremes.
- When |Z| > 1.5: FADE the divergence (bet on reversion toward
  realignment) for 5 trading days.

Validated results: PF 1.216 pooled across the 4 pairs, 97.6th-percentile
randomization test, cost-stress robust (PF 1.322->1.134 at 5x cost),
essentially zero correlation (-0.09 to +0.05) with every other live
strategy - the most diversifying addition found this session.

Yield data comes from FRED's public CSV endpoint (fredgraph.csv), which
needs no API key. Runs on the same daily cadence as the other daily
strategies, sharing the same journal, equity, and 10% risk budget.
"""
import json
from pathlib import Path
from io import StringIO

import numpy as np
import pandas as pd
import requests

from journal import (
    record_trade_close, available_risk_fraction, current_risk_gbp, get_risk_pct,
)
from execution import execute_entry, execute_exit, EXECUTION_ENABLED

BASE = Path(__file__).parent
DIVERGENCE_LOG = BASE / "divergence_log.csv"
DIVERGENCE_STATE_PATH = BASE / "divergence_state.json"
YIELD_LOG = BASE / "dgs2_log.csv"

DIVERGENCE_INSTRUMENTS = {
    'AUDUSD': 'AUDUSD=X', 'USDJPY': 'USDJPY=X', 'GBPUSD': 'GBPUSD=X', 'NZDUSD': 'NZDUSD=X',
}
LOOKBACK = 60      # rolling regression window for beta
ROLL_WINDOW = 20   # rolling residual-sum window
Z_HISTORY = 252    # trailing window for the Z-score's own mean/std
Z_THRESHOLD = 1.5
HOLD_DAYS = 5


def fetch_latest_daily_bar(symbol):
    url = f"https://query1.finance.yahoo.com/v8/finance/chart/{symbol}"
    params = {"range": "5d", "interval": "1d"}
    headers = {"User-Agent": "Mozilla/5.0"}
    r = requests.get(url, params=params, headers=headers, timeout=15)
    r.raise_for_status()
    data = r.json()["chart"]["result"][0]
    timestamps = data["timestamp"]
    closes = data["indicators"]["quote"][0]["close"]
    valid = [i for i in range(len(closes)) if closes[i] is not None]
    latest_i = valid[-1]
    dt = pd.to_datetime(timestamps[latest_i], unit="s", utc=True).date()
    return {"date": dt, "close": float(closes[latest_i])}


def fetch_latest_dgs2():
    """FRED's public CSV endpoint - no API key needed."""
    url = "https://fred.stlouisfed.org/graph/fredgraph.csv?id=DGS2"
    r = requests.get(url, timeout=15)
    r.raise_for_status()
    df = pd.read_csv(StringIO(r.text))
    df.columns = ['date', 'DGS2']
    df['date'] = pd.to_datetime(df['date'])  # keep as Timestamp, consistent with load_yield_log
    df['DGS2'] = pd.to_numeric(df['DGS2'], errors='coerce')
    df = df.dropna().tail(10)  # just need the last few observations
    return df


def load_price_log():
    if DIVERGENCE_LOG.exists():
        return pd.read_csv(DIVERGENCE_LOG, parse_dates=["date"])
    return pd.DataFrame(columns=["instrument", "date", "close"])


def load_yield_log():
    if YIELD_LOG.exists():
        return pd.read_csv(YIELD_LOG, parse_dates=["date"])
    return pd.DataFrame(columns=["date", "DGS2"])


def load_state():
    if DIVERGENCE_STATE_PATH.exists():
        return json.loads(DIVERGENCE_STATE_PATH.read_text())
    return {inst: {"state": 0, "entry_price": None, "entry_date": None,
                    "trade_id": None, "risk_fraction": 1.0} for inst in DIVERGENCE_INSTRUMENTS}


def compute_divergence_z(price_log, yield_log):
    """Rebuild the full causal pipeline: rolling beta, residual, rolling
    sum, Z-score - all using only data through the current row, matching
    the backtested methodology exactly."""
    df = price_log.set_index('date')[['close']].join(
        yield_log.set_index('date')[['DGS2']], how='inner').sort_index()
    df['yield_chg'] = df['DGS2'].diff(1)
    ret = df['close'].pct_change()

    betas = pd.Series(index=df.index, dtype=float)
    for i in range(LOOKBACK, len(df)):
        y = ret.iloc[i-LOOKBACK:i].values
        x = df['yield_chg'].iloc[i-LOOKBACK:i].values
        if np.std(x) > 0:
            betas.iloc[i] = np.cov(x, y)[0, 1] / np.var(x)

    predicted_ret = betas.shift(1) * df['yield_chg']
    residual = ret - predicted_ret
    cum_residual = residual.rolling(ROLL_WINDOW).sum()
    z_score = (cum_residual - cum_residual.rolling(Z_HISTORY).mean()) / cum_residual.rolling(Z_HISTORY).std()
    return z_score


def process_divergence_all(state, msgs, open_counter):
    price_log = load_price_log()
    yield_log = load_yield_log()
    divergence_state = state.get("divergence", load_state())

    dgs2_new = fetch_latest_dgs2()
    yield_log = pd.concat([yield_log, dgs2_new], ignore_index=True).drop_duplicates(subset='date', keep='last').sort_values('date')

    for inst, yahoo_symbol in DIVERGENCE_INSTRUMENTS.items():
        bar = fetch_latest_daily_bar(yahoo_symbol)
        inst_log = price_log[price_log["instrument"] == inst]

        if len(inst_log) and (inst_log["date"] == pd.Timestamp(bar["date"])).any():
            continue

        row = {"instrument": inst, "date": pd.Timestamp(bar["date"]), "close": bar["close"]}
        price_log = pd.concat([price_log, pd.DataFrame([row])], ignore_index=True)
        inst_log = price_log[price_log["instrument"] == inst].sort_values("date")

        s = divergence_state.get(inst, {"state": 0, "entry_price": None, "entry_date": None,
                                         "trade_id": None, "risk_fraction": 1.0})

        inst_price_only = inst_log[['date', 'close']]
        z_series = compute_divergence_z(inst_price_only, yield_log)
        if len(z_series) < LOOKBACK + ROLL_WINDOW + Z_HISTORY:
            msgs.append(f"{inst} (Divergence-Fade): building history, not enough data yet.")
            divergence_state[inst] = s
            continue

        cur_z = z_series.iloc[-1]
        cur_date = pd.Timestamp(bar["date"])
        close = bar["close"]

        if np.isnan(cur_z):
            msgs.append(f"{inst} (Divergence-Fade, Z=n/a): {'HOLD' if s['state']!=0 else 'FLAT'} @ {close:.5f}")
            divergence_state[inst] = s
            continue

        if s["state"] != 0:
            # days_held counted by ACTUAL CALENDAR/TRADING DATES in the log,
            # not a positional index - a positional index breaks once the
            # log gets trimmed between runs, since trimming shifts every
            # row's position without changing what date it represents.
            entry_date = pd.Timestamp(s["entry_date"])
            days_held = (inst_price_only['date'] > entry_date).sum()
            if days_held >= HOLD_DAYS:
                direction = "long" if s["state"] == 1 else "short"
                if s.get("trade_id"):
                    execute_exit(s["trade_id"])
                pnl, new_equity = record_trade_close(inst, "Divergence-Fade", direction, s["entry_price"], s["entry_price"]*0.99, close, s.get("risk_fraction", 1.0))
                msgs.append(f"*{inst}* — EXIT {direction.upper()} @ {close:.5f} (Divergence-Fade, {HOLD_DAYS}-day hold complete). P&L: £{pnl:,.0f}. Equity: £{new_equity:,.0f}")
                s.update({"state": 0, "entry_price": None, "entry_date": None, "trade_id": None, "risk_fraction": 1.0})
            else:
                action = "HOLD LONG" if s["state"] == 1 else "HOLD SHORT"
                msgs.append(f"{inst} (Divergence-Fade, Z={cur_z:.2f}): {action} @ {close:.5f} (day {days_held+1}/{HOLD_DAYS})")

        elif abs(cur_z) > Z_THRESHOLD:
            direction = "short" if cur_z > 0 else "long"  # FADE the divergence
            risk_frac = available_risk_fraction("Divergence-Fade", open_counter[0])
            if risk_frac <= 0:
                msgs.append(f"{inst}: divergence signal fired (Z={cur_z:.2f}) but SKIPPED — 10% risk budget full.")
            else:
                risk_ref = close * (0.99 if direction == "long" else 1.01)
                risk_gbp = current_risk_gbp("Divergence-Fade", risk_frac)
                fill = execute_entry(inst, direction, risk_gbp, risk_ref)
                trade_id = fill["trade_id"] if fill else None
                s.update({"state": 1 if direction == "long" else -1, "entry_price": close,
                          "entry_date": cur_date.isoformat(), "trade_id": trade_id, "risk_fraction": risk_frac})
                open_counter[0] += get_risk_pct("Divergence-Fade")
                exec_note = f" [LIVE, trade {trade_id}]" if fill else (" [EXECUTION ENABLED but order failed]" if EXECUTION_ENABLED else "")
                frac_note = f" [{risk_frac:.0%} of full slice]" if risk_frac < 1.0 else ""
                msgs.append(f"*{inst}* (Divergence-Fade, Z={cur_z:.2f}) — ENTER {direction.upper()} @ {close:.5f} (~£{risk_gbp:,.0f} at risk){exec_note}{frac_note}")
        else:
            msgs.append(f"{inst} (Divergence-Fade, Z={cur_z:.2f}): FLAT @ {close:.5f}")

        divergence_state[inst] = s

    state["divergence"] = divergence_state
    # Trim to 600, not 400 - given FX/bond markets only overlap on ~80% of
    # calendar days, 400 raw rows was proven (in testing) to yield only
    # ~321 genuinely overlapping dates, just short of the 332 minimum this
    # strategy needs (LOOKBACK+ROLL_WINDOW+Z_HISTORY). 600 gives a safe buffer.
    trimmed = [price_log[price_log["instrument"] == inst].sort_values("date").tail(600) for inst in DIVERGENCE_INSTRUMENTS]
    price_log = pd.concat(trimmed, ignore_index=True)
    yield_log = yield_log.tail(600)
    return state, price_log, yield_log


def send_telegram(msg):
    import os
    token = os.environ.get("TELEGRAM_BOT_TOKEN")
    chat_id = os.environ.get("TELEGRAM_CHAT_ID")
    if not token or not chat_id:
        print("[warn] Telegram not configured:\n" + msg)
        return
    url = f"https://api.telegram.org/bot{token}/sendMessage"
    try:
        r = requests.post(url, data={"chat_id": chat_id, "text": msg}, timeout=15)
        if r.status_code != 200:
            print(f"[ERROR] Telegram send failed: HTTP {r.status_code} - {r.text}")
        else:
            print("[ok] Telegram message sent successfully.")
    except Exception as e:
        print(f"[ERROR] Telegram send raised an exception: {e}")


def main():
    state_path = BASE / "divergence_full_state.json"
    full_state = json.loads(state_path.read_text()) if state_path.exists() else {}
    msgs = []
    open_counter = [0]

    full_state, price_log, yield_log = process_divergence_all(full_state, msgs, open_counter)

    price_log.to_csv(DIVERGENCE_LOG, index=False)
    yield_log.to_csv(YIELD_LOG, index=False)
    state_path.write_text(json.dumps(full_state, indent=2))

    full_message = "\n".join(msgs)
    send_telegram(full_message)
    print(full_message)


if __name__ == "__main__":
    main()
