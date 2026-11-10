"""Options Bot - Improved version"""

import os
import time
import math
import logging
import threading
from datetime import date, datetime
from zoneinfo import ZoneInfo

import requests
import yfinance as yf
from flask import Flask, request, abort

logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
log = logging.getLogger("options_bot")

TOKEN = os.environ.get("TELEGRAM_TOKEN") or os.environ.get("TELEGRAM_BOT_TOKEN")
SECRET = os.environ.get("WEBHOOK_SECRET", "mysecret123")
CHAT_ID = str(os.environ.get("TELEGRAM_CHAT_ID", "")).strip()
PUBLIC_URL = os.environ.get("PUBLIC_URL", "").rstrip("/")

WATCHLIST = [
    "SPY", "QQQ", "IWM", "AAPL", "MSFT", "NVDA", "TSLA", "AMZN", "META",
    "GOOGL", "AMD", "NFLX", "AVGO", "JPM", "BAC", "XOM", "UNH", "LLY",
    "V", "MA", "HD", "COST", "CRM", "ORCL", "ADBE", "INTC", "MU", "QCOM"
]

NY = ZoneInfo("America/New_York")
API = f"https://api.telegram.org/bot{TOKEN}"
DISCLAIMER = "⚠️ معلومات وتحليل فني فقط وليست توصية استثمارية. لا تخاطر بأكثر من 1-2% من رأس مالك."

app = Flask(__name__)

def tg_send(chat_id, text):
    if not TOKEN or not chat_id:
        return
    for i in range(0, len(text), 4000):
        try:
            requests.post(f"{API}/sendMessage", json={
                "chat_id": chat_id,
                "text": text[i:i+4000]
            }, timeout=15)
        except Exception as e:
            log.error("tg_send error: %s", e)

def register_webhook():
    if not PUBLIC_URL or not TOKEN:
        return
    try:
        requests.post(f"{API}/setWebhook", json={
            "url": f"{PUBLIC_URL}/webhook/{SECRET}",
            "secret_token": SECRET,
            "allowed_updates": ["message"],
            "drop_pending_updates": True
        }, timeout=15)
    except Exception as e:
        log.error("Webhook error: %s", e)

def is_market_open():
    now = datetime.now(NY)
    if now.weekday() >= 5:
        return False
    minutes = now.hour * 60 + now.minute
    return 9*60+30 <= minutes <= 16*60

def get_ticker_data(sym):
    t = yf.Ticker(sym)
    hist = t.history(period="3mo")
    if hist.empty or len(hist) < 20:
        return None
    price = float(hist["Close"].iloc[-1])
    atr = float((hist["High"] - hist["Low"]).rolling(14).mean().iloc[-1])
    ema20 = float(hist["Close"].ewm(span=20).mean().iloc[-1])
    ema50 = float(hist["Close"].ewm(span=50).mean().iloc[-1])
    return {"price": price, "atr": atr, "ema20": ema20, "ema50": ema50, "ticker": t}

def find_best_contract(t, side, price):
    try:
        options = t.options
        if not options:
            return None
        # أقرب تاريخ انتهاء بين 7 و 45 يوم
        today = date.today()
        best_exp = None
        for exp in options:
            dte = (date.fromisoformat(exp) - today).days
            if 7 <= dte <= 45:
                best_exp = exp
                break
        if not best_exp:
            best_exp = options[0]

        chain = t.option_chain(best_exp)
        df = chain.calls if side == "CALL" else chain.puts
        df = df[(df["bid"] > 0) & (df["ask"] > 0)].copy()
        if df.empty:
            return None

        # نختار أقرب سترايك للسعر (ATM أو قريب منه)
        df["distance"] = abs(df["strike"] - price)
        df = df[df["lastPrice"].between(10, 90)]
        if df.empty:
            return None

        best = df.nsmallest(1, "distance").iloc[0]
        return {
            "strike": best["strike"],
            "expiry": best_exp,
            "price": float(best["lastPrice"]),
            "bid": float(best["bid"]),
            "ask": float(best["ask"]),
        }
    except Exception as e:
        log.warning("contract error: %s", e)
        return None

def get_gamma_levels(t, price):
    try:
        options = t.options
        if not options:
            return None
        exp = options[0]
        chain = t.option_chain(exp)
        calls = chain.calls
        puts = chain.puts

        call_wall = None
        put_wall = None
        if not calls.empty:
            calls = calls[calls["openInterest"] > 0]
            if not calls.empty:
                call_wall = float(calls.loc[calls["openInterest"].idxmax()]["strike"])
        if not puts.empty:
            puts = puts[puts["openInterest"] > 0]
            if not puts.empty:
                put_wall = float(puts.loc[puts["openInterest"].idxmax()]["strike"])

        return {"call_wall": call_wall, "put_wall": put_wall}
    except:
        return None

def analyze_symbol(sym):
    sym = sym.upper().strip()
    data = get_ticker_data(sym)
    if not data:
        return f"ما لقيت بيانات كافية لـ {sym}"

    price = data["price"]
    atr = data["atr"]
    ema20 = data["ema20"]
    ema50 = data["ema50"]

    score = 0
    reasons = []
    if price > ema20 > ema50:
        score = 3
        reasons.append("ترند صاعد قوي")
        side = "CALL"
    elif price < ema20 < ema50:
        score = -3
        reasons.append("ترند هابط قوي")
        side = "PUT"
    else:
        side = "محايد"
        reasons.append("ترند مختلط")

    # الأهداف
    if side == "CALL":
        t1 = price + atr * 1.5
        t2 = price + atr * 3
        stop = price - atr * 1.2
    elif side == "PUT":
        t1 = price - atr * 1.5
        t2 = price - atr * 3
        stop = price + atr * 1.2
    else:
        t1 = t2 = stop = price

    contract = find_best_contract(data["ticker"], side, price) if side != "محايد" else None
    gex = get_gamma_levels(data["ticker"], price)

    lines = [
        f"📊 {sym}  ${price:.2f}",
        f"التقييم: {score:+d} | الاتجاه: {side}",
        "",
        "الأسباب:",
    ]
    lines += [f"• {r}" for r in reasons]

    if side != "محايد":
        lines.append("")
        lines.append("🎯 الأهداف (على السهم):")
        lines.append(f"دخول تقريبي: ${price:.2f}")
        lines.append(f"هدف 1: ${t1:.2f}")
        lines.append(f"هدف 2: ${t2:.2f}")
        lines.append(f"وقف خسارة: ${stop:.2f}")

    if contract:
        lines.append("")
        lines.append(f"عقد مقترح ({side}):")
        lines.append(f"{sym} {side} {contract['strike']:g} | انتهاء {contract['expiry']}")
        lines.append(f"السعر تقريباً: ${contract['price']:.2f}")
        lines.append(f"Bid/Ask: ${contract['bid']:.2f}/${contract['ask']:.2f}")

    if gex:
        lines.append("")
        lines.append("🧮 مستويات الجاما:")
        if gex.get("call_wall"):
            lines.append(f"Call Wall: ${gex['call_wall']:g}")
        if gex.get("put_wall"):
            lines.append(f"Put Wall: ${gex['put_wall']:g}")

    lines.append("")
    lines.append(DISCLAIMER)
    return "\n".join(lines)

def handle_update(update):
    try:
        msg = update.get("message") or {}
        chat_id = str((msg.get("chat") or {}).get("id", ""))
        text = (msg.get("text") or "").strip()
        if not chat_id or not text:
            return
        if CHAT_ID and chat_id != CHAT_ID:
            return

        if text.lower() in ("/start", "/help"):
            tg_send(chat_id, "أرسل رمز سهم مثل TSLA أو NVDA\n/scan لفحص سريع")
            return

        if text.lower().startswith("/scan"):
            tg_send(chat_id, "جاري الفحص...")
            results = []
            for s in WATCHLIST[:6]:
                r = analyze_symbol(s)
                if "ترند صاعد" in r or "ترند هابط" in r:
                    results.append(r.split("\n")[0])
            tg_send(chat_id, "نتائج:\n" + "\n".join(results) if results else "ما فيه إشارات قوية")
            return

        tg_send(chat_id, "⏳ أحلل...")
        tg_send(chat_id, analyze_symbol(text))
    except Exception as e:
        log.error("handle error: %s", e)

@app.route(f"/webhook/{SECRET}", methods=["POST"])
def webhook():
    if request.headers.get("X-Telegram-Bot-Api-Secret-Token") != SECRET:
        abort(403)
    handle_update(request.get_json(force=True, silent=True) or {})
    return "ok"

@app.route("/")
def home():
    return "Options Bot Running"

def start():
    threading.Thread(target=register_webhook, daemon=True).start()
    log.info("Bot started")

start()

if __name__ == "__main__":
    app.run(host="0.0.0.0", port=int(os.environ.get("PORT", 10000)))
