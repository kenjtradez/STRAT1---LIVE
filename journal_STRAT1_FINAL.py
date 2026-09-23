"""
Shared journal + equity tracking - STRAT 1 forward test.

Design: identical risk/R-multiple/equity logic to the original
portfolio-forward-test journal.py (per-strategy % of current equity,
10% total open-risk cap, 5x-of-starting-capital compounding cap,
R-multiple tracking, £ P&L conversion). See that file's own docstring
for the full mechanical explanation if needed - not repeated here.

CURRENT MEMBERS (5, as of 2026-09-22): Pivot S/R, Overnight Extension,
Divergence-Fade, COT Positioning Extreme, Gold MM Positioning.
CAGR 9.89%, MaxDD -6.00%, Sharpe 1.632.

Gold MM Positioning was the 5th admission: re-running the full
exhaustive search with it as a candidate showed CAGR 9.8645% -> 9.8885%
(+0.024pp) and Sharpe 1.629 -> 1.632 - a genuine, verified improvement,
though a thin margin, honestly stated as such rather than oversold.

STRAT 1 ADMISSION RULE (apply this every time a new strategy is
validated, before ever adding it here):

    A new strategy is added to Strat 1 ONLY IF re-running the full
    exhaustive combinatorial search (now across the CURRENT Strat 1
    members + the new candidate, i.e. up to 2^(n+1)-1 combinations)
    finds a combination that GENUINELY IMPROVES CAGR at the exact same
    6% MaxDD ceiling, compared to the current Strat 1 CAGR figure
    above. Not "looks promising alone" - not "positive Sharpe" - the
    full re-optimization has to show a real, verified improvement to
    the actual combined number, using the same binary-search-to-exact-
    6%-MaxDD method used to find this very configuration.

    If a new strategy passes: rebuild STRATEGY_RISK_PCT below using
    that verified re-optimization's output (not the strategy's own
    standalone sizing), re-verify the combined MaxDD is still exactly
    -6.00% (not "close"), and update the CAGR/Sharpe figures in this
    docstring and the file's own header to match.

    If a new strategy does NOT pass: it does not belong in Strat 1,
    regardless of how good it looks in isolation - it goes in Capital
    instead (see journal_capital_config.py), which has no such
    admission bar because it has no drawdown ceiling to protect.

Every other validated strategy not currently in this list (Donchian,
Connors RSI, Monday Effect, RSI(2), TGA-Follow, VIX Shock-Fade) has
already been tested against this bar and did NOT improve on the
current members - they remain valid, live in Capital, just not
admitted here.
"""
import json
import csv
from pathlib import Path
from datetime import datetime, timezone

BASE = Path(__file__).parent
EQUITY_PATH = BASE / "equity.json"
JOURNAL_PATH = BASE / "journal.csv"
OVERNIGHT_STATE_PATH = BASE / "overnight_extension_state.json"
DIVERGENCE_STATE_PATH = BASE / "divergence_full_state.json"
COT_STATE_PATH = BASE / "cot_full_state.json"
PIVOT_STATE_PATH = BASE / "nas100_state.json"  # confirmed correct filename, verified against pivot_sr_signals.py
GOLD_MM_STATE_PATH = BASE / "gold_mm_state.json"

STARTING_EQUITY = 1_000_000.0
RISK_PCT = 0.01  # default fallback - should never actually apply, since every strategy here is explicitly listed below
STRATEGY_RISK_PCT = {
    # STRAT 1 (2026-09-22) - see full admission rule in this file's docstring
    "Pivot S/R": 0.0034427,
    "Overnight Extension": 0.0068068,
    "Divergence-Fade": 0.0032725,
    "COT Positioning Extreme": 0.0024217,
    "Gold MM Positioning": 0.0026180,
}

MAX_TOTAL_OPEN_RISK_PCT = 0.10
MAX_RISK_MULTIPLE_OF_STARTING = 5
JOURNAL_HEADERS = [
    "close_timestamp", "instrument", "strategy", "direction",
    "entry_price", "risk_reference_price", "exit_price",
    "risk_distance", "r_multiple", "risked_gbp", "pnl_gbp",
    "equity_before", "equity_after", "risk_fraction_applied",
]


def get_risk_pct(strategy):
    return STRATEGY_RISK_PCT.get(strategy, RISK_PCT)


MAX_ALLOWED_DRAWDOWN_PCT = 0.06
DRAWDOWN_WARNING_PCT = 0.04


def load_equity():
    if EQUITY_PATH.exists():
        return json.loads(EQUITY_PATH.read_text())["equity"]
    return STARTING_EQUITY


def load_peak_equity():
    if EQUITY_PATH.exists():
        data = json.loads(EQUITY_PATH.read_text())
        return data.get("peak_equity", data.get("equity", STARTING_EQUITY))
    return STARTING_EQUITY


def save_equity(equity):
    peak = max(load_peak_equity(), equity)
    EQUITY_PATH.write_text(json.dumps({"equity": equity, "starting_equity": STARTING_EQUITY, "peak_equity": peak}, indent=2))


def get_drawdown_status():
    equity = load_equity()
    peak = load_peak_equity()
    if peak <= 0:
        return 0.0, MAX_ALLOWED_DRAWDOWN_PCT, "ok"
    current_dd = (peak - equity) / peak
    distance_to_limit = MAX_ALLOWED_DRAWDOWN_PCT - current_dd
    if current_dd >= MAX_ALLOWED_DRAWDOWN_PCT:
        level = "BREACH"
    elif current_dd >= DRAWDOWN_WARNING_PCT:
        level = "warning"
    else:
        level = "ok"
    return current_dd, distance_to_limit, level


def ensure_journal_exists():
    if not JOURNAL_PATH.exists():
        with open(JOURNAL_PATH, "w", newline="") as f:
            writer = csv.writer(f)
            writer.writerow(JOURNAL_HEADERS)


def compute_committed_risk_pct():
    """Sums actual risk % already committed by every open position,
    across exactly the 4 Strat 1 strategies. Add a new block here ONLY
    after a new strategy has passed the admission rule above."""
    committed = 0.0
    if PIVOT_STATE_PATH.exists():
        pivot_state = json.loads(PIVOT_STATE_PATH.read_text())
        if pivot_state.get("state", 0) != 0:
            committed += get_risk_pct("Pivot S/R")
    if OVERNIGHT_STATE_PATH.exists():
        overnight_state = json.loads(OVERNIGHT_STATE_PATH.read_text())
        for inst_state in overnight_state.values():
            if inst_state.get("state", 0) != 0:
                committed += get_risk_pct("Overnight Extension")
    if DIVERGENCE_STATE_PATH.exists():
        divergence_full_state = json.loads(DIVERGENCE_STATE_PATH.read_text())
        divergence_state = divergence_full_state.get("divergence", {})
        for inst_state in divergence_state.values():
            if inst_state.get("state", 0) != 0:
                committed += get_risk_pct("Divergence-Fade")
    if COT_STATE_PATH.exists():
        cot_full_state = json.loads(COT_STATE_PATH.read_text())
        cot_state = cot_full_state.get("cot", {})
        for inst_state in cot_state.values():
            if inst_state.get("state", 0) != 0:
                committed += get_risk_pct("COT Positioning Extreme")
    if GOLD_MM_STATE_PATH.exists():
        gold_mm_state = json.loads(GOLD_MM_STATE_PATH.read_text())
        if gold_mm_state.get("state", 0) != 0:
            committed += get_risk_pct("Gold MM Positioning")
    return committed


def available_risk_fraction(strategy, extra_committed_pct=0.0):
    committed = compute_committed_risk_pct() + extra_committed_pct
    remaining_budget_pct = MAX_TOTAL_OPEN_RISK_PCT - committed
    desired_slice = get_risk_pct(strategy)
    if remaining_budget_pct <= 0:
        return 0.0
    return min(1.0, remaining_budget_pct / desired_slice)


def current_risk_gbp(strategy, risk_fraction=1.0):
    equity = load_equity()
    risk_pct = get_risk_pct(strategy)
    uncapped = risk_pct * equity
    compounding_cap = MAX_RISK_MULTIPLE_OF_STARTING * risk_pct * STARTING_EQUITY
    base_risk = min(uncapped, compounding_cap)
    return base_risk * risk_fraction


def record_trade_close(instrument, strategy, direction, entry_price, risk_reference_price, exit_price, risk_fraction_at_entry=1.0):
    ensure_journal_exists()
    equity_before = load_equity()
    risk_pct = get_risk_pct(strategy)
    risk_distance = abs(entry_price - risk_reference_price)
    if risk_distance == 0:
        r_multiple = 0.0
    else:
        raw_move = (exit_price - entry_price) if direction == "long" else (entry_price - exit_price)
        r_multiple = raw_move / risk_distance
    uncapped = risk_pct * equity_before
    compounding_cap = MAX_RISK_MULTIPLE_OF_STARTING * risk_pct * STARTING_EQUITY
    risked_gbp = min(uncapped, compounding_cap) * risk_fraction_at_entry
    pnl_gbp = r_multiple * risked_gbp
    equity_after = equity_before + pnl_gbp
    with open(JOURNAL_PATH, "a", newline="") as f:
        writer = csv.writer(f)
        writer.writerow([
            datetime.now(timezone.utc).isoformat(), instrument, strategy, direction,
            round(entry_price, 6), round(risk_reference_price, 6), round(exit_price, 6),
            round(risk_distance, 6), round(r_multiple, 3), round(risked_gbp, 2), round(pnl_gbp, 2),
            round(equity_before, 2), round(equity_after, 2), round(risk_fraction_at_entry, 3),
        ])
    save_equity(equity_after)
    return pnl_gbp, equity_after


def build_daily_pnl_summary():
    equity = load_equity()
    peak = load_peak_equity()
    total_pnl = equity - STARTING_EQUITY
    total_pnl_pct = (equity / STARTING_EQUITY - 1) * 100
    current_dd, distance_to_limit, dd_level = get_drawdown_status()
    dd_flag = {"ok": "", "warning": " ⚠️ WARNING", "BREACH": " 🚨 LIMIT BREACHED"}[dd_level]
    lines = [
        "DAILY P&L SUMMARY - STRAT 1",
        f"Current equity: £{equity:,.2f}",
        f"Peak equity: £{peak:,.2f}",
        f"Total P&L since inception: £{total_pnl:,.2f} ({total_pnl_pct:+.2f}%)",
        "",
        f"DRAWDOWN: {current_dd:.2%} of peak (limit: {MAX_ALLOWED_DRAWDOWN_PCT:.0%}){dd_flag}",
        f"Room remaining before limit: {distance_to_limit:.2%}",
        "",
    ]
    if not JOURNAL_PATH.exists():
        lines.append("No trades recorded yet.")
        return "\n".join(lines)
    today = datetime.now(timezone.utc).date()
    todays_trades = []
    with open(JOURNAL_PATH, newline="") as f:
        reader = csv.DictReader(f)
        for row in reader:
            try:
                ts = datetime.fromisoformat(row["close_timestamp"])
            except (KeyError, ValueError):
                continue
            if ts.date() == today:
                todays_trades.append(row)
    if not todays_trades:
        lines.append("No trades closed today.")
    else:
        todays_pnl = sum(float(t["pnl_gbp"]) for t in todays_trades)
        lines.append(f"Today's closed trades: {len(todays_trades)}, total P&L: £{todays_pnl:,.2f}")
        lines.append("")
        for t in todays_trades:
            sign = "+" if float(t["pnl_gbp"]) >= 0 else ""
            lines.append(f"  {t['instrument']} ({t['strategy']}, {t['direction']}): {sign}£{float(t['pnl_gbp']):,.2f} ({float(t['r_multiple']):+.2f}R)")
    return "\n".join(lines)
