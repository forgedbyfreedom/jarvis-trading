"""
Jarvis Advisory Engine
Generates rich trading advice beyond simple buy/sell signals.
"""
import logging
from datetime import datetime

log = logging.getLogger("jarvis.advisory")

def generate_advisory(scan_data: dict, portfolio: dict,
                      sentiment: dict, win_stats: dict) -> dict:
    """
    Produce a comprehensive advisory report.
    """
    advice = {
        "generated": datetime.now().isoformat(),
        "market_regime":  _assess_regime(sentiment),
        "overall_stance": "",
        "top_opportunities": [],
        "risk_alerts": [],
        "allocation_advice": _allocation_advice(scan_data, portfolio),
        "position_sizing": [],
        "things_to_avoid": [],
        "summary": "",
    }

    stocks = scan_data.get("stocks", {})
    crypto = scan_data.get("crypto", {})
    all_signals = {**stocks, **crypto}

    # ── Overall stance ──
    buys  = [s for s in all_signals.values() if s.get("action") == "BUY"]
    sells = [s for s in all_signals.values() if s.get("action") == "SELL"]
    total = len(all_signals)

    if total == 0:
        advice["overall_stance"] = "Waiting for sufficient data."
    elif len(buys) > len(sells) * 1.5:
        advice["overall_stance"] = "BULLISH — Multiple strong buy signals detected."
    elif len(sells) > len(buys) * 1.5:
        advice["overall_stance"] = "BEARISH — Multiple strong sell signals. Consider reducing exposure."
    else:
        advice["overall_stance"] = "NEUTRAL — Mixed signals. Selective trading advised."

    # ── Top opportunities (sorted by confidence) ──
    actionable = [
        s for s in all_signals.values()
        if s.get("action") != "HOLD" and s.get("confidence", 0) >= 0.75
    ]
    actionable.sort(key=lambda x: x.get("confidence", 0), reverse=True)

    for sig in actionable[:5]:
        asset = sig.get("asset", "?")
        action = sig.get("action", "?")
        price = sig.get("price", 0)
        conf = sig.get("confidence", 0)
        rationale = sig.get("rationale", "")

        entry = {
            "asset": asset,
            "action": action,
            "price": price,
            "confidence": round(conf * 100, 1),
            "rationale": rationale,
            "suggested_stop_loss": round(price * 0.97, 4) if action == "BUY" else round(price * 1.03, 4),
            "suggested_take_profit": round(price * 1.08, 4) if action == "BUY" else round(price * 0.92, 4),
            "risk_reward": _calc_risk_reward(price, action),
        }
        advice["top_opportunities"].append(entry)

    # ── Risk alerts ──
    sentiment_val = sentiment.get("current", {}).get("value", 50)
    if sentiment_val < 25:
        advice["risk_alerts"].append("EXTREME FEAR — Market sentiment very low. Contrarian opportunities may exist but size positions carefully.")
    elif sentiment_val > 75:
        advice["risk_alerts"].append("EXTREME GREED — Sentiment very high. Watch for reversals; take profits on winners.")

    if win_stats.get("win_rate", 50) < 40 and win_stats.get("closed_trades", 0) > 5:
        advice["risk_alerts"].append(f"Win rate below 40% ({win_stats['win_rate']}%). Consider reducing position sizes until edge improves.")

    daily_pnl = win_stats.get("total_pnl", 0)
    if daily_pnl < -200:
        advice["risk_alerts"].append(f"Recent losses totaling ${abs(daily_pnl):,.2f}. Consider reducing risk exposure.")

    # ── Things to avoid ──
    low_conf = [s for s in all_signals.values() if 0.5 <= s.get("confidence", 0) < 0.70 and s.get("action") != "HOLD"]
    if low_conf:
        advice["things_to_avoid"] = [
            f"{s['asset']} — {s['action']} signal but only {round(s['confidence']*100)}% confidence. Wait for stronger confirmation."
            for s in low_conf[:3]
        ]

    # ── Summary ──
    advice["summary"] = (
        f"{len(buys)} BUY / {len(sells)} SELL signals across {total} assets. "
        f"Sentiment: {sentiment_val} ({sentiment.get('current',{}).get('label','N/A')}). "
        f"Win rate: {win_stats.get('win_rate',0)}%. "
        f"{advice['overall_stance']}"
    )

    return advice


def _assess_regime(sentiment: dict) -> str:
    val = sentiment.get("current", {}).get("value", 50)
    if val < 20:  return "Extreme Fear — Potential bottom zone"
    if val < 40:  return "Fear — Risk-off environment"
    if val < 60:  return "Neutral — Mixed conditions"
    if val < 80:  return "Greed — Risk-on environment"
    return "Extreme Greed — Potential overextension"


def _calc_risk_reward(price: float, action: str) -> str:
    if action == "BUY":
        sl = price * 0.97
        tp = price * 1.08
    else:
        sl = price * 1.03
        tp = price * 0.92
    risk = abs(price - sl)
    reward = abs(tp - price)
    rr = round(reward / risk, 2) if risk > 0 else 0
    return f"1:{rr}"


def _allocation_advice(scan_data: dict, portfolio: dict) -> list:
    """Suggest allocation based on signal strength."""
    stocks = scan_data.get("stocks", {})
    crypto = scan_data.get("crypto", {})

    stock_buys = sum(1 for s in stocks.values() if s.get("action") == "BUY")
    crypto_buys = sum(1 for s in crypto.values() if s.get("action") == "BUY")

    total_buys = stock_buys + crypto_buys
    if total_buys == 0:
        return ["Hold current allocation — no strong directional signals."]

    stock_pct = round(stock_buys / total_buys * 100)
    crypto_pct = round(crypto_buys / total_buys * 100)

    return [
        f"Signal-weighted allocation: {stock_pct}% stocks / {crypto_pct}% crypto",
        f"Based on {stock_buys} stock BUY vs {crypto_buys} crypto BUY signals",
        "Rebalance if actual allocation drifts >10% from target",
    ]
