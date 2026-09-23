"""
Maps this portfolio's instrument names to OANDA's exact tradeable symbols.

IMPORTANT: index symbols in particular are NOT guaranteed to match the
obvious guess (OANDA has renamed some over time, e.g. GER30 -> DE30, and
naming conventions like the quote-currency suffix vary). Do not trust
this mapping blindly — run verify_all_mappings() once against your real
account before the first live order, and fix anything it flags.
"""
from oanda_client import get_instrument_list

# Best-effort mapping — VERIFY before trading, see verify_all_mappings() below.
INSTRUMENT_MAP = {
    'AUDCAD': 'AUD_CAD', 'AUDCHF': 'AUD_CHF', 'AUDJPY': 'AUD_JPY', 'AUDNZD': 'AUD_NZD',
    'AUDUSD': 'AUD_USD', 'CADCHF': 'CAD_CHF', 'CADJPY': 'CAD_JPY', 'CHFJPY': 'CHF_JPY',
    'EURAUD': 'EUR_AUD', 'EURCAD': 'EUR_CAD', 'EURCHF': 'EUR_CHF', 'EURGBP': 'EUR_GBP',
    'EURJPY': 'EUR_JPY', 'EURNZD': 'EUR_NZD', 'EURUSD': 'EUR_USD', 'GBPAUD': 'GBP_AUD',
    'GBPCAD': 'GBP_CAD', 'GBPCHF': 'GBP_CHF', 'GBPJPY': 'GBP_JPY', 'GBPNZD': 'GBP_NZD',
    'GBPUSD': 'GBP_USD', 'NZDCAD': 'NZD_CAD', 'NZDJPY': 'NZD_JPY', 'NZDUSD': 'NZD_USD',
    'USDCAD': 'USD_CAD', 'USDCHF': 'USD_CHF', 'USDJPY': 'USD_JPY',
    'NAS100_USD': 'NAS100_USD', 'SPX500': 'SPX500_USD', 'US30': 'US30_USD',
    'US2000': 'US2000_USD', 'DE30': 'DE30_EUR', 'UK100': 'UK100_GBP',
    'XAUUSD': 'XAU_USD',
}


def verify_all_mappings():
    """Run this once against your real (practice) account before the
    first live order. Prints any mapping that doesn't match a real
    tradeable OANDA instrument, so you can fix it before it causes a
    failed (or worse, wrongly-routed) order."""
    live_instruments = set(get_instrument_list())
    problems = []
    for our_name, oanda_symbol in INSTRUMENT_MAP.items():
        if oanda_symbol not in live_instruments:
            # try to suggest a close match
            candidates = [i for i in live_instruments if our_name[:3].upper() in i]
            problems.append((our_name, oanda_symbol, candidates[:5]))

    if not problems:
        print(f"All {len(INSTRUMENT_MAP)} instrument mappings verified OK against your account.")
    else:
        print(f"{len(problems)} mapping(s) need fixing:")
        for our_name, guessed, candidates in problems:
            print(f"  {our_name}: guessed '{guessed}' — not found. Possible matches: {candidates}")
    return problems


if __name__ == "__main__":
    verify_all_mappings()
