"""Options Bot - Stronger Manual Analysis"""

import os
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

NY = ZoneInfo("America/New_York")
API = f"https://api.telegram.org/bot{TOKEN}"
DISCLAIMER = "⚠️ تحليل فني فقط وليس توصية استثمارية. لا تخاطر بأكثر من 1-2% من رأس مالك في الصفقة."

app = Flask(__name__)

NEGATIVE_WORDS = ["lawsuit", "sec", "investigation", "downgrade", "miss", "cut", "warning", "bankruptcy", "probe", "fine"]

def tg_send(chat_id, text):
    if not TOKEN or not chat_id:
        return
    for i in range(0, len(text), 4000):
        try:
            requests.post(f"{API}/sendMessage", json={"chat_id": chat_id, "text": text[i:i+4000]}, timeout=15)
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
        }, timeout=15)
    except Exception as e:
        log.error("webhook: %s", e)

def calc_rsi(closes, period=14):
    delta = closes.diff()
    gain = delta.clip(lower=0).rolling(period).mean()
    loss = (-delta.clip(upper=0)).rolling(period).mean()
    rs = gain / loss
    rsi = 100 - (100 / (1 + rs))
    return float(rsi.iloc[-1]) if not rsi.empty else 50

def get_ticker_data(sym):
    try:
        t = yf.Ticker(sym)
        hist = t.history(period="6mo")
        if hist.empty or len(hist) < 50:
            return None
        close = hist["Close"]
        price = float(close.iloc[-1])
        atr = float((hist["High"] - hist["Low"]).rolling(14).mean().iloc[-1])
        ema20 = float(close.ewm(span=20).mean().iloc[-1])
        ema50 = float(close.ewm(span=50).mean().iloc[-1])
        rsi = calc_rsi(close)
        vol_avg = float(hist["Volume"].tail(20).mean())
        last_vol = float(hist["Volume"].iloc[-1])
        vol_ratio = last_vol / vol_avg if vol_avg > 0 else 1
        return {
            "price": price, "atr": atr, "ema20": ema20, "ema50": ema50,
            "rsi": rsi, "vol_ratio": vol_ratio, "ticker": t
        }
    except Exception as e:
        log.warning("data %s: %s", sym, e)
        return None

def get_news(t):
    try:
        news = t.news or []
        items = []
        for n in news[:5]:
            title = (n.get("title") or "")[:120]
            if title:
                items.append(title)
        return items
    except:
        return []

def has_negative_news(news_list):
    text = " ".join(news_list).lower()
    return any(w in text for w in NEGATIVE_WORDS)

def get_gamma(t):
    try:
        opts = t.options
        if not opts:
            return None
        ch = t.option_chain(opts[0])
        c = ch.calls[ch.calls["openInterest"] > 0]
        p = ch.puts[ch.puts["openInterest"] > 0]
        cw = float(c.loc[c["openInterest"].idxmax()]["strike"]) if not c.empty else None
        pw = float(p.loc[p["openInterest"].idxmax()]["strike"]) if not p.empty else None
        return {"cw": cw, "pw": pw}
    except:
        return None

def analyze(sym):
    sym = sym.upper().strip()
    d = get_ticker_data(sym)
    if not d:
        return f"بيانات غير كافية لـ {sym}"

    price, atr, rsi = d["price"], d["atr"], d["rsi"]
    e20, e50 = d["ema20"], d["ema50"]
    vol_ratio = d["vol_ratio"]
    t = d["ticker"]

    score = 0
    reasons = []
    warnings = []

    # الترند
    if price > e20 > e50:
        score += 3
        reasons.append("ترند صاعد قوي (السعر فوق EMA20 وEMA50)")
    elif price < e20 < e50:
        score -= 3
        reasons.append("ترند هابط قوي")
        warnings.append("الترند هابط")

    # RSI
    if 50 <= rsi <= 70:
        score += 1
        reasons.append(f"RSI {rsi:.0f} في منطقة قوة")
    elif rsi > 75:
        score -= 1
        warnings.append(f"RSI {rsi:.0f} تشبع شراء")
    elif rsi < 35:
        score += 1
        reasons.append(f"RSI {rsi:.0f} تشبع بيع (احتمال ارتداد)")

    # الحجم
    if vol_ratio >= 1.3:
        score += 1
        reasons.append(f"حجم أعلى من المتوسط ({vol_ratio:.1f}x)")
    elif vol_ratio < 0.7:
        warnings.append("حجم ضعيف")

    side = "CALL" if score > 1 else "PUT" if score < -1 else "محايد"

    if side == "CALL":
        t1, t2, stop = price + atr * 1.3, price + atr * 2.6, price - atr * 1.0
    elif side == "PUT":
        t1, t2, stop = price - atr * 1.3, price - atr * 2.6, price + atr * 1.0
    else:
        t1 = t2 = stop = price

    rr = abs(t2 - price) / max(abs(price - stop), 0.01)

    # الحكم النهائي
    news = get_news(t)
    bad_news = has_negative_news(news)

    if side == "CALL" and score >= 3 and not bad_news and rr >= 1.5:
        verdict = "✅ دخول قوي (CALL)"
    elif side == "PUT" and score <= -3 and not bad_news and rr >= 1.5:
        verdict = "✅ دخول قوي (PUT)"
    elif bad_news:
        verdict = "⛔ توقف - فيه أخبار سلبية"
    elif abs(score) < 2:
        verdict = "⏳ انتظر تأكيد أقوى"
    else:
        verdict = "⚠️ دخول متوسط (بحذر)"

    lines = [
        f"📊 {sym}  ${price:.2f}",
        f"الحكم: {verdict}",
        f"التقييم: {score:+d} | الاتجاه: {side}",
        f"RSI: {rsi:.0f} | حجم: {vol_ratio:.1f}x",
        "",
        "التأكيدات:",
    ]
    lines += [f"• {r}" for r in reasons]
    if warnings:
        lines.append("\nتحذيرات:")
        lines += [f"• {w}" for w in warnings]

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
                exp = next((e for e in opts if 5 <= (date.fromisoformat(e) - date.today()).days <= 40), opts[0])
                ch = t.option_chain(exp)
                df = ch.calls if side == "CALL" else ch.puts
                df = df[(df["bid"] > 0) & (df["ask"] > 0) & df["lastPrice"].between(10, 90)].copy()
                if not df.empty:
                    df["dist"] = abs(df["strike"] - price)
                    best = df.nsmallest(1, "dist").iloc[0]
                    spr = (best["ask"] - best["bid"]) / best["lastPrice"] if best["lastPrice"] else 0
                    lines += [
                        "",
                        "عقد مقترح:",
                        f"{sym} {side} {best['strike']:g} | {exp}",
                        f"سعر: ${best['lastPrice']:.2f} | Bid/Ask ${best['bid']:.2f}/${best['ask']:.2f}",
                        f"سبريد: {spr*100:.1f}% | حجم: {int(best['volume']):,} | OI: {int(best['openInterest']):,}",
                    ]
        except Exception as e:
            log.warning("contract: %s", e)

    g = get_gamma(t)
    if g:
        lines.append("\n🧮 جاما:")
        if g.get("cw"): lines.append(f"Call Wall: ${g['cw']:g}")
        if g.get("pw"): lines.append(f"Put Wall: ${g['pw']:g}")

    if news:
        lines.append("\n📰 أخبار:")
        lines += [f"• {n}" for n in news[:3]]
        if bad_news:
            lines.append("⚠️ فيه أخبار قد تكون سلبية - راجعها قبل الدخول")

    lines.append(f"\n{DISCLAIMER}")
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

        low = text.lower()
        if low in ("/start", "/help"):
            tg_send(chat_id, "أرسل رمز سهم مثل NVDA أو TSLA\nالبوت يعطيك حكم دخول + أهداف + تحذيرات")
            return

        tg_send(chat_id, "⏳ أحلل...")
        tg_send(chat_id, analyze(text))
    except Exception as e:
        log.error("handle: %s", e)
        if CHAT_ID:
            tg_send(CHAT_ID, "⚠️ خطأ مؤقت، جرب بعد ثواني.")

@app.route(f"/webhook/{SECRET}", methods=["POST"])
def webhook():
    if request.headers.get("X-Telegram-Bot-Api-Secret-Token") != SECRET:
        abort(403)
    handle_update(request.get_json(force=True, silent=True) or {})
    return "ok"

@app.route("/")
def home():
    return "Options Bot Running (Manual Strong)"

def start():
    threading.Thread(target=register_webhook, daemon=True).start()
    log.info("Manual strong bot started")

start()

if __name__ == "__main__":
    app.run(host="0.0.0.0", port=int(os.environ.get("PORT", 10000)))
