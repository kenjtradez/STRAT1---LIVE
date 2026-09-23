"""
COT Positioning-Extreme Contrarian Strategy - AUDUSD, GBPUSD, NZDUSD.

UPGRADED (2026-09-20) from Legacy Non-Commercial to TFF Leveraged Money:
backtested this more precise data (hedge-fund-specific positioning,
via CFTC's Traders in Financial Futures report) and found it genuinely
stronger than the original Legacy Non-Commercial version - PF 1.581
pooled vs the original's 1.194-1.627 depending on scope, all three
instruments' IS/OOS held up (AUDUSD 1.212->1.353, NZDUSD 1.672->1.759,
GBPUSD 4.061->1.529 - IS looks outlier-inflated but OOS confirms real).
99.6th-percentile randomization test, robust to 5x cost stress (1.447).

IMPORTANT: this REPLACES the Legacy data source, it does not add a
second strategy - the two are ~0.49 correlated (Leveraged Money is a
subset of the broader Non-Commercial category), so running both would
double-count similar risk, not diversify it.

Mechanic (otherwise unchanged): each week, fetch CFTC's TFF Futures-Only
report via their public Socrata API (published Fridays, covering the
prior Tuesday's positions). Compute Leveraged Money (hedge fund) net
positioning as % of open interest, then its percentile rank against its
own trailing 156-week (3-year) history. When that percentile crosses
above 90 or below 10: FADE it. Hold 20 trading days, then exit
regardless of price.

RISK SIZING: this account has a hard 6% max-drawdown limit (funded
account). Sized conservatively, matching the same safety-scaling logic
already applied to the other live strategies - see journal.py's
STRATEGY_RISK_PCT for the exact value and its derivation.

BUG FIXED 2026-09-22: the TFF API's actual field names are
'lev_money_positions_long' and 'lev_money_positions_short' - WITHOUT
an '_all' suffix, unlike 'open_interest_all' which does have one. An
earlier version of this script incorrectly included '_all' on the
lev_money fields (an assumption never verified against a live API
call), causing a KeyError in production. Confirmed directly against
the live API before this fix - the Disaggregated report used for
gold_mm_signals.py is different: it genuinely does use '_all' on its
equivalent fields ('m_money_positions_long_all'), so that script was
never affected by this same bug.
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
COT_LOG = BASE / "cot_log.csv"
COT_STATE_PATH = BASE / "cot_state.json"

TFF_API_URL = "https://publicreporting.cftc.gov/resource/gpe5-46if.json"

COT_INSTRUMENTS = {
    'AUDUSD': {'market_name': 'AUSTRALIAN DOLLAR - CHICAGO MERCANTILE EXCHANGE', 'yahoo': 'AUDUSD=X'},
    'GBPUSD': {'market_name': 'BRITISH POUND - CHICAGO MERCANTILE EXCHANGE', 'yahoo': 'GBPUSD=X'},
    'NZDUSD': {'market_name': 'NZ DOLLAR - CHICAGO MERCANTILE EXCHANGE', 'yahoo': 'NZDUSD=X'},
}
LOOKBACK_WEEKS = 156
EXTREME_PCTILE = 90
HOLD_DAYS = 20


def fetch_latest_tff(market_name, limit=200):
    """CFTC's public Socrata API for the TFF Futures-Only report - no
    API key needed, keyless/unauthenticated tier. Returns the most
    recent 'limit' weekly reports for the given market."""
    params = {
        "market_and_exchange_names": market_name,
        "$order": "report_date_as_yyyy_mm_dd DESC",
        "$limit": limit,
    }
    r = requests.get(TFF_API_URL, params=params, timeout=30)
    r.raise_for_status()
    return pd.DataFrame(r.json())


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


def load_cot_log():
    if COT_LOG.exists():
        return pd.read_csv(COT_LOG, parse_dates=["date"])
    return pd.DataFrame(columns=["instrument", "date", "noncomm_net_pct_oi"])


def load_price_log(inst):
    path = BASE / f"cot_price_{inst}.csv"
    if path.exists():
        return pd.read_csv(path, parse_dates=["date"])
    return pd.DataFrame(columns=["date", "close"])


def load_state():
    if COT_STATE_PATH.exists():
        return json.loads(COT_STATE_PATH.read_text())
    return {inst: {"state": 0, "entry_price": None, "entry_date": None,
                    "trade_id": None, "risk_fraction": 1.0} for inst in COT_INSTRUMENTS}


def refresh_cot_data(cot_log):
    """Pull the latest TFF reports for each of our 3 instruments via the
    Socrata API, using Leveraged Money positioning (hedge funds) rather
    than the broader legacy Non-Commercial category."""
    new_rows = []
    for inst, cfg in COT_INSTRUMENTS.items():
        try:
            df = fetch_latest_tff(cfg['market_name'])
        except Exception as e:
            print(f"[warn] Could not fetch TFF data for {inst}: {e}")
            continue
        if df.empty:
            continue
        for col in ['lev_money_positions_long', 'lev_money_positions_short', 'open_interest_all']:
            df[col] = pd.to_numeric(df[col], errors='coerce')
        df['lev_net_pct_oi'] = (df['lev_money_positions_long'] - df['lev_money_positions_short']) / df['open_interest_all']
        for _, row in df.iterrows():
            new_rows.append({'instrument': inst, 'date': pd.to_datetime(row['report_date_as_yyyy_mm_dd']),
                              'noncomm_net_pct_oi': row['lev_net_pct_oi']})
    if new_rows:
        new_df = pd.DataFrame(new_rows).dropna()
        cot_log = pd.concat([cot_log, new_df], ignore_index=True).drop_duplicates(subset=['instrument', 'date']).sort_values(['instrument', 'date'])
    return cot_log


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


def process_cot_all(state, msgs, open_counter):
    cot_log = load_cot_log()
    cot_log = refresh_cot_data(cot_log)
    cot_state = state.get("cot", load_state())

    for inst, cfg in COT_INSTRUMENTS.items():
        bar = fetch_latest_daily_bar(cfg['yahoo'])
        price_log = load_price_log(inst)
        if len(price_log) and (price_log["date"] == pd.Timestamp(bar["date"])).any():
            continue
        price_log = pd.concat([price_log, pd.DataFrame([{"date": pd.Timestamp(bar["date"]), "close": bar["close"]}])], ignore_index=True)
        price_log = price_log.tail(400)
        price_log.to_csv(BASE / f"cot_price_{inst}.csv", index=False)

        s = cot_state.get(inst, {"state": 0, "entry_price": None, "entry_date": None,
                                  "trade_id": None, "risk_fraction": 1.0})
        close = bar["close"]
        cur_date = pd.Timestamp(bar["date"])

        # === Exit check: 20 trading days since entry ===
        if s["state"] != 0:
            entry_date = pd.Timestamp(s["entry_date"])
            days_held = (price_log['date'] > entry_date).sum()
            if days_held >= HOLD_DAYS:
                direction = "long" if s["state"] == 1 else "short"
                if s.get("trade_id"):
                    execute_exit(s["trade_id"])
                pnl, new_equity = record_trade_close(inst, "COT Positioning Extreme", direction, s["entry_price"], s["entry_price"]*0.99, close, s.get("risk_fraction", 1.0))
                msgs.append(f"*{inst}* — EXIT {direction.upper()} @ {close:.5f} (COT, {HOLD_DAYS}-day hold complete). P&L: £{pnl:,.0f}. Equity: £{new_equity:,.0f}")
                s.update({"state": 0, "entry_price": None, "entry_date": None, "trade_id": None, "risk_fraction": 1.0})
                cot_state[inst] = s
                continue
            else:
                action = "HOLD LONG" if s["state"] == 1 else "HOLD SHORT"
                msgs.append(f"{inst} (COT): {action} @ {close:.5f} (day {days_held+1}/{HOLD_DAYS})")
                cot_state[inst] = s
                continue

        # === Entry check: only when flat, using latest COT percentile ===
        inst_cot = cot_log[cot_log['instrument'] == inst].sort_values('date')
        if len(inst_cot) < LOOKBACK_WEEKS + 1:
            msgs.append(f"{inst} (COT): building history ({len(inst_cot)}/{LOOKBACK_WEEKS+1} weeks).")
            cot_state[inst] = s
            continue

        series = inst_cot.set_index('date')['noncomm_net_pct_oi']
        latest_val = series.iloc[-1]
        trailing = series.iloc[-(LOOKBACK_WEEKS+1):-1]
        pctile = (trailing < latest_val).mean() * 100

        # Real-world publish lag: only act once the report is at least 3
        # days old (Tue report -> Fri publish), matching the backtest
        latest_report_date = inst_cot['date'].iloc[-1]
        if (cur_date - latest_report_date).days < 3:
            msgs.append(f"{inst} (COT): latest report too recent to act on yet.")
            cot_state[inst] = s
            continue

        if pctile >= EXTREME_PCTILE or pctile <= (100 - EXTREME_PCTILE):
            direction = "short" if pctile >= EXTREME_PCTILE else "long"
            risk_frac = available_risk_fraction("COT Positioning Extreme", open_counter[0])
            if risk_frac <= 0:
                msgs.append(f"{inst}: COT extreme signal fired (pctile={pctile:.0f}) but SKIPPED — 10% risk budget full.")
            else:
                risk_ref = close * (0.99 if direction == "long" else 1.01)
                risk_gbp = current_risk_gbp("COT Positioning Extreme", risk_frac)
                fill = execute_entry(inst, direction, risk_gbp, risk_ref)
                trade_id = fill["trade_id"] if fill else None
                s.update({"state": 1 if direction == "long" else -1, "entry_price": close,
                          "entry_date": cur_date.isoformat(), "trade_id": trade_id, "risk_fraction": risk_frac})
                open_counter[0] += get_risk_pct("COT Positioning Extreme")
                exec_note = f" [LIVE, trade {trade_id}]" if fill else (" [EXECUTION ENABLED but order failed]" if EXECUTION_ENABLED else "")
                msgs.append(f"*{inst}* (COT, pctile={pctile:.0f}) — ENTER {direction.upper()} @ {close:.5f} (~£{risk_gbp:,.0f} at risk){exec_note}")
        else:
            msgs.append(f"{inst} (COT, pctile={pctile:.0f}): FLAT @ {close:.5f}")

        cot_state[inst] = s

    state["cot"] = cot_state
    cot_log = cot_log.tail(3000)  # generous buffer, ~3 instruments x ~10 years of weekly data
    return state, cot_log


def main():
    state_path = BASE / "cot_full_state.json"
    full_state = json.loads(state_path.read_text()) if state_path.exists() else {}
    msgs = []
    open_counter = [0]

    full_state, cot_log = process_cot_all(full_state, msgs, open_counter)

    cot_log.to_csv(COT_LOG, index=False)
    state_path.write_text(json.dumps(full_state, indent=2))

    full_message = "\n".join(msgs)
    send_telegram(full_message)
    print(full_message)


if __name__ == "__main__":
    main()
