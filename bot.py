"""Options Bot - Stronger Version"""

import os
import time
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
    "SPY","QQQ","IWM","AAPL","MSFT","NVDA","TSLA","AMZN","META",
    "GOOGL","AMD","NFLX","AVGO","JPM","BAC","XOM","UNH","LLY",
    "V","MA","HD","COST","CRM","ORCL","ADBE","INTC","MU","QCOM"
]

SCAN_INTERVAL = 10 * 60
MIN_VOLUME = 600
MIN_OI = 250
MIN_PREMIUM = 120000
MIN_PRICE = 10.0
MAX_PRICE = 90.0

NY = ZoneInfo("America/New_York")
API = f"https://api.telegram.org/bot{TOKEN}"
DISCLAIMER = "⚠️ تحليل فني فقط وليس توصية. لا تخاطر بأكثر من 1-2% من رأس مالك."

app = Flask(__name__)
_sent_today = set()

def tg_send(chat_id, text):
    if not TOKEN or not chat_id: return
    for i in range(0, len(text), 4000):
        try:
            requests.post(f"{API}/sendMessage", json={"chat_id": chat_id, "text": text[i:i+4000]}, timeout=15)
        except Exception as e:
            log.error("send: %s", e)

def register_webhook():
    if not PUBLIC_URL or not TOKEN: return
    try:
        requests.post(f"{API}/setWebhook", json={
            "url": f"{PUBLIC_URL}/webhook/{SECRET}",
            "secret_token": SECRET,
            "allowed_updates": ["message"],
            "drop_pending_updates": True
        }, timeout=15)
    except Exception as e:
        log.error("webhook: %s", e)

def is_market_open():
    now = datetime.now(NY)
    if now.weekday() >= 5: return False
    m = now.hour*60 + now.minute
    return 9*60+30 <= m <= 16*60

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
        if hist.empty or len(hist) < 50: return None
        close = hist["Close"]
        price = float(close.iloc[-1])
        atr = float((hist["High"] - hist["Low"]).rolling(14).mean().iloc[-1])
        ema20 = float(close.ewm(span=20).mean().iloc[-1])
        ema50 = float(close.ewm(span=50).mean().iloc[-1])
        rsi = calc_rsi(close)
        return {"price": price, "atr": atr, "ema20": ema20, "ema50": ema50, "rsi": rsi, "ticker": t}
    except:
        return None

def get_news(t):
    try:
        news = t.news or []
        return [(n.get("title") or "")[:110] for n in news[:3] if n.get("title")]
    except:
        return []

def find_unusual(sym):
    try:
        data = get_ticker_data(sym)
        if not data: return []
        t = data["ticker"]
        options = t.options
        if not options: return []
        hits = []
        for exp in options[:2]:
            try:
                chain = t.option_chain(exp)
            except:
                continue
            for side, df in (("CALL", chain.calls), ("PUT", chain.puts)):
                df = df[(df["volume"] >= MIN_VOLUME) & (df["openInterest"] >= MIN_OI)].copy()
                if df.empty: continue
                df["prem"] = df["volume"] * df["lastPrice"] * 100
                df = df[(df["prem"] >= MIN_PREMIUM) & df["lastPrice"].between(MIN_PRICE, MAX_PRICE)]
                for _, r in df.iterrows():
                    key = f"{sym}_{r.get('contractSymbol','')}_{date.today()}"
                    if key in _sent_today: continue
                    hits.append({
                        "symbol": sym, "side": side, "strike": float(r["strike"]),
                        "expiry": exp, "vol": int(r["volume"]), "oi": int(r["openInterest"]),
                        "price": float(r["lastPrice"]), "prem": float(r["prem"]), "key": key
                    })
        hits.sort(key=lambda x: x["prem"], reverse=True)
        return hits[:4]
    except:
        return []

def get_gamma(t):
    try:
        opts = t.options
        if not opts: return None
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
    if not d: return f"بيانات غير كافية لـ {sym}"

    price, atr, rsi = d["price"], d["atr"], d["rsi"]
    e20, e50 = d["ema20"], d["ema50"]
    t = d["ticker"]

    score = 0
    reasons = []
    if price > e20 > e50:
        score += 3; reasons.append("ترند صاعد قوي")
    elif price < e20 < e50:
        score -= 3; reasons.append("ترند هابط قوي")
    if rsi >= 60:
        score += 1; reasons.append(f"RSI {rsi:.0f} قوي")
    elif rsi <= 40:
        score -= 1; reasons.append(f"RSI {rsi:.0f} ضعيف")

    side = "CALL" if score > 0 else "PUT" if score < 0 else "محايد"

    if side == "CALL":
        t1, t2, stop = price + atr*1.4, price + atr*2.8, price - atr*1.1
    elif side == "PUT":
        t1, t2, stop = price - atr*1.4, price - atr*2.8, price + atr*1.1
    else:
        t1 = t2 = stop = price

    rr = abs(t2 - price) / max(abs(price - stop), 0.01)

    lines = [
        f"📊 {sym}  ${price:.2f}",
        f"التقييم: {score:+d} | الاتجاه: {side}",
        f"RSI: {rsi:.0f}",
        "",
        "الأسباب:",
    ]
    lines += [f"• {r}" for r in reasons]
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
                exp = next((e for e in opts if 5 <= (date.fromisoformat(e)-date.today()).days <= 40), opts[0])
                ch = t.option_chain(exp)
                df = ch.calls if side=="CALL" else ch.puts
                df = df[(df["bid"]>0)&(df["ask"]>0)&df["lastPrice"].between(MIN_PRICE,MAX_PRICE)].copy()
                if not df.empty:
                    df["dist"] = abs(df["strike"]-price)
                    best = df.nsmallest(1,"dist").iloc[0]
                    spr = (best["ask"]-best["bid"])/best["lastPrice"] if best["lastPrice"] else 0
                    lines += [
                        "",
                        f"✅ عقد مقترح:",
                        f"{sym} {side} {best['strike']:g} | {exp}",
                        f"سعر: ${best['lastPrice']:.2f} | Bid/Ask ${best['bid']:.2f}/${best['ask']:.2f}",
                        f"سبريد: {spr*100:.1f}% | حجم: {int(best['volume']):,} | OI: {int(best['openInterest']):,}",
                    ]
        except:
            pass

    g = get_gamma(t)
    if g:
        lines.append("\n🧮 جاما:")
        if g.get("cw"): lines.append(f"Call Wall: ${g['cw']:g}")
        if g.get("pw"): lines.append(f"Put Wall: ${g['pw']:g}")

    news = get_news(t)
    if news:
        lines.append("\n📰 أخبار:")
        lines += [f"• {n}" for n in news]

    lines.append(f"\n{DISCLAIMER}")
    return "\n".join(lines)

def scanner_loop():
    global _sent_today
    while True:
        try:
            if is_market_open() and CHAT_ID:
                today = date.today()
                if not hasattr(scanner_loop,"day") or scanner_loop.day != today:
                    _sent_today.clear()
                    scanner_loop.day = today
                hits = []
                for s in WATCHLIST:
                    hits.extend(find_unusual(s))
                    time.sleep(0.4)
                if hits:
                    hits.sort(key=lambda x: x["prem"], reverse=True)
                    msg = "🔥 عقود قوية (تلقائي):\n\n"
                    for h in hits[:7]:
                        msg += f"{h['symbol']} {h['side']} {h['strike']:g} | {h['expiry']}\nحجم {h['vol']:,} | OI {h['oi']:,} | ${h['prem']/1e6:.1f}M\n\n"
                    msg += DISCLAIMER
                    tg_send(CHAT_ID, msg)
                    for h in hits: _sent_today.add(h["key"])
        except Exception as e:
            log.error("scan: %s", e)
        time.sleep(SCAN_INTERVAL)

def handle_update(update):
    try:
        msg = update.get("message") or {}
        chat_id = str((msg.get("chat") or {}).get("id",""))
        text = (msg.get("text") or "").strip()
        if not chat_id or not text: return
        if CHAT_ID and chat_id != CHAT_ID: return

        low = text.lower()
        if low in ("/start","/help"):
            tg_send(chat_id, "أرسل رمز سهم مثل NVDA\n/scan فحص يدوي\nالتلقائي يعمل كل 10 دقائق")
            return
        if low.startswith("/scan"):
            tg_send(chat_id, "⏳ أفحص...")
            res = []
            for s in WATCHLIST[:8]:
                h = find_unusual(s)
                if h: res.append(f"{s}: {len(h)}")
            tg_send(chat_id, "نتائج:\n" + ("\n".join(res) if res else "ما فيه شيء قوي حالياً"))
            return
        tg_send(chat_id, "⏳ أحلل...")
        tg_send(chat_id, analyze(text))
    except Exception as e:
        log.error("handle: %s", e)

@app.route(f"/webhook/{SECRET}", methods=["POST"])
def webhook():
    if request.headers.get("X-Telegram-Bot-Api-Secret-Token") != SECRET: abort(403)
    handle_update(request.get_json(force=True, silent=True) or {})
    return "ok"

@app.route("/")
def home():
    return "Options Bot Running"

def start():
    threading.Thread(target=register_webhook, daemon=True).start()
    threading.Thread(target=scanner_loop, daemon=True).start()
    log.info("Strong bot started")

start()

if __name__ == "__main__":
    app.run(host="0.0.0.0", port=int(os.environ.get("PORT", 10000)))
