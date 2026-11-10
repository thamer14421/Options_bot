"""Options Bot - Strict & Fast"""

import os
import logging
import threading
from datetime import date
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

NY = ZoneInfo("America/New_York")
API = f"https://api.telegram.org/bot{TOKEN}"
DISCLAIMER = "⚠️ تحليل فني فقط وليس ضمان ربح. لا تخاطر بأكثر من 1-2% من رأس مالك."

app = Flask(__name__)

def tg_send(chat_id, text):
    if not TOKEN or not chat_id:
        return
    for i in range(0, len(text), 4000):
        try:
            requests.post(f"{API}/sendMessage", json={"chat_id": chat_id, "text": text[i:i+4000]}, timeout=10)
        except Exception as e:
            log.error("send: %s", e)

def register_webhook():
    if not PUBLIC_URL or not TOKEN:
        return
    try:
        requests.post(f"{API}/setWebhook", json={
            "url": f"{PUBLIC_URL}/webhook/{SECRET}",
            "secret_token": SECRET,
            "allowed_updates": ["message"],
            "drop_pending_updates": True
        }, timeout=10)
    except Exception as e:
        log.error("webhook: %s", e)

def calc_rsi(closes, period=14):
    try:
        delta = closes.diff()
        gain = delta.clip(lower=0).rolling(period).mean()
        loss = (-delta.clip(upper=0)).rolling(period).mean()
        rs = gain / loss
        rsi = 100 - (100 / (1 + rs))
        return float(rsi.iloc[-1]) if not rsi.empty else 50
    except:
        return 50

def analyze(sym):
    sym = sym.upper().strip()
    try:
        t = yf.Ticker(sym)
        hist = t.history(period="3mo")
        if hist.empty or len(hist) < 20:
            return f"بيانات غير كافية لـ {sym}"

        close = hist["Close"]
        price = float(close.iloc[-1])
        atr = float((hist["High"] - hist["Low"]).rolling(14).mean().iloc[-1])
        ema20 = float(close.ewm(span=20).mean().iloc[-1])
        ema50 = float(close.ewm(span=50).mean().iloc[-1])
        rsi = calc_rsi(close)

        checks = []
        score = 0

        trend_strong = price > ema20 > ema50
        if trend_strong:
            score += 3
            checks.append("✅ ترند صاعد قوي")
        elif price < ema20 < ema50:
            score -= 3
            checks.append("❌ ترند هابط")
        else:
            checks.append("⚠️ ترند غير واضح")

        rsi_good = 48 <= rsi <= 66
        if rsi_good:
            score += 1
            checks.append(f"✅ RSI {rsi:.0f} مناسب")
        elif rsi > 72:
            score -= 1
            checks.append(f"⚠️ RSI {rsi:.0f} تشبع شراء")
        else:
            checks.append(f"• RSI {rsi:.0f}")

        side = "CALL" if score >= 3 else "PUT" if score <= -3 else "محايد"

        if side == "CALL":
            t1 = price + atr * 1.2
            t2 = price + atr * 2.3
            stop = price - atr * 1.0
        elif side == "PUT":
            t1 = price - atr * 1.2
            t2 = price - atr * 2.3
            stop = price + atr * 1.0
        else:
            t1 = t2 = stop = price

        rr = abs(t2 - price) / max(abs(price - stop), 0.01)
        rr_good = rr >= 1.5

        # الحكم الصارم
        if side == "CALL" and trend_strong and rsi_good and rr_good and score >= 4:
            verdict = "✅ دخول قوي ومؤكد"
        elif side == "PUT" and score <= -4 and rr_good:
            verdict = "✅ دخول قوي (PUT)"
        elif abs(score) >= 3:
            verdict = "⚠️ دخول متوسط (بحذر)"
        else:
            verdict = "⏳ لا تدخل الآن"

        lines = [
            f"📊 {sym}  ${price:.2f}",
            f"الحكم: {verdict}",
            f"التقييم: {score:+d} | الاتجاه: {side}",
            "",
            "التأكيدات:",
        ]
        lines += checks
        lines += [
            "",
            "🎯 الخطة:",
            f"دخول: ~${price:.2f}",
            f"هدف 1: ${t1:.2f}",
            f"هدف 2: ${t2:.2f}",
            f"وقف: ${stop:.2f}",
            f"نسبة المخاطرة/العائد: 1:{rr:.1f}",
        ]

        if side != "محايد":
            try:
                opts = t.options
                if opts:
                    exp = opts[0]
                    ch = t.option_chain(exp)
                    df = ch.calls if side == "CALL" else ch.puts
                    df = df[(df["bid"] > 0) & (df["ask"] > 0) & df["lastPrice"].between(10, 90)].copy()
                    if not df.empty:
                        df["dist"] = abs(df["strike"] - price)
                        best = df.nsmallest(1, "dist").iloc[0]
                        lines += [
                            "",
                            f"عقد: {sym} {side} {best['strike']:g} | {exp}",
                            f"سعر تقريبي: ${best['lastPrice']:.2f}",
                        ]
            except:
                pass

        lines.append(f"\n{DISCLAIMER}")
        return "\n".join(lines)
    except Exception as e:
        log.error("analyze %s: %s", sym, e)
        return f"خطأ مؤقت في {sym}، جرب بعد ثواني."

def handle_update(update):
    try:
        msg = update.get("message") or {}
        chat_id = str((msg.get("chat") or {}).get("id", ""))
        text = (msg.get("text") or "").strip()
        if not chat_id or not text:
            return
        if CHAT_ID and chat_id != CHAT_ID:
            return

        low = text.lower()
        if low in ("/start", "/help"):
            tg_send(chat_id, "أرسل رمز سهم مثل NVDA\nالبوت يعطي حكم صارم للدخول")
            return

        tg_send(chat_id, "⏳ أحلل...")
        tg_send(chat_id, analyze(text))
    except Exception as e:
        log.error("handle: %s", e)

@app.route(f"/webhook/{SECRET}", methods=["POST"])
def webhook():
    if request.headers.get("X-Telegram-Bot-Api-Secret-Token") != SECRET:
        abort(403)
    handle_update(request.get_json(force=True, silent=True) or {})
    return "ok"

@app.route("/")
def home():
    return "Options Bot Running (Strict Fast)"

def start():
    threading.Thread(target=register_webhook, daemon=True).start()
    log.info("Strict fast bot started")

start()

if __name__ == "__main__":
    app.run(host="0.0.0.0", port=int(os.environ.get("PORT", 10000)))
