"""Options Bot - Clean version for Telegram + Render"""

import os
import re
import time
import math
import logging
import threading
from datetime import date, datetime, timedelta
from zoneinfo import ZoneInfo

import requests
import yfinance as yf
from flask import Flask, request, abort

logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
log = logging.getLogger("options_bot")

# ================== CONFIG ==================
TOKEN = os.environ.get("TELEGRAM_TOKEN") or os.environ.get("TELEGRAM_BOT_TOKEN")
SECRET = os.environ.get("WEBHOOK_SECRET", "mysecret123")
CHAT_ID = str(os.environ.get("TELEGRAM_CHAT_ID", "")).strip()
PUBLIC_URL = os.environ.get("PUBLIC_URL", "").rstrip("/")

WATCHLIST = [
    "SPY", "QQQ", "IWM", "AAPL", "MSFT", "NVDA", "TSLA", "AMZN", "META",
    "GOOGL", "AMD", "NFLX", "AVGO", "JPM", "BAC", "XOM", "UNH", "LLY",
    "V", "MA", "HD", "COST", "CRM", "ORCL", "ADBE", "INTC", "MU", "QCOM"
]

SCAN_INTERVAL = 12 * 60
MIN_SCORE_ALERT = 4
MIN_DTE = 3
MAX_DTE = 60
MIN_PRICE = 10.0
MAX_PRICE = 90.0
MAX_SPREAD = 0.15
MIN_VOLUME = 300
MIN_OI = 100

NY = ZoneInfo("America/New_York")
API = f"https://api.telegram.org/bot{TOKEN}"
DISCLAIMER = "⚠️ معلومات وتحليل فني فقط وليست توصية استثمارية. لا تخاطر بأكثر من 1-2% من رأس مالك."

app = Flask(__name__)

# ================== TELEGRAM ==================
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
        r = requests.post(f"{API}/setWebhook", json={
            "url": f"{PUBLIC_URL}/webhook/{SECRET}",
            "secret_token": SECRET,
            "allowed_updates": ["message"],
            "drop_pending_updates": True
        }, timeout=15)
        log.info("Webhook: %s", r.json())
    except Exception as e:
        log.error("Webhook error: %s", e)

# ================== HELPERS ==================
def is_market_open():
    now = datetime.now(NY)
    if now.weekday() >= 5:
        return False
    minutes = now.hour * 60 + now.minute
    return 9*60+30 <= minutes <= 16*60

def money(x):
    if x >= 1_000_000:
        return f"${x/1_000_000:.1f}M"
    if x >= 1000:
        return f"${x/1000:.0f}K"
    return f"${x:.0f}"

# ================== ANALYSIS ==================
def get_price(sym):
    try:
        t = yf.Ticker(sym)
        h = t.history(period="5d")
        if h.empty:
            return None
        return float(h["Close"].iloc[-1])
    except:
        return None

def analyze_symbol(sym):
    sym = sym.upper().strip()
    price = get_price(sym)
    if not price:
        return f"ما لقيت بيانات لـ {sym}"

    try:
        t = yf.Ticker(sym)
        # Simple technicals
        hist = t.history(period="3mo")
        if hist.empty or len(hist) < 30:
            return f"بيانات غير كافية لـ {sym}"

        close = hist["Close"]
        ema20 = close.ewm(span=20).mean().iloc[-1]
        ema50 = close.ewm(span=50).mean().iloc[-1]
        rsi = 50  # placeholder for simplicity

        score = 0
        reasons = []

        if price > ema20 > ema50:
            score += 3
            reasons.append("ترند صاعد قوي")
        elif price < ema20 < ema50:
            score -= 3
            reasons.append("ترند هابط قوي")

        side = "CALL" if score > 0 else "PUT" if score < 0 else "محايد"

        # Options suggestion
        msg = [
            f"📊 {sym}  ${price:.2f}",
            f"التقييم: {score:+d} | الاتجاه: {side}",
            "",
            "الأسباب:",
        ]
        msg += [f"• {r}" for r in reasons]

        # Try to find a contract
        try:
            options = t.options
            if options:
                exp = options[0]
                chain = t.option_chain(exp)
                df = chain.calls if side == "CALL" else chain.puts
                df = df[(df["bid"] > 0) & (df["ask"] > 0)]
                df = df[(df["lastPrice"] >= MIN_PRICE) & (df["lastPrice"] <= MAX_PRICE)]
                if not df.empty:
                    best = df.iloc[0]
                    msg.append("")
                    msg.append(f"عقد مقترح ({side}):")
                    msg.append(f"{sym} {side} {best['strike']:g} | انتهاء {exp}")
                    msg.append(f"السعر تقريباً: ${best['lastPrice']:.2f}")
        except:
            pass

        msg.append("")
        msg.append(DISCLAIMER)
        return "\n".join(msg)
    except Exception as e:
        return f"خطأ في تحليل {sym}: {e}"

# ================== HANDLER ==================
def handle_update(update):
    try:
        msg = update.get("message") or {}
        chat_id = str((msg.get("chat") or {}).get("id", ""))
        text = (msg.get("text") or "").strip()
        if not chat_id or not text:
            return

        if CHAT_ID and chat_id != CHAT_ID:
            return

        if text.startswith("/start") or text.startswith("/help"):
            tg_send(chat_id, "أرسل رمز سهم مثل TSLA أو NVDA وأحلل لك.\n/scan لفحص سريع")
            return

        if text.startswith("/scan"):
            tg_send(chat_id, "جاري الفحص...")
            results = []
            for sym in WATCHLIST[:8]:
                res = analyze_symbol(sym)
                if "ترند صاعد" in res or "ترند هابط" in res:
                    results.append(res.split("\n")[0])
            if results:
                tg_send(chat_id, "نتائج سريعة:\n" + "\n".join(results))
            else:
                tg_send(chat_id, "ما فيه إشارات قوية حالياً")
            return

        # أي نص آخر = تحليل سهم
        tg_send(chat_id, "⏳ أحلل...")
        result = analyze_symbol(text)
        tg_send(chat_id, result)

    except Exception as e:
        log.error("handle error: %s", e)

# ================== FLASK ==================
@app.route(f"/webhook/{SECRET}", methods=["POST"])
def webhook():
    if request.headers.get("X-Telegram-Bot-Api-Secret-Token") != SECRET:
        abort(403)
    handle_update(request.get_json(force=True, silent=True) or {})
    return "ok"

@app.route("/")
def home():
    return "Options Bot Running"

# ================== BACKGROUND ==================
def scanner_loop():
    while True:
        try:
            if is_market_open() and CHAT_ID:
                # بسيط: يفحص بعض الأسهم ويرسل لو فيه شيء
                pass
        except Exception as e:
            log.error("scanner: %s", e)
        time.sleep(SCAN_INTERVAL)

def start():
    threading.Thread(target=register_webhook, daemon=True).start()
    threading.Thread(target=scanner_loop, daemon=True).start()
    log.info("Bot started")

start()

if __name__ == "__main__":
    port = int(os.environ.get("PORT", 10000))
    app.run(host="0.0.0.0", port=port)
