"""Options Bot: Telegram webhook bot + unusual options flow scanner + multi-factor rating.

All secrets come from environment variables. Nothing sensitive lives in this file.

Required env vars:
  TELEGRAM_TOKEN    bot token from @BotFather
  WEBHOOK_SECRET    random string, letters/digits/_/- only (used in the webhook URL and as Telegram secret_token)

Recommended env vars:
  TELEGRAM_CHAT_ID  your chat id. Only this chat can use the bot and receive alerts.
  PUBLIC_URL        e.g. https://your-service.onrender.com (webhook is registered automatically at startup)

Optional env vars (flow scanner):
  WATCHLIST         comma separated tickers (default below)
  SCAN_INTERVAL_MIN minutes between automatic scans (default 15)
  MIN_VOLUME        minimum contract volume (default 500)
  MIN_VOL_OI        minimum volume / open interest ratio (default 2.0)
  MIN_PREMIUM       minimum premium traded in USD = volume * price * 100 (default 100000)
  MAX_ALERTS        max contracts per alert message (default 8)
  EXPIRIES_TO_SCAN  how many nearest expiries to scan per ticker (default 3)

Optional env vars (rating / trade plan):
  MIN_SCORE_ALERT   automatic alerts only for tickers whose |score| >= this (default 0 = no filter)
  STRONG_SCORE      |score| needed for a "strong" rating (default 5, max possible is 8)
  MIN_DTE / MAX_DTE days to expiry range for the suggested contract (default 7 / 45)
  MAX_SPREAD        max bid/ask spread as a fraction of mid price (default 0.12)
  ATR_STOP          stop distance in ATRs (default 1.5)
  ATR_TARGET        final target distance in ATRs (default 3.0)
"""

import hmac
import logging
import math
import os
import re
import threading
import time
from datetime import date, datetime
from zoneinfo import ZoneInfo

import pandas as pd
import requests
import yfinance as yf
from flask import Flask, abort, request

logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
logging.getLogger("urllib3").setLevel(logging.WARNING)
logging.getLogger("yfinance").setLevel(logging.WARNING)
log = logging.getLogger("options_bot")


# ----------------------------------------------------------------------------
# Config
# ----------------------------------------------------------------------------
def _env(name, default="", required=False):
    value = os.environ.get(name, default)
    if required and not value:
        raise RuntimeError(f"Missing required environment variable: {name}")
    return value


TOKEN = _env("TELEGRAM_TOKEN", required=True)
SECRET = _env("WEBHOOK_SECRET", required=True)
if not re.fullmatch(r"[A-Za-z0-9_-]{8,256}", SECRET):
    raise RuntimeError("WEBHOOK_SECRET must be 8-256 chars: letters, digits, _ or - only")

CHAT_ID = str(_env("TELEGRAM_CHAT_ID")).strip()
PUBLIC_URL = _env("PUBLIC_URL").strip().rstrip("/")
WATCHLIST = [
    s.strip().upper()
    for s in _env("WATCHLIST", "SPY,QQQ,AAPL,TSLA,NVDA,AMD,AMZN,META,MSFT").split(",")
    if s.strip()
]
SCAN_INTERVAL = int(_env("SCAN_INTERVAL_MIN", "15")) * 60
MIN_VOLUME = int(_env("MIN_VOLUME", "500"))
MIN_VOL_OI = float(_env("MIN_VOL_OI", "2.0"))
MIN_PREMIUM = float(_env("MIN_PREMIUM", "100000"))
MAX_ALERTS = int(_env("MAX_ALERTS", "8"))
EXPIRIES_TO_SCAN = int(_env("EXPIRIES_TO_SCAN", "3"))
STRIKE_RANGE = 0.15  # only look at strikes within +-15% of the underlying price

MIN_SCORE_ALERT = int(_env("MIN_SCORE_ALERT", "0"))
STRONG_SCORE = int(_env("STRONG_SCORE", "5"))
MAX_SCORE = 8
MIN_DTE = int(_env("MIN_DTE", "7"))
MAX_DTE = int(_env("MAX_DTE", "45"))
MAX_SPREAD = float(_env("MAX_SPREAD", "0.12"))
ATR_STOP = float(_env("ATR_STOP", "1.5"))
ATR_TARGET = float(_env("ATR_TARGET", "3.0"))

API = f"https://api.telegram.org/bot{TOKEN}"
NY = ZoneInfo("America/New_York")
DISCLAIMER = "⚠️ معلومات وتحليل فني فقط وليست توصية استثمارية. لا تخاطر بأكثر من 1-2% من رأس مالك في الصفقة الواحدة."

app = Flask(__name__)


# ----------------------------------------------------------------------------
# Telegram helpers (never log the URL: it contains the token)
# ----------------------------------------------------------------------------
def tg_send(chat_id, text):
    for i in range(0, len(text), 4000):
        try:
            r = requests.post(
                f"{API}/sendMessage",
                json={"chat_id": chat_id, "text": text[i : i + 4000]},
                timeout=15,
            )
            if not r.ok:
                log.error("sendMessage failed: HTTP %s", r.status_code)
        except Exception as e:  # noqa: BLE001
            log.error("sendMessage error: %s", type(e).__name__)


def register_webhook():
    if not PUBLIC_URL:
        log.warning("PUBLIC_URL not set: webhook not registered automatically")
        return
    try:
        r = requests.post(
            f"{API}/setWebhook",
            json={
                "url": f"{PUBLIC_URL}/webhook/{SECRET}",
                "secret_token": SECRET,
                "allowed_updates": ["message"],
                "drop_pending_updates": True,
            },
            timeout=15,
        )
        log.info("setWebhook: HTTP %s ok=%s", r.status_code, r.json().get("ok"))
    except Exception as e:  # noqa: BLE001
        log.error("setWebhook error: %s", type(e).__name__)


# ----------------------------------------------------------------------------
# Generic helpers
# ----------------------------------------------------------------------------
def is_market_open():
    now = datetime.now(NY)
    if now.weekday() >= 5:
        return False
    minutes = now.hour * 60 + now.minute
    return 9 * 60 + 35 <= minutes <= 16 * 60


def money(x):
    if x >= 1_000_000:
        return f"${x / 1_000_000:.1f}M"
    if x >= 1_000:
        return f"${x / 1_000:.0f}K"
    return f"${x:.0f}"


def _num(x, default=0.0):
    try:
        if x is None or (isinstance(x, float) and math.isnan(x)):
            return default
        return float(x)
    except (TypeError, ValueError):
        return default


def calc_rsi(closes, period=14):
    deltas = closes.diff().dropna()
    gain = deltas.clip(lower=0).rolling(period).mean().iloc[-1]
    loss = (-deltas.clip(upper=0)).rolling(period).mean().iloc[-1]
    if math.isnan(gain) or math.isnan(loss):
        return None
    if loss == 0:
        return 100.0
    return 100 - 100 / (1 + gain / loss)


# ----------------------------------------------------------------------------
# Unusual options flow scanner
# ----------------------------------------------------------------------------
def scan_ticker(sym):
    """Return unusual option contracts for one ticker (not deduped)."""
    t = yf.Ticker(sym)
    hist = t.history(period="1d")
    if hist.empty:
        return []
    price = float(hist["Close"].iloc[-1])
    lo, hi = price * (1 - STRIKE_RANGE), price * (1 + STRIKE_RANGE)

    hits = []
    for exp in list(t.options)[:EXPIRIES_TO_SCAN]:
        try:
            chain = t.option_chain(exp)
        except Exception as e:  # noqa: BLE001
            log.warning("chain %s %s failed: %s", sym, exp, type(e).__name__)
            continue
        for side, df in (("CALL", chain.calls), ("PUT", chain.puts)):
            for row in df.itertuples():
                strike = _num(row.strike)
                vol = _num(row.volume)
                oi = _num(row.openInterest)
                last = _num(row.lastPrice)
                if not (lo <= strike <= hi) or vol < MIN_VOLUME or last <= 0:
                    continue
                ratio = vol / oi if oi > 0 else float("inf")
                premium = vol * last * 100
                if ratio < MIN_VOL_OI or premium < MIN_PREMIUM:
                    continue
                hits.append(
                    {
                        "symbol": sym,
                        "contract": row.contractSymbol,
                        "side": side,
                        "strike": strike,
                        "expiry": exp,
                        "volume": int(vol),
                        "oi": int(oi),
                        "ratio": ratio,
                        "last": last,
                        "premium": premium,
                        "underlying": price,
                    }
                )
    return hits


_seen = {}  # contract -> last alerted volume
_seen_day = None
_seen_lock = threading.Lock()


def _only_new(hits):
    """Alert each contract once per day, again only if its volume doubled."""
    global _seen_day
    today = datetime.now(NY).date()
    fresh = []
    with _seen_lock:
        if _seen_day != today:
            _seen.clear()
            _seen_day = today
        for h in hits:
            prev = _seen.get(h["contract"], 0)
            if prev == 0 or h["volume"] >= 2 * prev:
                _seen[h["contract"]] = h["volume"]
                fresh.append(h)
    return fresh


def scan_all(dedupe):
    hits = []
    for sym in WATCHLIST:
        try:
            hits.extend(scan_ticker(sym))
        except Exception as e:  # noqa: BLE001
            log.warning("scan %s failed: %s", sym, type(e).__name__)
    hits.sort(key=lambda h: h["premium"], reverse=True)
    if dedupe:
        hits = _only_new(hits)
    return hits[:MAX_ALERTS]


# ----------------------------------------------------------------------------
# Multi-factor rating (-8 .. +8)
#   trend (EMA20/EMA50 stack)    +-2
#   price vs SMA200              +-1
#   MACD histogram + direction   +-1
#   daily RSI zone               +-1
#   volume spike with direction  +-1
#   options flow (call vs put $) +-2
# A "strong" rating also requires the EMA trend to agree with the direction.
# ----------------------------------------------------------------------------
def technicals(t):
    d = t.history(period="1y", interval="1d")
    if d is None or d.empty or len(d) < 60:
        return None
    d = d.dropna(subset=["Close"])
    c, v = d["Close"], d["Volume"]
    ema20 = c.ewm(span=20, adjust=False).mean()
    ema50 = c.ewm(span=50, adjust=False).mean()
    sma200 = c.rolling(200).mean().iloc[-1]
    macd_line = c.ewm(span=12, adjust=False).mean() - c.ewm(span=26, adjust=False).mean()
    hist = macd_line - macd_line.ewm(span=9, adjust=False).mean()
    tr = pd.concat(
        [d["High"] - d["Low"], (d["High"] - c.shift()).abs(), (d["Low"] - c.shift()).abs()],
        axis=1,
    ).max(axis=1)
    atr = tr.rolling(14).mean().iloc[-1]
    if math.isnan(atr) or atr <= 0:
        return None

    # volume: use the last COMPLETED day (today's bar is partial while the market is open)
    i = -2 if is_market_open() else -1
    base = v.iloc[i - 20 : i].mean()
    vol_ratio = float(v.iloc[i] / base) if base and base > 0 else None
    day_up = bool(c.iloc[i] > c.iloc[i - 1])

    return {
        "price": float(c.iloc[-1]),
        "ema20": float(ema20.iloc[-1]),
        "ema50": float(ema50.iloc[-1]),
        "sma200": None if math.isnan(sma200) else float(sma200),
        "macd_hist": float(hist.iloc[-1]),
        "macd_hist_prev": float(hist.iloc[-2]),
        "rsi": calc_rsi(c),
        "atr": float(atr),
        "vol_ratio": vol_ratio,
        "day_up": day_up,
    }


def pick_contract(t, side, price):
    """Pick a liquid near-the-money contract between MIN_DTE and MAX_DTE from the real chain."""
    today = datetime.now(NY).date()
    for exp in t.options:
        dte = (date.fromisoformat(exp) - today).days
        if dte < MIN_DTE:
            continue
        if dte > MAX_DTE:
            break
        try:
            chain = t.option_chain(exp)
        except Exception:  # noqa: BLE001
            continue
        df = chain.calls if side == "CALL" else chain.puts
        df = df[(df["bid"] > 0) & (df["ask"] > 0) & (df["openInterest"] >= 100)]
        if df.empty:
            continue
        row = df.loc[(df["strike"] - price).abs().idxmin()]
        bid, ask = float(row["bid"]), float(row["ask"])
        mid = (bid + ask) / 2
        spread = (ask - bid) / mid if mid > 0 else 1.0
        if spread > MAX_SPREAD:
            continue
        return {
            "expiry": exp,
            "dte": dte,
            "strike": float(row["strike"]),
            "bid": bid,
            "ask": ask,
            "spread": spread,
            "iv": _num(row.get("impliedVolatility")),
            "oi": int(_num(row["openInterest"])),
            "volume": int(_num(row.get("volume"))),
            "side": side,
        }
    return None


def earnings_in_window(t, until_iso):
    """Return an earnings date between today and the contract expiry, if known."""
    try:
        cal = t.calendar
        dates = cal.get("Earnings Date") or []
        today = datetime.now(NY).date()
        until = date.fromisoformat(until_iso)
        for d in dates:
            if today <= d <= until:
                return d
    except Exception:  # noqa: BLE001
        pass
    return None


def evaluate(sym, flow_hits=None):
    """Score a ticker and build a trade plan. Returns None when data is missing."""
    t = yf.Ticker(sym)
    tech = technicals(t)
    if tech is None:
        return None

    score, trend_pts, reasons = 0, 0, []
    p = tech["price"]

    # 1) trend
    if p > tech["ema20"] > tech["ema50"]:
        trend_pts = 2
        reasons.append("ترند صاعد: السعر فوق EMA20 وEMA20 فوق EMA50 (+2)")
    elif p < tech["ema20"] < tech["ema50"]:
        trend_pts = -2
        reasons.append("ترند هابط: السعر تحت EMA20 وEMA20 تحت EMA50 (-2)")
    else:
        reasons.append("الترند غير واضح (EMA متداخلة) (0)")
    score += trend_pts

    # 2) long-term position
    if tech["sma200"] is not None:
        if p > tech["sma200"]:
            score += 1
            reasons.append("السعر فوق SMA-200 (+1)")
        else:
            score -= 1
            reasons.append("السعر تحت SMA-200 (-1)")

    # 3) MACD
    h, hp = tech["macd_hist"], tech["macd_hist_prev"]
    if h > 0 and h > hp:
        score += 1
        reasons.append("زخم MACD إيجابي ويتزايد (+1)")
    elif h < 0 and h < hp:
        score -= 1
        reasons.append("زخم MACD سلبي ويتزايد (-1)")

    # 4) RSI zone
    r = tech["rsi"]
    if r is not None:
        if r >= 75:
            score -= 1
            reasons.append(f"RSI {r:.0f} تشبع شراء، خطر تصحيح (-1)")
        elif r <= 25:
            score += 1
            reasons.append(f"RSI {r:.0f} تشبع بيع، احتمال ارتداد (+1)")
        elif r >= 50:
            score += 1
            reasons.append(f"RSI {r:.0f} في منطقة قوة (+1)")
        else:
            score -= 1
            reasons.append(f"RSI {r:.0f} في منطقة ضعف (-1)")

    # 5) volume spike with direction
    vr = tech["vol_ratio"]
    if vr is not None and vr >= 1.5:
        if tech["day_up"]:
            score += 1
            reasons.append(f"حجم أعلى من المعتاد {vr:.1f}x مع إغلاق صاعد (+1)")
        else:
            score -= 1
            reasons.append(f"حجم أعلى من المعتاد {vr:.1f}x مع إغلاق هابط (-1)")

    # 6) unusual options flow
    flow_pts = 0
    if flow_hits:
        calls = sum(x["premium"] for x in flow_hits if x["side"] == "CALL")
        puts = sum(x["premium"] for x in flow_hits if x["side"] == "PUT")
        if calls > 0 and calls >= 1.5 * puts:
            flow_pts = 2
            reasons.append(f"تدفق أوبشنز غير عادي لصالح CALL ({money(calls)} مقابل {money(puts)}) (+2)")
        elif puts > 0 and puts >= 1.5 * calls:
            flow_pts = -2
            reasons.append(f"تدفق أوبشنز غير عادي لصالح PUT ({money(puts)} مقابل {money(calls)}) (-2)")
        else:
            reasons.append("تدفق الأوبشنز متعادل بين CALL وPUT (0)")
    else:
        reasons.append("ما فيه تدفق أوبشنز غير عادي حالياً (0)")
    score += flow_pts

    side = "CALL" if score > 0 else "PUT"
    strong = abs(score) >= STRONG_SCORE and abs(trend_pts) == 2 and trend_pts * score > 0
    if strong:
        label = "قوية"
    elif abs(score) >= 3:
        label = "متوسطة"
    else:
        label = "محايدة"

    out = {
        "symbol": sym,
        "price": p,
        "score": score,
        "side": side,
        "label": label,
        "strong": strong,
        "reasons": reasons,
        "atr": tech["atr"],
        "plan": None,
        "contract": None,
        "earnings": None,
    }

    if label != "محايدة":
        sign = 1 if side == "CALL" else -1
        atr = tech["atr"]
        out["plan"] = {
            "entry": p,
            "stop": p - sign * ATR_STOP * atr,
            "t1": p + sign * (ATR_TARGET / 2) * atr,
            "t2": p + sign * ATR_TARGET * atr,
            "rr": ATR_TARGET / ATR_STOP,
        }
        try:
            out["contract"] = pick_contract(t, side, p)
            if out["contract"]:
                out["earnings"] = earnings_in_window(t, out["contract"]["expiry"])
        except Exception as e:  # noqa: BLE001
            log.warning("contract pick failed %s: %s", sym, type(e).__name__)
    return out


def rate_symbols(hits):
    by = {}
    for h in hits:
        by.setdefault(h["symbol"], []).append(h)
    ratings = {}
    for sym, hs in by.items():
        try:
            ratings[sym] = evaluate(sym, hs)
        except Exception as e:  # noqa: BLE001
            log.warning("rate %s failed: %s", sym, type(e).__name__)
            ratings[sym] = None
    return ratings


# ----------------------------------------------------------------------------
# Formatting
# ----------------------------------------------------------------------------
def rating_line(ev):
    if ev is None:
        return "التقييم غير متوفر"
    icon = {"قوية": "💪", "متوسطة": "👍", "محايدة": "⏸️"}[ev["label"]]
    if ev["label"] == "محايدة":
        return f"{icon} {ev['score']:+d}/{MAX_SCORE} محايدة، لا توصية"
    arrow = "🟢" if ev["side"] == "CALL" else "🔴"
    return f"{icon} {ev['score']:+d}/{MAX_SCORE} {ev['side']} {ev['label']} {arrow}"


def format_hit(h):
    ratio = "جديد (OI=0)" if h["oi"] == 0 else f"{h['ratio']:.1f}x"
    icon = "🟢" if h["side"] == "CALL" else "🔴"
    return (
        f"{icon} {h['symbol']} {h['side']}  Strike {h['strike']:g}  انتهاء {h['expiry']}\n"
        f"   حجم: {h['volume']:,} | OI: {h['oi']:,} | النسبة: {ratio}\n"
        f"   سعر العقد: ${h['last']:.2f} | قيمة التداول: {money(h['premium'])}\n"
        f"   سعر السهم: ${h['underlying']:.2f}"
    )


def format_hits(hits, ratings=None, title="🔥 نشاط أوبشنز غير عادي"):
    body = "\n\n".join(format_hit(h) for h in hits)
    out = f"{title}\n\n{body}"
    if ratings:
        lines = [f"{sym}: {rating_line(ev)}" for sym, ev in ratings.items()]
        out += "\n\n🧠 التقييم (من -8 إلى +8):\n" + "\n".join(lines)
        out += "\nللتفاصيل والخطة أرسل رمز السهم."
    return f"{out}\n\n{DISCLAIMER}\nالبيانات من Yahoo وقد تتأخر ~15 دقيقة."


def format_evaluation(ev):
    lines = [f"📊 {ev['symbol']}  ${ev['price']:.2f}", f"التقييم: {rating_line(ev)}", ""]
    lines.append("الأسباب:")
    lines.extend(f"• {r}" for r in ev["reasons"])

    plan, c = ev["plan"], ev["contract"]
    if plan is None:
        lines.append("\nالإشارات غير متوافقة بشكل كافٍ، الأفضل الانتظار.")
    else:
        if not ev["strong"]:
            lines.append("\nℹ️ الإشارة متوسطة وليست قوية، فخفف حجم الصفقة أو انتظر تأكيد.")
        lines.append(
            "\n🎯 خطة على سعر السهم (بناءً على ATR):\n"
            f"دخول: ~${plan['entry']:.2f}\n"
            f"وقف الخسارة: ${plan['stop']:.2f}\n"
            f"هدف 1: ${plan['t1']:.2f}\n"
            f"هدف 2: ${plan['t2']:.2f}\n"
            f"المخاطرة:العائد = 1:{plan['rr']:.1f}"
        )
        if c:
            lines.append(
                f"\n📝 عقد مقترح ({c['side']} قريب من السعر):\n"
                f"{ev['symbol']} {c['side']} {c['strike']:g}  انتهاء {c['expiry']} ({c['dte']} يوم)\n"
                f"Bid/Ask: ${c['bid']:.2f}/${c['ask']:.2f} | سبريد {c['spread'] * 100:.1f}%\n"
                f"IV: {c['iv'] * 100:.0f}% | OI: {c['oi']:,} | حجم اليوم: {c['volume']:,}"
            )
            if ev["earnings"]:
                lines.append(f"⚠️ فيه إعلان أرباح بتاريخ {ev['earnings']} قبل الانتهاء، والتقلب بعده يكون عالي.")
        else:
            lines.append(
                f"\nما لقيت عقد سيولته مناسبة ({MIN_DTE}-{MAX_DTE} يوم وسبريد أقل من {MAX_SPREAD * 100:.0f}%)."
            )
    lines.append(f"\n{DISCLAIMER}")
    return "\n".join(lines)


def analyze(raw):
    sym = raw.upper().strip().lstrip("$")
    if not re.fullmatch(r"[A-Z][A-Z.\-]{0,5}", sym):
        return "اكتب رمز سهم صحيح مثل: AAPL"

    try:
        flow = scan_ticker(sym)
    except Exception as e:  # noqa: BLE001
        log.warning("analyze flow failed: %s", type(e).__name__)
        flow = []
    ev = evaluate(sym, flow)
    if ev is None:
        return f"ما لقيت بيانات كافية للرمز {sym}"

    text = format_evaluation(ev)
    top = sorted(flow, key=lambda h: h["premium"], reverse=True)[:3]
    if top:
        text = text.replace(
            f"\n\n{DISCLAIMER}",
            "\n\n🔥 أعلى عقود نشاطاً:\n\n" + "\n\n".join(format_hit(h) for h in top) + f"\n\n{DISCLAIMER}",
        )
    return text


# ----------------------------------------------------------------------------
# Update handling
# ----------------------------------------------------------------------------
HELP = (
‎    "أوامر البوت:\n"
‎    "• أرسل رمز سهم (مثل TSLA) ويجيك تقييم + خطة + عقد مقترح\n"
    "• /scan فحص فوري لقائمة المراقبة مع التقييم\n"
    "• /watch عرض قائمة المراقبة\n"
    "• /help هذي الرسالة\n\n"
    f"الفحص التلقائي كل {SCAN_INTERVAL // 60} دقيقة أثناء جلسة السوق الأمريكي."
)


def handle_update(update):
    try:
        msg = update.get("message") or {}
        chat_id = str((msg.get("chat") or {}).get("id", ""))
        text = (msg.get("text") or "").strip()
        if not chat_id or not text:
            return

        if not CHAT_ID:
            tg_send(chat_id, f"رقم محادثتك: {chat_id}\nحطه في متغير TELEGRAM_CHAT_ID على الاستضافة.")
            return
        if chat_id != CHAT_ID:
            return  # ignore strangers silently

        cmd = text.split()[0].split("@")[0].lower()
        if cmd in ("/start", "/help"):
            tg_send(chat_id, HELP)
        elif cmd == "/watch":
            tg_send(chat_id, "قائمة المراقبة:\n" + ", ".join(WATCHLIST))
        elif cmd == "/scan":
            tg_send(chat_id, "⏳ أفحص السوق...")
            hits = scan_all(dedupe=False)
            if hits:
                tg_send(chat_id, format_hits(hits, rate_symbols(hits)))
            else:
                tg_send(chat_id, "ما فيه نشاط غير عادي حالياً حسب الفلاتر.")
        elif text.startswith("/"):
            tg_send(chat_id, HELP)
        else:
            tg_send(chat_id, "⏳ أحلل...")
            tg_send(chat_id, analyze(text))
    except Exception as e:  # noqa: BLE001
        log.error("handle_update error: %s", type(e).__name__)


# ----------------------------------------------------------------------------
# Web routes
# ----------------------------------------------------------------------------
def _same(a, b):
    return hmac.compare_digest(a.encode(), b.encode())


@app.get("/")
@app.get("/health")
def health():
    return "Bot is active!"


@app.post("/webhook/<path_secret>")
def webhook(path_secret):
    if not _same(path_secret, SECRET):
        abort(404)
    if not _same(request.headers.get("X-Telegram-Bot-Api-Secret-Token", ""), SECRET):
        abort(403)
    update = request.get_json(silent=True) or {}
    threading.Thread(target=handle_update, args=(update,), daemon=True).start()
    return "ok"


# ----------------------------------------------------------------------------
# Background scanner
# ----------------------------------------------------------------------------
def scanner_loop():
    while True:
        try:
            if CHAT_ID and is_market_open():
                hits = scan_all(dedupe=True)
                if hits:
                    ratings = rate_symbols(hits)
                    if MIN_SCORE_ALERT > 0:
                        keep = {
                            s for s, ev in ratings.items() if ev is not None and abs(ev["score"]) >= MIN_SCORE_ALERT
                        }
                        hits = [h for h in hits if h["symbol"] in keep]
                        ratings = {s: ev for s, ev in ratings.items() if s in keep}
                    if hits:
                        tg_send(CHAT_ID, format_hits(hits, ratings))
        except Exception as e:  # noqa: BLE001
            log.error("scanner error: %s", type(e).__name__)
        time.sleep(SCAN_INTERVAL)


_started = False


def start_background():
    global _started
    if _started or os.environ.get("DISABLE_BACKGROUND") == "1":
        return
    _started = True
    threading.Thread(target=register_webhook, daemon=True).start()
    threading.Thread(target=scanner_loop, daemon=True).start()
    log.info("Started. Watchlist=%s interval=%ss", ",".join(WATCHLIST), SCAN_INTERVAL)


start_background()

if __name__ == "__main__":
    app.run(host="0.0.0.0", port=int(os.environ.get("PORT", "10000")))
