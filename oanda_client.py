"""
OANDA v20 REST API client — thin wrapper for the specific operations this
portfolio needs: place a market order with an attached stop, modify a
trailing stop, close a position, and read live account balance.

SAFETY: defaults to OANDA's PRACTICE environment. Going live requires
explicitly setting OANDA_ENVIRONMENT=live as an env var / GitHub secret —
this is a deliberate two-step guard against accidentally trading real
money with code that was only ever tested on practice.

Credentials (OANDA_API_TOKEN, OANDA_ACCOUNT_ID) are read from environment
variables only — never hardcoded, never logged, never printed.
"""
import os
import requests

ENVIRONMENT = os.environ.get("OANDA_ENVIRONMENT") or "practice"  # falls back on empty string too, not just unset (GitHub Actions injects "" when a secret isn't configured, not a missing key)
API_TOKEN = os.environ.get("OANDA_API_TOKEN")
ACCOUNT_ID = os.environ.get("OANDA_ACCOUNT_ID")

BASE_URLS = {
    "practice": "https://api-fxpractice.oanda.com",
    "live": "https://api-fxtrade.oanda.com",
}

if ENVIRONMENT not in BASE_URLS:
    raise ValueError(f"OANDA_ENVIRONMENT must be 'practice' or 'live', got: {ENVIRONMENT}")

BASE_URL = BASE_URLS[ENVIRONMENT]


def _headers():
    if not API_TOKEN:
        raise RuntimeError("OANDA_API_TOKEN not set")
    return {
        "Authorization": f"Bearer {API_TOKEN}",
        "Content-Type": "application/json",
    }


def _account_url(path=""):
    if not ACCOUNT_ID:
        raise RuntimeError("OANDA_ACCOUNT_ID not set")
    return f"{BASE_URL}/v3/accounts/{ACCOUNT_ID}{path}"


def get_account_summary():
    """Returns dict with 'balance', 'NAV', 'marginAvailable', etc."""
    r = requests.get(_account_url("/summary"), headers=_headers(), timeout=15)
    r.raise_for_status()
    return r.json()["account"]


def get_current_balance():
    return float(get_account_summary()["balance"])


def get_instrument_list():
    """Returns the account's tradeable instrument names, e.g. 'EUR_USD',
    'NAS100_USD'. Use this to verify instrument name mappings before
    placing any order — OANDA's exact symbol for indices can differ from
    what you'd guess (e.g. DE30 might be 'DE30_EUR' or a renamed variant)."""
    r = requests.get(_account_url("/instruments"), headers=_headers(), timeout=15)
    r.raise_for_status()
    return [i["name"] for i in r.json()["instruments"]]


def get_open_trades():
    r = requests.get(_account_url("/openTrades"), headers=_headers(), timeout=15)
    r.raise_for_status()
    return r.json()["trades"]


def place_market_order_with_stop(instrument, units, stop_loss_price, take_profit_price=None):
    """
    instrument: OANDA symbol, e.g. 'EUR_USD', 'XAU_USD', 'NAS100_USD'
    units: positive = long, negative = short
    stop_loss_price: absolute price level for the attached stop
    take_profit_price: optional absolute price level for attached target

    Returns the order fill response, including the actual fill price and
    the tradeID (needed later to modify the stop or close the position).
    """
    order = {
        "order": {
            "type": "MARKET",
            "instrument": instrument,
            "units": str(int(units)),
            "timeInForce": "FOK",  # fill-or-kill: don't leave a resting order if price moved
            "positionFill": "DEFAULT",
            "stopLossOnFill": {"price": f"{stop_loss_price:.5f}"},
        }
    }
    if take_profit_price is not None:
        order["order"]["takeProfitOnFill"] = {"price": f"{take_profit_price:.5f}"}

    r = requests.post(_account_url("/orders"), headers=_headers(), json=order, timeout=15)
    r.raise_for_status()
    return r.json()


def modify_stop_loss(trade_id, new_stop_price):
    """Replace the stop-loss on an existing open trade (used by the
    ADX+Supertrend trailing-stop logic each run)."""
    body = {"stopLoss": {"price": f"{new_stop_price:.5f}"}}
    r = requests.put(_account_url(f"/trades/{trade_id}/orders"), headers=_headers(), json=body, timeout=15)
    r.raise_for_status()
    return r.json()


def close_trade(trade_id):
    """Fully close an open trade at market (used for target-hit / reversal
    exits that aren't triggered by the stop itself)."""
    r = requests.put(_account_url(f"/trades/{trade_id}/close"), headers=_headers(), json={}, timeout=15)
    r.raise_for_status()
    return r.json()


def get_current_price(instrument):
    """Latest bid/ask for an instrument."""
    r = requests.get(_account_url(f"/pricing?instruments={instrument}"), headers=_headers(), timeout=15)
    r.raise_for_status()
    p = r.json()["prices"][0]
    return {"bid": float(p["bids"][0]["price"]), "ask": float(p["asks"][0]["price"])}
