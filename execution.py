"""
Execution layer: converts a GBP risk amount into the correct OANDA order
size, places the order with its stop attached, and closes trades on
signal-driven exits (target hit / reversal — not stop hits, which OANDA
handles natively once the stop is attached).

Falls back to signal-only (no real order) if OANDA credentials aren't
configured, so the existing scripts keep working in forward-test mode
until you're ready to flip this on.
"""
import os
import oanda_client as oanda
from instrument_map import INSTRUMENT_MAP

EXECUTION_ENABLED = bool(os.environ.get("OANDA_API_TOKEN")) and bool(os.environ.get("OANDA_ACCOUNT_ID"))


def gbp_to_quote_currency_rate(quote_currency):
    """Rate to convert 1 GBP into the instrument's quote currency, via
    OANDA's own pricing (so it's consistent with what you'll actually
    trade at, not a separate data source)."""
    if quote_currency == "GBP":
        return 1.0
    pair = f"GBP_{quote_currency}"
    try:
        price = oanda.get_current_price(pair)
        return (price["bid"] + price["ask"]) / 2
    except Exception:
        # try the inverse pair if the direct one doesn't exist on OANDA
        inv_pair = f"{quote_currency}_GBP"
        price = oanda.get_current_price(inv_pair)
        mid = (price["bid"] + price["ask"]) / 2
        return 1.0 / mid


def compute_units(oanda_symbol, risked_gbp, stop_distance_price, direction):
    """Returns signed unit count (positive=long, negative=short) sized so
    that hitting the stop loses approximately risked_gbp."""
    quote_currency = oanda_symbol.split("_")[1]
    fx_rate = gbp_to_quote_currency_rate(quote_currency)
    risked_in_quote_ccy = risked_gbp * fx_rate
    units = risked_in_quote_ccy / stop_distance_price
    return units if direction == "long" else -units


def execute_entry(our_instrument_name, direction, risked_gbp, stop_price):
    """
    our_instrument_name: e.g. 'EURGBP', 'NAS100_USD' — this portfolio's naming
    direction: 'long' or 'short'
    risked_gbp: £ amount to risk (already computed by journal.py's 1%-of-equity logic)
    stop_price: absolute price for the protective stop

    Returns dict with trade_id and actual fill price, or None if execution
    is disabled (forward-test mode) or the order fails.
    """
    if not EXECUTION_ENABLED:
        return None

    oanda_symbol = INSTRUMENT_MAP.get(our_instrument_name)
    if not oanda_symbol:
        print(f"[execution] No OANDA mapping for {our_instrument_name}, skipping real order.")
        return None

    price_now = oanda.get_current_price(oanda_symbol)
    entry_est = price_now["ask"] if direction == "long" else price_now["bid"]
    stop_distance = abs(entry_est - stop_price)
    if stop_distance <= 0:
        print(f"[execution] Invalid stop distance for {our_instrument_name}, skipping order.")
        return None

    units = compute_units(oanda_symbol, risked_gbp, stop_distance, direction)

    try:
        result = oanda.place_market_order_with_stop(oanda_symbol, units, stop_price)
        fill = result.get("orderFillTransaction")
        if not fill:
            print(f"[execution] Order for {our_instrument_name} did not fill: {result}")
            return None
        return {
            "trade_id": fill["tradeOpened"]["tradeID"],
            "fill_price": float(fill["price"]),
            "units": units,
        }
    except Exception as e:
        print(f"[execution] Order failed for {our_instrument_name}: {e}")
        return None


def execute_exit(trade_id):
    """Closes an open trade at market. Returns the close transaction, or
    None if execution is disabled or the close fails."""
    if not EXECUTION_ENABLED or not trade_id:
        return None
    try:
        result = oanda.close_trade(trade_id)
        return result.get("orderFillTransaction")
    except Exception as e:
        print(f"[execution] Close failed for trade {trade_id}: {e}")
        return None


def update_trailing_stop(trade_id, new_stop_price):
    """Used by the ADX+Supertrend system to trail its stop each run."""
    if not EXECUTION_ENABLED or not trade_id:
        return None
    try:
        return oanda.modify_stop_loss(trade_id, new_stop_price)
    except Exception as e:
        print(f"[execution] Stop update failed for trade {trade_id}: {e}")
        return None
