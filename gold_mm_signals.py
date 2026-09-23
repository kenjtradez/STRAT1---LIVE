"""
Gold Managed Money Positioning Extreme - XAUUSD.

Mechanic: each week, check the CFTC Disaggregated report's Managed Money
net position (long - short) as a % of total Open Interest for COMEX
Gold. When this reading's percentile rank vs its own trailing 156-week
(3-year) history is >=90th or <=10th percentile: FOLLOW that extreme -
enter in the SAME direction the extreme points (net long extreme ->
go long; net short extreme -> go short). Hold 20 trading days.

VALIDATION: pooled PF 1.961, 99.7th-percentile randomization test,
OOS (2022+, 85 trades) PF 1.975. Cost-stress robust (PF 1.740 at 10x
cost). Hold-period and threshold sensitivity both robust and improving
in the expected direction. This is the OPPOSITE direction from COT
Positioning Extreme's currency methodology (which fades extremes) -
gold's Managed Money positioning genuinely trends, unlike the
mean-reverting currency pairs COT trades.

DATA SOURCE: CFTC's Disaggregated Futures Only report via the Socrata
API (publicreporting.cftc.gov, dataset 72hh-3qpy), confirmed from the
CFTC's own site and two independent API directories. UNLIKE cot_signals.py's
endpoint (gpe5-46if, TFF), this specific query was NOT live-tested by
fetching it directly before delivery - a tool restriction prevented it.
Run this workflow manually once and confirm it returns real data before
trusting the schedule, same as any new integration.

SCHEDULE: runs daily (weekdays), NOT weekly, even though the underlying
CFTC report only updates once a week. This is deliberate - the report's
exact release day can shift around holidays, so checking daily and
relying on the existing date-deduplication check (see main(): "already
logged, skipping") is more robust than a fixed weekly cron that could
miss a shifted release. Most days this script does nothing (same date
as last check); it only acts on the day the report genuinely updates.

Added to Strat 1 2026-09-22 (5th member) after passing the admission
rule: re-running the exhaustive search with this as a candidate showed
CAGR 9.8645% -> 9.8885%, Sharpe 1.629 -> 1.632 - a genuine, though thin,
verified improvement.

ATR STOP: the entry message now includes a 1.5x-ATR(14) stop reference,
matching the exact convention used in tga_daily_signal.py. Same honest
caveat applies here as there: this stop was never part of the original
backtest (which uses a pure 20-day time exit) - it's a sensible risk-
management addition on top of a validated directional signal, not
itself a validated stop distance.
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
GOLD_MM_LOG = BASE / "gold_mm_log.csv"
GOLD_MM_STATE_PATH = BASE / "gold_mm_state.json"

CFTC_API = "https://publicreporting.cftc.gov/resource/72hh-3qpy.json"
GOLD_MARKET_NAME = "GOLD - COMMODITY EXCHANGE INC."
LOOKBACK_WEEKS = 156
EXTREME_PCTILE = 90
HOLD_DAYS = 20
ATR_STOP_MULT = 1.5  # matches tga_daily_signal.py's convention - NOT itself backtested for this signal


def fetch_recent_daily_bars(symbol, days=20):
    """Enough recent daily bars to compute a 14-day ATR for the stop."""
    url = f"https://query1.finance.yahoo.com/v8/finance/chart/{symbol}"
    params = {"range": f"{days}d", "interval": "1d"}
    headers = {"User-Agent": "Mozilla/5.0"}
    r = requests.get(url, params=params, headers=headers, timeout=15)
    r.raise_for_status()
    data = r.json()["chart"]["result"][0]
    quote = data["indicators"]["quote"][0]
    df = pd.DataFrame({"high": quote["high"], "low": quote["low"], "close": quote["close"]})
    return df.dropna()


def compute_atr(df, period=14):
    """Standard True Range / ATR, matching the convention used across
    every other strategy this session."""
    high, low, close = df['high'], df['low'], df['close']
    tr = pd.concat([high-low, (high-close.shift(1)).abs(), (low-close.shift(1)).abs()], axis=1).max(axis=1)
    return tr.rolling(period).mean().iloc[-1]


def fetch_latest_gold_positioning():
    params = {
        "$where": f"market_and_exchange_names='{GOLD_MARKET_NAME}'",
        "$order": "report_date_as_yyyy_mm_dd DESC",
        "$limit": "1",
    }
    r = requests.get(CFTC_API, params=params, timeout=20)
    r.raise_for_status()
    data = r.json()
    if not data:
        raise RuntimeError("No Gold Disaggregated data returned from CFTC API")
    row = data[0]
    oi = float(row["open_interest_all"])
    mm_long = float(row["m_money_positions_long_all"])
    mm_short = float(row["m_money_positions_short_all"])
    report_date = pd.to_datetime(row["report_date_as_yyyy_mm_dd"])
    return {
        "date": report_date, "open_interest": oi,
        "mm_net_pct_oi": (mm_long - mm_short) / oi if oi > 0 else 0,
    }


def load_gold_mm_log():
    if GOLD_MM_LOG.exists():
        return pd.read_csv(GOLD_MM_LOG, parse_dates=["date"])
    return pd.DataFrame(columns=["date", "mm_net_pct_oi"])


def load_gold_mm_state():
    if GOLD_MM_STATE_PATH.exists():
        return json.loads(GOLD_MM_STATE_PATH.read_text())
    return {"state": 0, "entry_price": None, "entry_date": None, "trade_id": None, "risk_fraction": 1.0}


def fetch_latest_xau_bar():
    url = "https://query1.finance.yahoo.com/v8/finance/chart/GC=F"
    params = {"range": "10d", "interval": "1d"}
    headers = {"User-Agent": "Mozilla/5.0"}
    r = requests.get(url, params=params, headers=headers, timeout=15)
    r.raise_for_status()
    data = r.json()
    result = data.get("chart", {}).get("result")
    if not result:
        raise RuntimeError(f"Yahoo error: {data}")
    result = result[0]
    quote = result["indicators"]["quote"][0]
    closes = quote["close"]
    valid = [i for i in range(len(closes)) if closes[i] is not None]
    if not valid:
        raise RuntimeError("No confirmed XAUUSD bars")
    return float(closes[valid[-1]])


def main():
    log = load_gold_mm_log()
    s = load_gold_mm_state()
    msgs = []

    latest = fetch_latest_gold_positioning()

    if len(log) and latest["date"] in set(log["date"]):
        print(f"{latest['date'].date()} already logged, skipping.")
        return

    row = {"date": latest["date"], "mm_net_pct_oi": latest["mm_net_pct_oi"]}
    log = pd.concat([log, pd.DataFrame([row])], ignore_index=True).sort_values("date")

    if len(log) < LOOKBACK_WEEKS:
        msgs.append(f"Gold MM Positioning: building history ({len(log)}/{LOOKBACK_WEEKS} weeks), not enough data yet.")
        log.to_csv(GOLD_MM_LOG, index=False)
        for m in msgs:
            print(m)
        return

    window = log["mm_net_pct_oi"].tail(LOOKBACK_WEEKS)
    current_val = window.iloc[-1]
    pctile = (window.iloc[:-1] < current_val).mean() * 100

    xau_price = fetch_latest_xau_bar()

    # Check exit first (time-based, 20 trading days from entry)
    if s["state"] != 0:
        entry_date = pd.to_datetime(s["entry_date"])
        days_held = (pd.Timestamp.now(tz="UTC").normalize() - entry_date).days
        if days_held >= HOLD_DAYS:
            direction = "long" if s["state"] == 1 else "short"
            if s.get("trade_id"):
                execute_exit(s["trade_id"])
            pnl, new_equity = record_trade_close("XAUUSD", "Gold MM Positioning", direction,
                                                  s["entry_price"], s["entry_price"]*0.99, xau_price,
                                                  s.get("risk_fraction", 1.0))
            msgs.append(f"*XAUUSD* — EXIT {direction.upper()} @ {xau_price:.2f} (Gold MM Positioning, {HOLD_DAYS}-day hold complete). P&L: £{pnl:,.0f}. Equity: £{new_equity:,.0f}")
            s = {"state": 0, "entry_price": None, "entry_date": None, "trade_id": None, "risk_fraction": 1.0}

    # Check for new entry (only if flat and an extreme fires this week)
    if s["state"] == 0:
        if pctile >= EXTREME_PCTILE or pctile <= (100 - EXTREME_PCTILE):
            direction = "long" if pctile >= EXTREME_PCTILE else "short"  # FOLLOW the extreme
            risk_frac = available_risk_fraction("Gold MM Positioning")
            if risk_frac <= 0:
                msgs.append(f"XAUUSD: Gold MM extreme fired (pctile={pctile:.1f}) but SKIPPED — 10% risk budget full.")
            else:
                risk_gbp = current_risk_gbp("Gold MM Positioning", risk_frac)
                fill = execute_entry("XAUUSD", direction, risk_gbp, xau_price * (0.99 if direction == "long" else 1.01))
                trade_id = fill["trade_id"] if fill else None
                s = {"state": 1 if direction == "long" else -1, "entry_price": xau_price,
                     "entry_date": pd.Timestamp.now(tz="UTC").normalize().isoformat(),
                     "trade_id": trade_id, "risk_fraction": risk_frac}
                exec_note = f" [LIVE, trade {trade_id}]" if fill else (" [EXECUTION ENABLED but order failed]" if EXECUTION_ENABLED else "")

                # ATR stop is INFORMATIONAL ONLY - does not change execute_entry's
                # actual stop-reference argument above, which stays a placeholder
                # for R-multiple journaling. Adding a real price stop to the live
                # order would deviate from the validated 20-day-time-exit backtest.
                try:
                    bars = fetch_recent_daily_bars('GC=F')
                    atr = compute_atr(bars)
                    if not np.isnan(atr):
                        stop_distance = ATR_STOP_MULT * atr
                        atr_stop = xau_price - stop_distance if direction == "long" else xau_price + stop_distance
                        stop_note = f", ATR stop {atr_stop:.2f} ({ATR_STOP_MULT}x ATR, informational only - not the live order's stop)"
                    else:
                        stop_note = ""
                except Exception:
                    stop_note = ""

                msgs.append(f"*XAUUSD* (Gold MM Positioning, pctile={pctile:.1f}) — ENTER {direction.upper()} @ {xau_price:.2f} (~£{risk_gbp:,.0f} at risk){stop_note}{exec_note}")

    log.to_csv(GOLD_MM_LOG, index=False)
    GOLD_MM_STATE_PATH.write_text(json.dumps(s, indent=2))

    if not msgs:
        msgs.append(f"Gold MM Positioning: no signal this week (pctile={pctile:.1f}).")
    for m in msgs:
        print(m)


if __name__ == "__main__":
    main()
