"""
Alert Dispatch - Slack + Telegram notifications.
"""
import requests
import logging
from config import SLACK_WEBHOOK_URL, TELEGRAM_BOT_TOKEN, TELEGRAM_CHAT_ID

log = logging.getLogger("jarvis.alerts")

def send_slack(text: str, emoji: str = "info") -> bool:
    if not SLACK_WEBHOOK_URL:
        return False
    try:
        r = requests.post(SLACK_WEBHOOK_URL, json={"text": f"{emoji} {text}"}, timeout=8)
        return r.status_code == 200
    except Exception as e:
        log.error(f"Slack alert failed: {e}")
        return False

def send_telegram(text: str, emoji: str = "info") -> bool:
    if not TELEGRAM_BOT_TOKEN or not TELEGRAM_CHAT_ID:
        return False
    try:
        url = f"https://api.telegram.org/bot{TELEGRAM_BOT_TOKEN}/sendMessage"
        r = requests.post(url, json={
            "chat_id": TELEGRAM_CHAT_ID,
            "text": f"{emoji} {text}",
            "parse_mode": "HTML",
        }, timeout=8)
        return r.status_code == 200
    except Exception as e:
        log.error(f"Telegram alert failed: {e}")
        return False

def alert_recommendation(asset: str, asset_type: str, action: str,
                         price: float, confidence: float, rationale: list) -> None:
    pct = round(confidence * 100) if confidence <= 1 else round(confidence)
    if action == "HOLD":
        return
    icon = "BUY" if action == "BUY" else "SELL"
    lines = [f"<b>{action} {asset.upper()}</b> - ${price:,.2f} ({pct}% conf)"]
    for r in rationale[:3]:
        lines.append(f"  - {r}")
    msg = "\n".join(lines)
    log.info(f"ALERT: {action} {asset} @ ${price} ({pct}%)")
    send_slack(msg, icon)
    send_telegram(msg, icon)

def alert_price_target(asset: str, target_price: float, current_price: float,
                       direction: str) -> None:
    msg = (f"<b>PRICE TARGET HIT</b>\n"
           f"{asset.upper()}: ${current_price:,.2f}\n"
           f"Target ({direction}): ${target_price:,.2f}")
    log.info(f"ALERT: Target hit {asset} @ ${current_price}")
    send_slack(msg, "TARGET")
    send_telegram(msg, "TARGET")

def alert_trade_executed(asset: str, side: str, qty: float, price: float,
                         status: str) -> None:
    msg = (f"<b>TRADE {status.upper()}</b>\n"
           f"{side.upper()} {qty} {asset.upper()} @ ~${price:,.2f}")
    log.info(f"ALERT: Trade {status} {side} {qty} {asset}")
    send_slack(msg, "TRADE")
    send_telegram(msg, "TRADE")
