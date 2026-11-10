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
  GEX_EXPIRIES      nearest expiries used for gamma exposure (default 4)
  GEX_MAX_DTE       max days to expiry used for gamma exposure (default 45)
"""

import hmac
import logging
import math
import os
import re
import threading
import time
from datetime import date, datetime, timedelta, timezone
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
    for s in _env(
        "WATCHLIST",
        "SPY,QQQ,IWM,DIA,AAPL,MSFT,NVDA,TSLA,AMZN,META,GOOGL,AMD,NFLX,AVGO,COST,JPM,BAC,XOM,CVX,UNH,LLY,JNJ,V,MA,HD,PG,KO,PEP,WMT,DIS,CRM,ORCL,ADBE,INTC,MU,QCOM,TXN,BA,CAT,GE"
    ).split(",")
    if s.strip()
]
SCAN_INTERVAL = int(_env("SCAN_INTERVAL_MIN", "12")) * 60
MIN_VOLUME = int(_env("MIN_VOLUME", "300"))
MIN_VOL_OI = float(_env("MIN_VOL_OI", "1.8"))
MIN_PREMIUM = float(_env("MIN_PREMIUM", "75000"))
MAX_ALERTS = int(_env("MAX_ALERTS", "12"))
EXPIRIES_TO_SCAN = int(_env("EXPIRIES_TO_SCAN", "4"))
STRIKE_RANGE = 0.18

MIN_SCORE_ALERT = int(_env("MIN_SCORE_ALERT", "4"))
STRONG_SCORE = int(_env("STRONG_SCORE", "6"))
MAX_SCORE = 8
MIN_DTE = int(_env("MIN_DTE", "3"))
MAX_DTE = int(_env("MAX_DTE", "60"))
MAX_SPREAD = float(_env("MAX_SPREAD", "0.15"))
ATR_STOP = float(_env("ATR_STOP", "1.5"))
ATR_TARGET = float(_env("ATR_TARGET", "3.0"))
GEX_EXPIRIES = int(_env("GEX_EXPIRIES", "5"))
GEX_MAX_DTE = int(_env("GEX_MAX_DTE", "60"))
RISK_FREE = 0.04
HOLD_DAYS = int(_env("HOLD_DAYS", "5"))

API = f"https://api.telegram.org/bot{TOKEN}"
NY = ZoneInfo("America/New_York")
DISCLAIMER = "\u26a0\ufe0f \u0645\u0639\u0644\u0648\u0645\u0627\u062a \u0648\u062a\u062d\u0644\u064a\u0644 \u0641\u0646\u064a \u0641\u0642\u0637 \u0648\u0644\u064a\u0633\u062a \u062a\u0648\u0635\u064a\u0629 \u0627\u0633\u062a\u062b\u0645\u0627\u0631\u064a\u0629. \u0644\u0627 \u062a\u062e\u0627\u0637\u0631 \u0628\u0623\u0643\u062b\u0631 \u0645\u0646 1-2% \u0645\u0646 \u0631\u0623\u0633 \u0645\u0627\u0644\u0643 \u0641\u064a \u0627\u0644\u0635\u0641\u0642\u0629 \u0627\u0644\u0648\u0627\u062d\u062f\u0629."

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


CACHE_TTL = int(_env("CACHE_TTL_SEC", "90"))
NEWS_HOURS = int(_env("NEWS_HOURS", "72"))
FRESH_MAX_MIN = int(_env("FRESH_MAX_MIN", "20"))


def _with_retry(fn):
    last = None
    for i in range(3):
        try:
            return fn()
        except Exception as e:  # noqa: BLE001
            last = e
            log.warning("yahoo call failed (try %d): %s", i + 1, type(e).__name__)
            time.sleep(1.5 * (i + 1))
    raise last


class CachedTicker:
    """yfinance Ticker wrapper: caches results for CACHE_TTL seconds and retries failed calls."""

    def __init__(self, sym):
        self._t = yf.Ticker(sym)
        self._store = {}

    def _get(self, key, fn):
        hit = self._store.get(key)
        if hit and time.time() - hit[0] < CACHE_TTL:
            return hit[1]
        val = _with_retry(fn)
        self._store[key] = (time.time(), val)
        return val

    @property
    def options(self):
        return self._get("options", lambda: tuple(self._t.options))

    @property
    def calendar(self):
        return self._get("calendar", lambda: self._t.calendar)

    @property
    def news(self):
        return self._get("news", lambda: self._t.news)

    def history(self, period="1d", interval="1d"):
        return self._get(("h", period, interval), lambda: self._t.history(period=period, interval=interval))

    def option_chain(self, exp):
        return self._get(("c", exp), lambda: self._t.option_chain(exp))


_tickers = {}
_tickers_lock = threading.Lock()


def get_ticker(sym):
    with _tickers_lock:
        if len(_tickers) > 60:
            _tickers.clear()
        ct = _tickers.get(sym)
        if ct is None:
            ct = _tickers[sym] = CachedTicker(sym)
        return ct


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
    t = get_ticker(sym)
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
        time.sleep(0.5)
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


def _bs_gamma(S, K, T, sigma):
    if S <= 0 or K <= 0 or T <= 0 or sigma <= 0:
        return 0.0
    d1 = (math.log(S / K) + (RISK_FREE + 0.5 * sigma * sigma) * T) / (sigma * math.sqrt(T))
    pdf = math.exp(-0.5 * d1 * d1) / math.sqrt(2 * math.pi)
    return pdf / (S * sigma * math.sqrt(T))


class GexUnavailable(Exception):
    """Raised with a user-facing (Arabic) reason when gamma exposure cannot be computed."""


def _realized_vol(t):
    try:
        d = t.history(period="1y", interval="1d")["Close"].dropna()
        v = float(d.pct_change().tail(30).std() * math.sqrt(252))
        return v if 0.05 <= v <= 5.0 else None
    except Exception:  # noqa: BLE001
        return None


def compute_gex(t, price, fallback_iv=None):
    """Estimate dealer gamma exposure from open interest (dealers assumed long calls / short puts).

    When Yahoo returns a missing/zero IV (typical after the close) the expiry's median IV is used,
    and if there is none, the stock's realized volatility. The share of estimated contracts is returned.
    """
    today = datetime.now(NY).date()
    opts, used, est, total, fails = [], 0, 0, 0, 0
    exps = list(t.options)
    if not exps:
        raise GexUnavailable("Yahoo \u0645\u0627 \u0631\u062c\u0651\u0639 \u0623\u064a \u062a\u0648\u0627\u0631\u064a\u062e \u0623\u0648\u0628\u0634\u0646\u0632 \u0644\u0647\u0630\u0627 \u0627\u0644\u0633\u0647\u0645 (\u063a\u0627\u0644\u0628\u0627\u064b \u062d\u062f \u0627\u0644\u0637\u0644\u0628\u0627\u062a \u0623\u0648 \u0645\u0634\u0643\u0644\u0629 \u0627\u062a\u0635\u0627\u0644)")
    for exp in exps:
        dte = (date.fromisoformat(exp) - today).days
        if dte < 0:
            continue
        if dte > GEX_MAX_DTE or used >= GEX_EXPIRIES:
            break
        try:
            chain = t.option_chain(exp)
        except Exception:  # noqa: BLE001
            fails += 1
            if fails >= 2:
                break
            continue
        used += 1
        tau = max(dte, 0.5) / 365.0
        rows = []
        for sign, df in ((1, chain.calls), (-1, chain.puts)):
            for row in df.itertuples():
                k, oi, iv = _num(row.strike), _num(row.openInterest), _num(row.impliedVolatility)
                if oi <= 0 or not (price * 0.7 <= k <= price * 1.3):
                    continue
                rows.append((sign, k, oi, iv))
        valid = sorted(iv for _, _, _, iv in rows if 0.05 <= iv <= 5.0)
        med = valid[len(valid) // 2] if valid else None
        for sign, k, oi, iv in rows:
            estimated = False
            if not (0.05 <= iv <= 5.0):
                iv = med or fallback_iv
                estimated = True
            if not iv or not (0.05 <= iv <= 5.0):
                continue
            total += 1
            est += 1 if estimated else 0
            opts.append((sign, k, oi, iv, tau))
    if not opts:
        if used == 0 and fails:
            raise GexUnavailable("\u0641\u0634\u0644 \u062c\u0644\u0628 \u0633\u0644\u0627\u0633\u0644 \u0627\u0644\u0623\u0648\u0628\u0634\u0646\u0632 \u0645\u0646 Yahoo (\u063a\u0627\u0644\u0628\u0627\u064b \u062d\u062f \u0627\u0644\u0637\u0644\u0628\u0627\u062a)\u060c \u062c\u0631\u0628 \u0628\u0639\u062f \u062f\u0642\u064a\u0642\u0629")
        if used == 0:
            raise GexUnavailable(f"\u0645\u0627 \u0641\u064a\u0647 \u062a\u0648\u0627\u0631\u064a\u062e \u0627\u0646\u062a\u0647\u0627\u0621 \u0636\u0645\u0646 {GEX_MAX_DTE} \u064a\u0648\u0645")
        raise GexUnavailable("\u0645\u0627 \u0641\u064a\u0647 Open Interest \u0623\u0648 IV \u0643\u0627\u0641\u064a \u0641\u064a \u0627\u0644\u0633\u0644\u0633\u0644\u0629 (\u063a\u0627\u0644\u0628\u0627\u064b \u0627\u0644\u0633\u0648\u0642 \u0645\u0633\u0643\u0631 \u0648\u0628\u064a\u0627\u0646\u0627\u062a Yahoo \u0646\u0627\u0642\u0635\u0629)")

    def net_at(spot):
        return sum(s * _bs_gamma(spot, k, tau, iv) * oi * 100 * spot * spot * 0.01 for s, k, oi, iv, tau in opts)

    calls, puts = {}, {}
    for s, k, oi, iv, tau in opts:
        g = _bs_gamma(price, k, tau, iv) * oi * 100 * price * price * 0.01
        book = calls if s > 0 else puts
        book[k] = book.get(k, 0.0) + g
    above = {k: v for k, v in calls.items() if k >= price}
    below = {k: v for k, v in puts.items() if k <= price}
    call_wall = max(above, key=above.get) if above else None
    put_wall = max(below, key=below.get) if below else None

    crossings, prev_s, prev_v = [], None, None
    for i in range(61):
        spot = price * (0.85 + 0.005 * i)
        v = net_at(spot)
        if prev_v is not None and prev_v * v < 0:
            crossings.append(prev_s + (spot - prev_s) * (-prev_v) / (v - prev_v))
        prev_s, prev_v = spot, v
    flip = min(crossings, key=lambda c: abs(c - price)) if crossings else None

    return {
        "net": net_at(price),
        "flip": flip,
        "call_wall": call_wall,
        "put_wall": put_wall,
        "est_frac": (est / total) if total else 0.0,
    }


def _norm_cdf(x):
    return 0.5 * (1 + math.erf(x / math.sqrt(2)))
  def _bs_d1(S, K, T, sigma):
    return (math.log(S / K) + (RISK_FREE + 0.5 * sigma * sigma) * T) / (sigma * math.sqrt(T))


def _bs_price(S, K, T, sigma, is_call):
    if T <= 0 or sigma <= 0:
        return max(S - K, 0.0) if is_call else max(K - S, 0.0)
    d1 = _bs_d1(S, K, T, sigma)
    d2 = d1 - sigma * math.sqrt(T)
    disc = K * math.exp(-RISK_FREE * T)
    if is_call:
        return S * _norm_cdf(d1) - disc * _norm_cdf(d2)
    return disc * _norm_cdf(-d2) - S * _norm_cdf(-d1)


def _bs_delta(S, K, T, sigma, is_call):
    d1 = _bs_d1(S, K, T, sigma)
    return _norm_cdf(d1) if is_call else _norm_cdf(d1) - 1


def _implied_vol(target, S, K, T, is_call):
    """Back out IV from an option mid price (bisection). Used when Yahoo returns a missing/zero IV."""
    intrinsic = max(S - K, 0.0) if is_call else max(K - S, 0.0)
    if T <= 0 or target <= intrinsic:
        return None
    lo, hi = 0.05, 5.0
    if _bs_price(S, K, T, hi, is_call) < target or _bs_price(S, K, T, lo, is_call) > target:
        return None
    for _ in range(50):
        mid = (lo + hi) / 2
        if _bs_price(S, K, T, mid, is_call) > target:
            hi = mid
        else:
            lo = mid
    return (lo + hi) / 2


def next_earnings(t):
    try:
        cal = t.calendar
        dates = cal.get("Earnings Date") or []
        today = datetime.now(NY).date()
        clean = [d.date() if isinstance(d, datetime) else d for d in dates]
        future = sorted(d for d in clean if d >= today)
        return future[0] if future else None
    except Exception:  # noqa: BLE001
        return None


def cap_targets(plan, side, gex, price):
    """Pull the final target back to the gamma wall when the wall sits between entry and target."""
    if not gex:
        return
    risk = abs(price - plan["stop"])
    if side == "CALL":
        wall = gex["call_wall"]
        if wall and price < wall < plan["t2"]:
            plan["t2"] = wall
            plan["t1"] = min(plan["t1"], price + (wall - price) / 2)
            plan["note"] = f"هدف 2 مخفّض إلى ${wall:g} لأن جدار CALL (مقاومة جاما) عنده."
    else:
        wall = gex["put_wall"]
        if wall and plan["t2"] < wall < price:
            plan["t2"] = wall
            plan["t1"] = max(plan["t1"], price - (price - wall) / 2)
            plan["note"] = f"هدف 2 مخفّض إلى ${wall:g} لأن جدار PUT (دعم جاما) عنده."
    if risk > 0:
        plan["rr"] = abs(plan["t2"] - price) / risk


def _why_text(why):
    if not why:
        return None
    key = max(why, key=why.get)
    return {
        "noquote": "الـ Bid/Ask صفر (السوق مسكر أو العقود ما تتداول الحين)",
        "lowoi": "عقود الـ OI فيها أقل من 100",
        "spread": f"السبريد أعلى من {MAX_SPREAD * 100:.0f}%",
        "iv": "ما قدرت أحسب IV للعقد",
        "delta": "ما فيه دلتا مناسبة قريبة من السعر",
        "gain": "العقد ما تحقق ربح عند الهدف",
        "chainfail": "فشل جلب سلاسل الأوبشنز من Yahoo (غالباً حد الطلبات)",
        "noexp": f"ما فيه تواريخ انتهاء بين {max(MIN_DTE, HOLD_DAYS + 3)} و{MAX_DTE} يوم",
    }[key]


def pick_contracts(t, side, price, plan, fallback_iv, earn, gex):
    """Price every liquid contract at target 1 / target 2 / stop and rank by gain-to-loss on the premium."""
    today = datetime.now(NY).date()
    is_call = side == "CALL"
    positive_gamma = bool(gex and gex["net"] > 0)
    lo_d, hi_d = (0.45, 0.80) if positive_gamma else (0.35, 0.70)
    min_dte = max(MIN_DTE, HOLD_DAYS + 3)
    cands, blocked, used, fails, why = [], False, 0, 0, {}

    def bump(key):
        why[key] = why.get(key, 0) + 1

    for exp in t.options:
        exp_d = date.fromisoformat(exp)
        dte = (exp_d - today).days
        if dte < min_dte:
            continue
        if dte > MAX_DTE:
            break
        if earn and exp_d >= earn:
            blocked = True
            continue
        if used >= 5:
            break
        try:
            chain = t.option_chain(exp)
        except Exception:  # noqa: BLE001
            bump("chainfail")
            fails += 1
            if fails >= 2:
                break
            continue
        used += 1
        df = chain.calls if is_call else chain.puts
        for row in df.itertuples():
            k, bid, ask, oi = _num(row.strike), _num(row.bid), _num(row.ask), _num(row.openInterest)
            if abs(k - price) / price > 0.06:
                continue
            if bid <= 0 or ask <= 0:
                bump("noquote")
                continue
            if oi < 100:
                bump("lowoi")
                continue
            mid = (bid + ask) / 2
            # فلتر سعر العقد بين 10 و 90 دولار
            if mid < 10.0 or mid > 90.0:
                continue
            spread = (ask - bid) / mid
            if spread > MAX_SPREAD:
                bump("spread")
                continue
            tau = dte / 365.0
            iv = _num(row.impliedVolatility)
            est_iv = False
            if not (0.05 <= iv <= 5.0):
                iv, est_iv = (_implied_vol(mid, price, k, tau, is_call) or fallback_iv), True
            if not iv or not (0.05 <= iv <= 5.0):
                bump("iv")
                continue
            hold = min(HOLD_DAYS, dte - 1) / 365.0
            delta = _bs_delta(price, k, tau, iv, is_call)
            if not (lo_d <= abs(delta) <= hi_d):
                bump("delta")
                continue

            # تقدير الربح والخسارة عند الأهداف
            p1 = _bs_price(plan["t1"], k, hold, iv, is_call)
            p2 = _bs_price(plan["t2"], k, hold, iv, is_call)
            pstop = _bs_price(plan["stop"], k, hold, iv, is_call)
            gain1 = (p1 - mid) / mid if mid > 0 else 0
            gain2 = (p2 - mid) / mid if mid > 0 else 0
            loss = (mid - pstop) / mid if mid > 0 else 0

            if gain1 <= 0 and gain2 <= 0:
                bump("gain")
                continue

            cands.append({
                "strike": k,
                "expiry": exp,
                "dte": dte,
                "bid": bid,
                "ask": ask,
                "mid": mid,
                "spread": spread,
                "iv": iv,
                "est_iv": est_iv,
                "delta": delta,
                "oi": int(oi),
                "volume": int(_num(row.volume)),
                "gain1": gain1,
                "gain2": gain2,
                "loss": loss,
                "side": side,
                "contractSymbol": getattr(row, "contractSymbol", ""),
                "reasons": [],
            })

    if not cands:
        return None, _why_text(why)

    # رتب حسب نسبة الربح إلى الخسارة
    cands.sort(key=lambda x: (x["gain2"] / max(x["loss"], 0.01)), reverse=True)
    best = cands[0]
    best["reasons"] = [
        f"دلتا {abs(best['delta']):.2f}",
        f"سبريد {best['spread']*100:.1f}%",
        f"OI {best['oi']:,}",
    ]
    return best, cands[:3]
def rate_symbol(sym, flow=None):
    t = get_ticker(sym)
    tech = technicals(t)
    if tech is None:
        return None

    score, trend_pts, reasons = 0, 0, []
    p = tech["price"]

    # 1) trend
    if p > tech["ema20"] > tech["ema50"]:
        trend_pts = 2
        reasons.append("ترند صاعد: السعر فوق EMA20 فوق EMA50 (+2)")
        score += 2
    elif p < tech["ema20"] < tech["ema50"]:
        trend_pts = -2
        reasons.append("ترند هابط: السعر تحت EMA20 تحت EMA50 (-2)")
        score -= 2
    else:
        reasons.append("ترند مختلط (0)")

    # 2) SMA200
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
    if tech["vol_ratio"] is not None:
        if tech["vol_ratio"] >= 1.5:
            if tech["day_up"]:
                score += 1
                reasons.append(f"حجم عالي مع صعود ({tech['vol_ratio']:.1f}x) (+1)")
            else:
                score -= 1
                reasons.append(f"حجم عالي مع هبوط ({tech['vol_ratio']:.1f}x) (-1)")

    # 6) options flow
    if flow:
        call_prem = sum(h["premium"] for h in flow if h["side"] == "CALL")
        put_prem = sum(h["premium"] for h in flow if h["side"] == "PUT")
        if call_prem + put_prem > 0:
            if call_prem > put_prem * 1.5:
                score += 2
                reasons.append(f"تدفق خيارات قوي على الـ CALL (+2)")
            elif put_prem > call_prem * 1.5:
                score -= 2
                reasons.append(f"تدفق خيارات قوي على الـ PUT (-2)")

    side = "CALL" if score > 0 else "PUT" if score < 0 else None
    strong = abs(score) >= STRONG_SCORE and (
        (score > 0 and trend_pts > 0) or (score < 0 and trend_pts < 0)
    )

    # بناء الخطة
    atr = tech["atr"]
    entry = p
    if side == "CALL":
        stop = entry - ATR_STOP * atr
        t1 = entry + ATR_TARGET * atr * 0.6
        t2 = entry + ATR_TARGET * atr
    elif side == "PUT":
        stop = entry + ATR_STOP * atr
        t1 = entry - ATR_TARGET * atr * 0.6
        t2 = entry - ATR_TARGET * atr
    else:
        stop = t1 = t2 = entry

    plan = {
        "entry": entry,
        "stop": stop,
        "t1": t1,
        "t2": t2,
        "rr": abs(t2 - entry) / max(abs(entry - stop), 0.01),
    }

    # Gamma
    gex = None
    gex_reason = None
    try:
        gex = compute_gex(t, p, _realized_vol(t))
        if gex:
            cap_targets(plan, side, gex, p)
    except GexUnavailable as e:
        gex_reason = str(e)

    # اختيار العقد
    contract = None
    alternatives = []
    earn = next_earnings(t)
    if side:
        contract, alternatives = pick_contracts(t, side, p, plan, _realized_vol(t), earn, gex)

    return {
        "symbol": sym,
        "score": score,
        "side": side,
        "strong": strong,
        "reasons": reasons,
        "price": p,
        "plan": plan,
        "contract": contract,
        "alternatives": alternatives,
        "gex": gex,
        "gex_reason": gex_reason,
        "next_earnings": earn,
        "tech": tech,
    }


def evaluate(sym, flow=None, full=False):
    ev = rate_symbol(sym, flow)
    if ev is None:
        return None
    # باقي التأكيدات والـ quote يمكن إضافتها هنا حسب الحاجة
    return ev


def format_gex(g, price):
    lines = ["🧮 مستويات الجاما:"]
    if g.get("flip"):
        lines.append(f"• Gamma Flip: ${g['flip']:.2f}")
    if g.get("call_wall"):
        lines.append(f"• Call Wall: ${g['call_wall']:g}")
    if g.get("put_wall"):
        lines.append(f"• Put Wall: ${g['put_wall']:g}")
    net = g.get("net", 0)
    regime = "إيجابي (Positive)" if net > 0 else "سلبي (Negative)"
    lines.append(f"• Net GEX: {net/1e6:.1f}M → نظام {regime}")
    return "\n".join(lines)


def format_news(news):
    if not news:
        return "📰 لا توجد أخبار حديثة مؤثرة."
    return "📰 أخبار حديثة:\n" + "\n".join(f"• {n}" for n in news[:3])


def format_hit(h):
    return (
        f"{h['symbol']} {h['side']} {h['strike']:g} | انتهاء {h['expiry']}\n"
        f"حجم: {h['volume']:,} | OI: {h['oi']:,} | بريميوم: {money(h['premium'])}"
    )


def format_hits(hits, ratings):
    lines = ["🔥 توصيات عالية الثقة:\n"]
    for h in hits:
        ev = ratings.get(h["symbol"])
        score = ev["score"] if ev else 0
        lines.append(
            f"{h['symbol']} {h['side']} {h['strike']:g} | Score: {score:+d}\n"
            f"انتهاء: {h['expiry']} | بريميوم: {money(h['premium'])}\n"
        )
    lines.append(DISCLAIMER)
    return "\n".join(lines)


# ----------------------------------------------------------------------------
# Telegram commands
# ----------------------------------------------------------------------------
HELP = (
    "أوامر البوت:\n"
    "• أرسل رمز سهم (مثل TSLA) ويجيك تقييم + خطة + عقد مقترح\n"
    "• /scan فحص فوري لقائمة المراقبة مع التقييم\n"
    "• /news TSLA الأخبار وتصنيفها\n"
    "• /gamma TSLA مستويات الجاما والجدران\n"
    "• /watch عرض قائمة المراقبة\n"
    "• /help هذه الرسالة\n\n"
    f"الفحص التلقائي كل {SCAN_INTERVAL // 60} دقيقة أثناء جلسة السوق الأمريكي."
)


def handle_update(update):
    chat_id = ""
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
            return

        cmd = text.split()[0].split("@")[0].lower()
        if cmd in ("/start", "/help"):
            tg_send(chat_id, HELP)
        elif cmd == "/news":
            parts = text.split()
            tg_send(chat_id, "⏳ أجلب الأخبار...")
            tg_send(chat_id, news_report(parts[1] if len(parts) > 1 else ""))
        elif cmd == "/gamma":
            parts = text.split()
            tg_send(chat_id, "⏳ أحسب الجاما...")
            tg_send(chat_id, gamma_report(parts[1] if len(parts) > 1 else ""))
        elif cmd == "/watch":
            tg_send(chat_id, "قائمة المراقبة:\n" + ", ".join(WATCHLIST))
        elif cmd == "/scan":
            tg_send(chat_id, "⏳ أفحص السوق...")
            hits = scan_all(dedupe=False)
            if hits:
                ratings = {h["symbol"]: rate_symbol(h["symbol"]) for h in hits}
                tg_send(chat_id, format_hits(hits, ratings))
            else:
                tg_send(chat_id, "ما فيه نشاط غير عادي حالياً حسب الفلاتر.")
        elif text.startswith("/"):
            tg_send(chat_id, HELP)
        else:
            tg_send(chat_id, "⏳ أحلل...")
            tg_send(chat_id, analyze(text))
    except Exception as e:
        log.error("handle_update error: %s", type(e).__name__)
        if CHAT_ID and chat_id == CHAT_ID:
            tg_send(chat_id, "⚠️ تعذر جلب البيانات الآن، غالباً Yahoo يحد الطلبات. جرب بعد دقيقة.")


def analyze(raw):
    sym = raw.strip().upper()
    if not sym or len(sym) > 6:
        return "اكتب رمز سهم صحيح مثل: AAPL أو TSLA"
    try:
        flow = scan_ticker(sym)
    except Exception:
        flow = []
    ev = evaluate(sym, flow, full=True)
    if ev is None:
        return f"ما لقيت بيانات كافية للرمز {sym}"
    return format_full(ev, flow) if "format_full" in globals() else str(ev)


def gamma_report(raw):
    sym = raw.strip().upper()
    if not sym:
        return "اكتب الأمر مع رمز السهم، مثل: /gamma TSLA"
    t = get_ticker(sym)
    h = t.history(period="1d")
    if h is None or h.empty:
        return f"ما لقيت بيانات للرمز {sym}"
    price = float(h["Close"].iloc[-1])
    try:
        g = compute_gex(t, price, _realized_vol(t))
    except GexUnavailable as e:
        return f"الجاما غير متوفرة على {sym}: {e}"
    return f"📊 {sym}  ${price:.2f}\n\n{format_gex(g, price)}\n\n{DISCLAIMER}"


def news_report(raw):
    sym = raw.strip().upper()
    if not sym:
        return "اكتب الأمر مع رمز السهم، مثل: /news TSLA"
    return f"📊 {sym}\n\n{format_news([])}\n\n{DISCLAIMER}"


# ----------------------------------------------------------------------------
# Flask routes + background scanner
# ----------------------------------------------------------------------------
@app.route(f"/webhook/{SECRET}", methods=["POST"])
def webhook():
    if request.headers.get("X-Telegram-Bot-Api-Secret-Token") != SECRET:
        abort(403)
    handle_update(request.get_json(force=True, silent=True) or {})
    return "ok"


@app.route("/")
def health():
    return "Options Bot is running"


def scanner_loop():
    while True:
        try:
            if is_market_open() and CHAT_ID:
                hits = scan_all(dedupe=True)
                if hits:
                    ratings = {}
                    for h in hits:
                        ratings[h["symbol"]] = rate_symbol(h["symbol"], [h])
                    if MIN_SCORE_ALERT > 0:
                        keep = {
                            s for s, ev in ratings.items()
                            if ev is not None and abs(ev["score"]) >= MIN_SCORE_ALERT
                        }
                        hits = [h for h in hits if h["symbol"] in keep]
                    if hits:
                        tg_send(CHAT_ID, format_hits(hits, ratings))
        except Exception as e:
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
