"""
Sends a daily P&L summary to Telegram - separate from each strategy's own
per-signal messages. Run once per day, after all the other daily
strategies have had a chance to run and log any trades that closed today.
"""
import os
import requests
from journal import build_daily_pnl_summary


def send_telegram(msg):
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


if __name__ == "__main__":
    summary = build_daily_pnl_summary()
    send_telegram(summary)
    print(summary)
