"""
Options Alert Bot - يرسل إشارات CALL / PUT على تليجرام (التنفيذ يدوي).
‎البيانات: yfinance (متأخرة ~15 دقيقة، غير رسمية).
"""
import os
import json
import logging
from datetime import datetime, date, timezone
from zoneinfo import ZoneInfo

import pandas as pd
import requests
import yfinance as yf

‎# ===================== الإعدادات =====================
TELEGRAM_TOKEN = os.environ["TELEGRAM_TOKEN"]  # من GitHub Secrets
CHAT_ID = os.environ["CHAT_ID"]                # من GitHub Secrets

SYMBOLS = ["^SPX", "SPY", "QQQ", "IWM", "AAPL", "TSLA", "NVDA",
           "AMZN", "MSFT", "META", "AMD", "GOOGL"]  # زد أو احذف براحتك

INTERVAL = "15m"          # فريم التحليل
SCAN_EVERY_SEC = 300      # كل 5 دقايق
COOLDOWN_MIN = 120        # لا تكرر نفس الإشارة على نفس الرمز قبل ساعتين

MIN_DTE, MAX_DTE = 2, 14  # أيام الانتهاء المقبولة
MIN_OPEN_INT = 100
MIN_VOLUME = 20
MAX_SPREAD_PCT = 0.15     # أقصى فرق بين bid/ask نسبة للسعر

STOP_LOSS_PCT = 0.30      # وقف الخسارة المقترح على سعر العقد
TAKE_PROFIT_PCT = 0.50    # الهدف المقترح
# =====================================================

NY = ZoneInfo("America/New_York")
logging.basicConfig(level=logging.INFO, format="%(asctime)s %(message)s")
STATE_FILE = "state.json"


def load_state() -> dict:
    try:
        with open(STATE_FILE) as f:
            return json.load(f)
    except Exception:
        return {}


def save_state(state: dict):
    with open(STATE_FILE, "w") as f:
        json.dump(state, f)


last_sent: dict = load_state()  # key -> ISO time (UTC)


def market_open() -> bool:
    now = datetime.now(NY)
    if now.weekday() >= 5:
        return False
    return now.replace(hour=9, minute=30, second=0) <= now <= now.replace(hour=16, minute=0, second=0)


def spx_night_open() -> bool:
‎    """جلسة SPX الليلية: 8:15م - 9:15ص بتوقيت نيويورك (أحد-جمعة)."""
    now = datetime.now(NY)
    wd, t = now.weekday(), now.hour * 60 + now.minute
    if t >= 20 * 60 + 15 and wd in (6, 0, 1, 2, 3):
        return True
    if t < 9 * 60 + 15 and wd in (0, 1, 2, 3, 4):
        return True
    return False


def es_basis() -> float:
‎    """الفرق بين عقد ES المستقبلي ومؤشر SPX (نحتاجه لتحديد السترايك بالليل)."""
    try:
        es = yf.Ticker("ES=F").history(period="5d", interval="15m")["Close"]
        spx = yf.Ticker("^SPX").history(period="5d", interval="15m")["Close"]
        both = pd.concat([es, spx], axis=1, join="inner").dropna()
        return float(both.iloc[-1, 0] - both.iloc[-1, 1]) if len(both) else 0.0
    except Exception:
        return 0.0


def rsi(series: pd.Series, n: int = 14) -> pd.Series:
    delta = series.diff()
    up = delta.clip(lower=0).ewm(alpha=1 / n, adjust=False).mean()
    down = (-delta.clip(upper=0)).ewm(alpha=1 / n, adjust=False).mean()
    return 100 - 100 / (1 + up / down)


def get_signal(symbol: str, use_futures: bool = False):
‎    """يرجع ('CALL'|'PUT', سعر, rsi) أو None"""
    src = "ES=F" if use_futures else symbol  # بالليل نحلل SPX من عقد ES
    df = yf.Ticker(src).history(period="5d", interval=INTERVAL)
    if df is None or len(df) < 30:
        return None
    close = df["Close"]
    ema9 = close.ewm(span=9, adjust=False).mean()
    ema21 = close.ewm(span=21, adjust=False).mean()
    r = rsi(close)

‎    # تقاطع خلال آخر شمعتين
    crossed_up = (ema9.iloc[-1] > ema21.iloc[-1]) and (ema9.iloc[-3] <= ema21.iloc[-3])
    crossed_dn = (ema9.iloc[-1] < ema21.iloc[-1]) and (ema9.iloc[-3] >= ema21.iloc[-3])
    price, rv = float(close.iloc[-1]), float(r.iloc[-1])

    if crossed_up and 50 <= rv <= 70:
        return "CALL", price, rv
    if crossed_dn and 30 <= rv <= 50:
        return "PUT", price, rv
    return None


def pick_contract(symbol: str, side: str, spot: float):
    t = yf.Ticker(symbol)
    today = date.today()
    for exp in t.options:
        dte = (datetime.strptime(exp, "%Y-%m-%d").date() - today).days
        if dte < MIN_DTE or dte > MAX_DTE:
            continue
        chain = t.option_chain(exp)
        tbl = chain.calls if side == "CALL" else chain.puts
        tbl = tbl.copy()
        tbl["dist"] = (tbl["strike"] - spot).abs()
        for _, row in tbl.sort_values("dist").head(3).iterrows():  # أقرب 3 سترايكات
            bid, ask = row["bid"], row["ask"]
            if not (bid > 0 and ask > 0):
                continue
            mid = (bid + ask) / 2
            if (ask - bid) / mid > MAX_SPREAD_PCT:
                continue
            if (row.get("openInterest") or 0) < MIN_OPEN_INT:
                continue
            if (row.get("volume") or 0) < MIN_VOLUME:
                continue
            return {
                "exp": exp, "dte": dte, "strike": float(row["strike"]),
                "bid": float(bid), "ask": float(ask), "mid": float(mid),
                "iv": float(row["impliedVolatility"]) * 100,
                "oi": int(row["openInterest"]), "vol": int(row["volume"]),
            }
    return None


def send(text: str):
    url = f"https://api.telegram.org/bot{TELEGRAM_TOKEN}/sendMessage"
    r = requests.post(url, data={"chat_id": CHAT_ID, "text": text, "parse_mode": "HTML"}, timeout=15)
    if not r.ok:
        logging.error("Telegram error: %s", r.text)


def build_message(symbol, side, price, rv, c, night=False):
    emoji = "🟢" if side == "CALL" else "🔴"
    night_note = "🌙 جلسة ليلية (السيولة أقل والسبريد أوسع)\n" if night else ""
    sl = c["mid"] * (1 - STOP_LOSS_PCT)
    tp = c["mid"] * (1 + TAKE_PROFIT_PCT)
    name = symbol.replace("^", "")
    return (
        f"{emoji} <b>{name} — {side}</b>\n"
        f"{night_note}"
        f"سعر السهم: {price:.2f} | RSI: {rv:.0f}\n\n"
        f"العقد: <b>{c['strike']:.0f} {side}</b> | انتهاء {c['exp']} ({c['dte']} يوم)\n"
        f"Bid/Ask: {c['bid']:.2f} / {c['ask']:.2f} | تقريبي: {c['mid']:.2f}\n"
        f"IV: {c['iv']:.0f}% | OI: {c['oi']} | Vol: {c['vol']}\n\n"
        f"🛑 وقف مقترح: {sl:.2f} (-{STOP_LOSS_PCT:.0%})\n"
        f"🎯 هدف مقترح: {tp:.2f} (+{TAKE_PROFIT_PCT:.0%})\n\n"
        f"⚠️ إشارة آلية للمعلومات فقط وليست توصية. البيانات متأخرة تقريباً 15 دقيقة. "
        f"التداول بالخيارات عالي المخاطر وقد تخسر كامل المبلغ."
    )


def scan():
    now = datetime.now(timezone.utc)
    regular = market_open()
    night = spx_night_open()
    for sym in SYMBOLS:
        try:
            use_fut = False
            if sym == "^SPX":
                if not (regular or night):
                    continue
                use_fut = not regular
            elif not regular:
                continue
            sig = get_signal(sym, use_fut)
            if not sig:
                continue
            side, price, rv = sig
            if use_fut:
                price -= es_basis()  # نحول سعر ES إلى مستوى SPX
            key = f"{sym}-{side}"
            if key in last_sent and (now - datetime.fromisoformat(last_sent[key])).total_seconds() < COOLDOWN_MIN * 60:
                continue
            contract = pick_contract(sym, side, price)
            if not contract:
                logging.info("%s %s: ما لقيت عقد سيولته كافية", sym, side)
                continue
            send(build_message(sym, side, price, rv, contract, night=use_fut))
            last_sent[key] = now.isoformat()
            logging.info("أرسلت %s", key)
        except Exception as e:
            logging.warning("%s خطأ: %s", sym, e)


if __name__ == "__main__":
    if market_open() or spx_night_open():
        scan()
        save_state(last_sent)
    else:
        logging.info("السوق مسكر")
