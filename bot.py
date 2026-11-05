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
GEX_EXPIRIES = int(_env("GEX_EXPIRIES", "4"))
GEX_MAX_DTE = int(_env("GEX_MAX_DTE", "45"))
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
            plan["note"] = f"\u0647\u062f\u0641 2 \u0645\u062e\u0641\u0651\u0636 \u0625\u0644\u0649 ${wall:g} \u0644\u0623\u0646 \u062c\u062f\u0627\u0631 CALL (\u0645\u0642\u0627\u0648\u0645\u0629 \u062c\u0627\u0645\u0627) \u0639\u0646\u062f\u0647."
    else:
        wall = gex["put_wall"]
        if wall and plan["t2"] < wall < price:
            plan["t2"] = wall
            plan["t1"] = max(plan["t1"], price - (price - wall) / 2)
            plan["note"] = f"\u0647\u062f\u0641 2 \u0645\u062e\u0641\u0651\u0636 \u0625\u0644\u0649 ${wall:g} \u0644\u0623\u0646 \u062c\u062f\u0627\u0631 PUT (\u062f\u0639\u0645 \u062c\u0627\u0645\u0627) \u0639\u0646\u062f\u0647."
    if risk > 0:
        plan["rr"] = abs(plan["t2"] - price) / risk


def _why_text(why):
    if not why:
        return None
    key = max(why, key=why.get)
    return {
        "noquote": "\u0627\u0644\u0640 Bid/Ask \u0635\u0641\u0631 (\u0627\u0644\u0633\u0648\u0642 \u0645\u0633\u0643\u0631 \u0623\u0648 \u0627\u0644\u0639\u0642\u0648\u062f \u0645\u0627 \u062a\u062a\u062f\u0627\u0648\u0644 \u0627\u0644\u062d\u064a\u0646)",
        "lowoi": "\u0639\u0642\u0648\u062f \u0627\u0644\u0640 OI \u0641\u064a\u0647\u0627 \u0623\u0642\u0644 \u0645\u0646 100",
        "spread": f"\u0627\u0644\u0633\u0628\u0631\u064a\u062f \u0623\u0639\u0644\u0649 \u0645\u0646 {MAX_SPREAD * 100:.0f}%",
        "iv": "\u0645\u0627 \u0642\u062f\u0631\u062a \u0623\u062d\u0633\u0628 IV \u0644\u0644\u0639\u0642\u0648\u062f",
        "delta": "\u0645\u0627 \u0641\u064a\u0647 \u062f\u0644\u062a\u0627 \u0645\u0646\u0627\u0633\u0628\u0629 \u0642\u0631\u064a\u0628\u0629 \u0645\u0646 \u0627\u0644\u0633\u0639\u0631",
        "gain": "\u0627\u0644\u0639\u0642\u0648\u062f \u0645\u0627 \u062a\u062d\u0642\u0642 \u0631\u0628\u062d \u0639\u0646\u062f \u0627\u0644\u0647\u062f\u0641",
        "chainfail": "\u0641\u0634\u0644 \u062c\u0644\u0628 \u0633\u0644\u0627\u0633\u0644 \u0627\u0644\u0623\u0648\u0628\u0634\u0646\u0632 \u0645\u0646 Yahoo (\u063a\u0627\u0644\u0628\u0627\u064b \u062d\u062f \u0627\u0644\u0637\u0644\u0628\u0627\u062a)",
        "noexp": f"\u0645\u0627 \u0641\u064a\u0647 \u062a\u0648\u0627\u0631\u064a\u062e \u0627\u0646\u062a\u0647\u0627\u0621 \u0628\u064a\u0646 {max(MIN_DTE, HOLD_DAYS + 3)} \u0648{MAX_DTE} \u064a\u0648\u0645",
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
            v1 = _bs_price(plan["t1"], k, tau - hold, iv, is_call)
            v2 = _bs_price(plan["t2"], k, tau - hold, iv, is_call)
            vs = _bs_price(plan["stop"], k, tau - hold, iv, is_call)
            gain1, gain2 = (v1 - ask) / ask, (v2 - ask) / ask
            loss = max((ask - vs) / ask, 0.05)
            if gain1 <= 0:
                bump("gain")
                continue
            cands.append(
                {
                    "side": side, "expiry": exp, "dte": dte, "strike": k, "bid": bid, "ask": ask,
                    "spread": spread, "iv": iv, "est_iv": est_iv, "delta": delta, "oi": int(oi),
                    "volume": int(_num(getattr(row, "volume", 0))), "gain1": gain1, "gain2": gain2,
                    "loss": loss, "score": gain1 / loss,
                }
            )
    if used == 0 and not blocked and not why.get("chainfail"):
        bump("noexp")
    cands.sort(key=lambda c: c["score"], reverse=True)
    top = cands[:3]
    if top:
        b0 = top[0]
        reasons = [
            f"{b0['dte']} \u064a\u0648\u0645: \u064a\u0643\u0641\u064a \u0644\u0648\u0635\u0648\u0644 \u0627\u0644\u0647\u062f\u0641 (~{HOLD_DAYS} \u0623\u064a\u0627\u0645) \u0645\u0639 \u0647\u0627\u0645\u0634 \u0642\u0628\u0644 \u0645\u0627 \u064a\u062a\u0622\u0643\u0644 \u0627\u0644\u0648\u0642\u062a",
            f"\u0623\u0641\u0636\u0644 \u0646\u0633\u0628\u0629 \u0639\u0627\u0626\u062f/\u062e\u0633\u0627\u0631\u0629 \u062a\u0642\u062f\u064a\u0631\u064a\u0629 \u0639\u0644\u0649 \u0627\u0644\u0642\u0633\u0637 \u0628\u064a\u0646 {len(cands)} \u0639\u0642\u062f \u0633\u0627\u0626\u0644 (\u0639\u0646\u062f \u0647\u062f\u0641 2: 1:{b0['gain2'] / b0['loss']:.1f})",
        ]
        if earn:
            reasons.append(f"\u064a\u0646\u062a\u0647\u064a \u0642\u0628\u0644 \u0625\u0639\u0644\u0627\u0646 \u0627\u0644\u0623\u0631\u0628\u0627\u062d ({earn})")
        if gex:
            reasons.append(
                "\u0627\u0644\u062c\u0627\u0645\u0627 \u0645\u0648\u062c\u0628\u0629 (\u062d\u0631\u0643\u0629 \u0623\u0647\u062f\u0623)\u060c \u0641\u0627\u062e\u062a\u0631\u062a \u062f\u0644\u062a\u0627 \u0623\u0639\u0644\u0649 \u0648\u0623\u0642\u0631\u0628 \u0644\u0644\u0633\u0639\u0631"
                if positive_gamma
                else "\u0627\u0644\u062c\u0627\u0645\u0627 \u0633\u0627\u0644\u0628\u0629 (\u062d\u0631\u0643\u0629 \u0623\u0643\u0628\u0631 \u0645\u062d\u062a\u0645\u0644\u0629)\u060c \u0641\u0633\u0645\u062d\u062a \u0628\u062f\u0644\u062a\u0627 \u0623\u0642\u0644"
            )
        if plan.get("note"):
            reasons.append("\u0627\u0644\u0623\u0647\u062f\u0627\u0641 \u0645\u062d\u0633\u0648\u0628\u0629 \u0628\u0639\u062f \u0636\u0628\u0637\u0647\u0627 \u0639\u0644\u0649 \u062c\u062f\u0627\u0631 \u0627\u0644\u062c\u0627\u0645\u0627")
        b0["reasons"] = reasons
    return {
        "best": top[0] if top else None,
        "candidates": top,
        "blocked_by_earnings": blocked,
        "why": None if top else _why_text(why),
    }


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


def evaluate(sym, flow_hits=None, full=False):
    """Score a ticker and build a trade plan. Returns None when data is missing."""
    t = get_ticker(sym)
    tech = technicals(t)
    if tech is None:
        return None

    score, trend_pts, reasons = 0, 0, []
    p = tech["price"]

    # 1) trend
    if p > tech["ema20"] > tech["ema50"]:
        trend_pts = 2
        reasons.append("\u062a\u0631\u0646\u062f \u0635\u0627\u0639\u062f: \u0627\u0644\u0633\u0639\u0631 \u0641\u0648\u0642 EMA20 \u0648EMA20 \u0641\u0648\u0642 EMA50 (+2)")
    elif p < tech["ema20"] < tech["ema50"]:
        trend_pts = -2
        reasons.append("\u062a\u0631\u0646\u062f \u0647\u0627\u0628\u0637: \u0627\u0644\u0633\u0639\u0631 \u062a\u062d\u062a EMA20 \u0648EMA20 \u062a\u062d\u062a EMA50 (-2)")
    else:
        reasons.append("\u0627\u0644\u062a\u0631\u0646\u062f \u063a\u064a\u0631 \u0648\u0627\u0636\u062d (EMA \u0645\u062a\u062f\u0627\u062e\u0644\u0629) (0)")
    score += trend_pts

    # 2) long-term position
    if tech["sma200"] is not None:
        if p > tech["sma200"]:
            score += 1
            reasons.append("\u0627\u0644\u0633\u0639\u0631 \u0641\u0648\u0642 SMA-200 (+1)")
        else:
            score -= 1
            reasons.append("\u0627\u0644\u0633\u0639\u0631 \u062a\u062d\u062a SMA-200 (-1)")

    # 3) MACD
    h, hp = tech["macd_hist"], tech["macd_hist_prev"]
    if h > 0 and h > hp:
        score += 1
        reasons.append("\u0632\u062e\u0645 MACD \u0625\u064a\u062c\u0627\u0628\u064a \u0648\u064a\u062a\u0632\u0627\u064a\u062f (+1)")
    elif h < 0 and h < hp:
        score -= 1
        reasons.append("\u0632\u062e\u0645 MACD \u0633\u0644\u0628\u064a \u0648\u064a\u062a\u0632\u0627\u064a\u062f (-1)")

    # 4) RSI zone
    r = tech["rsi"]
    if r is not None:
        if r >= 75:
            score -= 1
            reasons.append(f"RSI {r:.0f} \u062a\u0634\u0628\u0639 \u0634\u0631\u0627\u0621\u060c \u062e\u0637\u0631 \u062a\u0635\u062d\u064a\u062d (-1)")
        elif r <= 25:
            score += 1
            reasons.append(f"RSI {r:.0f} \u062a\u0634\u0628\u0639 \u0628\u064a\u0639\u060c \u0627\u062d\u062a\u0645\u0627\u0644 \u0627\u0631\u062a\u062f\u0627\u062f (+1)")
        elif r >= 50:
            score += 1
            reasons.append(f"RSI {r:.0f} \u0641\u064a \u0645\u0646\u0637\u0642\u0629 \u0642\u0648\u0629 (+1)")
        else:
            score -= 1
            reasons.append(f"RSI {r:.0f} \u0641\u064a \u0645\u0646\u0637\u0642\u0629 \u0636\u0639\u0641 (-1)")

    # 5) volume spike with direction
    vr = tech["vol_ratio"]
    if vr is not None and vr >= 1.5:
        if tech["day_up"]:
            score += 1
            reasons.append(f"\u062d\u062c\u0645 \u0623\u0639\u0644\u0649 \u0645\u0646 \u0627\u0644\u0645\u0639\u062a\u0627\u062f {vr:.1f}x \u0645\u0639 \u0625\u063a\u0644\u0627\u0642 \u0635\u0627\u0639\u062f (+1)")
        else:
            score -= 1
            reasons.append(f"\u062d\u062c\u0645 \u0623\u0639\u0644\u0649 \u0645\u0646 \u0627\u0644\u0645\u0639\u062a\u0627\u062f {vr:.1f}x \u0645\u0639 \u0625\u063a\u0644\u0627\u0642 \u0647\u0627\u0628\u0637 (-1)")

    # 6) unusual options flow
    flow_pts = 0
    if flow_hits:
        calls = sum(x["premium"] for x in flow_hits if x["side"] == "CALL")
        puts = sum(x["premium"] for x in flow_hits if x["side"] == "PUT")
        if calls > 0 and calls >= 1.5 * puts:
            flow_pts = 2
            reasons.append(f"\u062a\u062f\u0641\u0642 \u0623\u0648\u0628\u0634\u0646\u0632 \u063a\u064a\u0631 \u0639\u0627\u062f\u064a \u0644\u0635\u0627\u0644\u062d CALL ({money(calls)} \u0645\u0642\u0627\u0628\u0644 {money(puts)}) (+2)")
        elif puts > 0 and puts >= 1.5 * calls:
            flow_pts = -2
            reasons.append(f"\u062a\u062f\u0641\u0642 \u0623\u0648\u0628\u0634\u0646\u0632 \u063a\u064a\u0631 \u0639\u0627\u062f\u064a \u0644\u0635\u0627\u0644\u062d PUT ({money(puts)} \u0645\u0642\u0627\u0628\u0644 {money(calls)}) (-2)")
        else:
            reasons.append("\u062a\u062f\u0641\u0642 \u0627\u0644\u0623\u0648\u0628\u0634\u0646\u0632 \u0645\u062a\u0639\u0627\u062f\u0644 \u0628\u064a\u0646 CALL \u0648PUT (0)")
    else:
        reasons.append("\u0645\u0627 \u0641\u064a\u0647 \u062a\u062f\u0641\u0642 \u0623\u0648\u0628\u0634\u0646\u0632 \u063a\u064a\u0631 \u0639\u0627\u062f\u064a \u062d\u0627\u0644\u064a\u0627\u064b (0)")
    score += flow_pts

    side = "CALL" if score > 0 else "PUT"
    strong = abs(score) >= STRONG_SCORE and abs(trend_pts) == 2 and trend_pts * score > 0

    gex, gex_reason = None, None
    try:
        gex = compute_gex(t, p, _realized_vol(t))
    except GexUnavailable as e:
        gex_reason = str(e)
        log.warning("gex unavailable %s: %s", sym, gex_reason)
    except Exception as e:  # noqa: BLE001
        gex_reason = "\u062e\u0637\u0623 \u063a\u064a\u0631 \u0645\u062a\u0648\u0642\u0639 \u0623\u062b\u0646\u0627\u0621 \u062d\u0633\u0627\u0628 \u0627\u0644\u062c\u0627\u0645\u0627"
        log.warning("gex failed %s: %s", sym, type(e).__name__)
    gex_warn = None
    if gex and abs(score) >= 3:
        atr_ = tech["atr"]
        cw, pw = gex["call_wall"], gex["put_wall"]
        if score > 0 and cw and 0 < cw - p <= atr_:
            gex_warn = f"\u062c\u062f\u0627\u0631 CALL \u0639\u0646\u062f ${cw:g} \u0642\u0631\u064a\u0628 \u0641\u0648\u0642 \u0627\u0644\u0633\u0639\u0631 \u0648\u063a\u0627\u0644\u0628\u0627\u064b \u064a\u0639\u0645\u0644 \u0643\u0645\u0642\u0627\u0648\u0645\u0629\u060c \u0641\u0627\u0644\u0635\u0639\u0648\u062f \u0645\u0645\u0643\u0646 \u064a\u062a\u0639\u062b\u0631 \u0639\u0646\u062f\u0647."
            strong = False
        elif score < 0 and pw and 0 < p - pw <= atr_:
            gex_warn = f"\u062c\u062f\u0627\u0631 PUT \u0639\u0646\u062f ${pw:g} \u0642\u0631\u064a\u0628 \u062a\u062d\u062a \u0627\u0644\u0633\u0639\u0631 \u0648\u063a\u0627\u0644\u0628\u0627\u064b \u064a\u0639\u0645\u0644 \u0643\u062f\u0639\u0645\u060c \u0641\u0627\u0644\u0647\u0628\u0648\u0637 \u0645\u0645\u0643\u0646 \u064a\u062a\u0639\u062b\u0631 \u0639\u0646\u062f\u0647."
            strong = False
    if strong:
        label = "\u0642\u0648\u064a\u0629"
    elif abs(score) >= 3:
        label = "\u0645\u062a\u0648\u0633\u0637\u0629"
    else:
        label = "\u0645\u062d\u0627\u064a\u062f\u0629"

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
        "gex": gex,
        "gex_warn": gex_warn,
        "gex_reason": gex_reason,
        "contract_why": None,
    }

    if label != "\u0645\u062d\u0627\u064a\u062f\u0629":
        sign = 1 if side == "CALL" else -1
        atr = tech["atr"]
        out["plan"] = {
            "entry": p,
            "stop": p - sign * ATR_STOP * atr,
            "t1": p + sign * (ATR_TARGET / 2) * atr,
            "t2": p + sign * ATR_TARGET * atr,
            "rr": ATR_TARGET / ATR_STOP,
        }
        cap_targets(out["plan"], side, gex, p)
        try:
            earn = next_earnings(t)
            out["next_earnings"] = earn
            picks = pick_contracts(t, side, p, out["plan"], _realized_vol(t), earn, gex)
            out["alternatives"] = picks["candidates"]
            out["contract"] = picks["best"]
            out["contract_why"] = picks["why"]
            if picks["blocked_by_earnings"] and out["contract"] is None:
                out["earnings"] = earn
        except Exception as e:  # noqa: BLE001
            log.warning("contract pick failed %s: %s", sym, type(e).__name__)
    if full:
        out["quote"] = latest_quote(t)
        try:
            out["news"] = news_sentiment(t)
        except Exception as e:  # noqa: BLE001
            log.warning("news failed %s: %s", sym, type(e).__name__)
            out["news"] = None
        if out["contract"] is None and out["earnings"] is None:
            out["earnings"] = earnings_in_window(t, (datetime.now(NY).date() + timedelta(days=14)).isoformat())
        out["confirm"] = build_confirmation(out)
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
        return "\u0627\u0644\u062a\u0642\u064a\u064a\u0645 \u063a\u064a\u0631 \u0645\u062a\u0648\u0641\u0631"
    icon = {"\u0642\u0648\u064a\u0629": "\U0001f4aa", "\u0645\u062a\u0648\u0633\u0637\u0629": "\U0001f44d", "\u0645\u062d\u0627\u064a\u062f\u0629": "\u23f8\ufe0f"}[ev["label"]]
    if ev["label"] == "\u0645\u062d\u0627\u064a\u062f\u0629":
        return f"{icon} {ev['score']:+d}/{MAX_SCORE} \u0645\u062d\u0627\u064a\u062f\u0629\u060c \u0644\u0627 \u062a\u0648\u0635\u064a\u0629"
    arrow = "\U0001f7e2" if ev["side"] == "CALL" else "\U0001f534"
    return f"{icon} {ev['score']:+d}/{MAX_SCORE} {ev['side']} {ev['label']} {arrow}"


def format_hit(h):
    ratio = "\u062c\u062f\u064a\u062f (OI=0)" if h["oi"] == 0 else f"{h['ratio']:.1f}x"
    icon = "\U0001f7e2" if h["side"] == "CALL" else "\U0001f534"
    return (
        f"{icon} {h['symbol']} {h['side']}  Strike {h['strike']:g}  \u0627\u0646\u062a\u0647\u0627\u0621 {h['expiry']}\n"
        f"   \u062d\u062c\u0645: {h['volume']:,} | OI: {h['oi']:,} | \u0627\u0644\u0646\u0633\u0628\u0629: {ratio}\n"
        f"   \u0633\u0639\u0631 \u0627\u0644\u0639\u0642\u062f: ${h['last']:.2f} | \u0642\u064a\u0645\u0629 \u0627\u0644\u062a\u062f\u0627\u0648\u0644: {money(h['premium'])}\n"
        f"   \u0633\u0639\u0631 \u0627\u0644\u0633\u0647\u0645: ${h['underlying']:.2f}"
    )


def format_hits(hits, ratings=None, title="\U0001f525 \u0646\u0634\u0627\u0637 \u0623\u0648\u0628\u0634\u0646\u0632 \u063a\u064a\u0631 \u0639\u0627\u062f\u064a"):
    body = "\n\n".join(format_hit(h) for h in hits)
    out = f"{title}\n\n{body}"
    if ratings:
        lines = [f"{sym}: {rating_line(ev)}" for sym, ev in ratings.items()]
        out += "\n\n\U0001f9e0 \u0627\u0644\u062a\u0642\u064a\u064a\u0645 (\u0645\u0646 -8 \u0625\u0644\u0649 +8):\n" + "\n".join(lines)
        out += "\n\u0644\u0644\u062a\u0641\u0627\u0635\u064a\u0644 \u0648\u0627\u0644\u062e\u0637\u0629 \u0623\u0631\u0633\u0644 \u0631\u0645\u0632 \u0627\u0644\u0633\u0647\u0645."
    return f"{out}\n\n{DISCLAIMER}\n\u0627\u0644\u0628\u064a\u0627\u0646\u0627\u062a \u0645\u0646 Yahoo \u0648\u0642\u062f \u062a\u062a\u0623\u062e\u0631 ~15 \u062f\u0642\u064a\u0642\u0629."


def format_gex(g, price):
    net = g["net"]
    if abs(net) < 100_000:
        regime, net_txt = "\u0645\u062a\u0639\u0627\u062f\u0644 (\u0645\u0627 \u0641\u064a\u0647 \u0645\u064a\u0644 \u0648\u0627\u0636\u062d)", money(abs(net))
    elif net > 0:
        regime, net_txt = "\u0645\u0648\u062c\u0628 (\u0635\u0646\u0627\u0639 \u0627\u0644\u0633\u0648\u0642 \u064a\u062e\u0641\u0641\u0648\u0646 \u0627\u0644\u062d\u0631\u0643\u0629)", "+" + money(net)
    else:
        regime, net_txt = "\u0633\u0627\u0644\u0628 (\u0627\u0644\u062d\u0631\u0643\u0629 \u062a\u062a\u0636\u062e\u0645)", "-" + money(abs(net))
    lines = [
        "\U0001f9f2 \u0627\u0644\u062c\u0627\u0645\u0627 (GEX):",
        f"\u0627\u0644\u0648\u0636\u0639: {regime}",
        f"\u0635\u0627\u0641\u064a GEX: {net_txt} \u0644\u0643\u0644 1% \u062d\u0631\u0643\u0629",
    ]
    if g["flip"]:
        side = "\u0627\u0644\u0633\u0639\u0631 \u0641\u0648\u0642\u0647" if price > g["flip"] else "\u0627\u0644\u0633\u0639\u0631 \u062a\u062d\u062a\u0647"
        lines.append(f"\u0645\u0633\u062a\u0648\u0649 \u0642\u0644\u0628 \u0627\u0644\u062c\u0627\u0645\u0627: ${g['flip']:.2f} ({side})")
    if g["call_wall"]:
        lines.append(f"\u062c\u062f\u0627\u0631 CALL (\u0645\u0642\u0627\u0648\u0645\u0629 \u0645\u062d\u062a\u0645\u0644\u0629): ${g['call_wall']:g}")
    if g["put_wall"]:
        lines.append(f"\u062c\u062f\u0627\u0631 PUT (\u062f\u0639\u0645 \u0645\u062d\u062a\u0645\u0644): ${g['put_wall']:g}")
    if g.get("est_frac", 0) > 0.3:
        lines.append(f"\u26a0\ufe0f {g['est_frac'] * 100:.0f}% \u0645\u0646 \u0627\u0644\u0639\u0642\u0648\u062f IV \u0645\u0642\u062f\u0651\u0631 (\u0627\u0644\u0633\u0648\u0642 \u0645\u0633\u0643\u0631 \u0623\u0648 \u0628\u064a\u0627\u0646\u0627\u062a Yahoo \u0646\u0627\u0642\u0635\u0629)\u060c \u0641\u0627\u0644\u0623\u0631\u0642\u0627\u0645 \u0623\u0642\u0644 \u062f\u0642\u0629.")
    lines.append("\u062a\u0642\u062f\u064a\u0631 \u062a\u0642\u0631\u064a\u0628\u064a \u0645\u0646 Yahoo\u060c \u0648\u0627\u0644\u0640 OI \u064a\u062a\u062d\u062f\u062b \u0645\u0631\u0629 \u0628\u0627\u0644\u064a\u0648\u0645 \u0642\u0628\u0644 \u0627\u0644\u0627\u0641\u062a\u062a\u0627\u062d.")
    return "\n".join(lines)


def format_evaluation(ev):
    lines = [f"\U0001f4ca {ev['symbol']}  ${ev['price']:.2f}", f"\u0627\u0644\u062a\u0642\u064a\u064a\u0645: {rating_line(ev)}", ""]
    lines.append("\u0627\u0644\u0623\u0633\u0628\u0627\u0628:")
    lines.extend(f"\u2022 {r}" for r in ev["reasons"])

    if ev.get("gex"):
        lines.append("\n" + format_gex(ev["gex"], ev["price"]))
    if ev.get("gex_warn"):
        lines.append("\n\u26a0\ufe0f " + ev["gex_warn"])

    plan, c = ev["plan"], ev["contract"]
    if plan is None:
        lines.append("\n\u0627\u0644\u0625\u0634\u0627\u0631\u0627\u062a \u063a\u064a\u0631 \u0645\u062a\u0648\u0627\u0641\u0642\u0629 \u0628\u0634\u0643\u0644 \u0643\u0627\u0641\u064d\u060c \u0627\u0644\u0623\u0641\u0636\u0644 \u0627\u0644\u0627\u0646\u062a\u0638\u0627\u0631.")
    else:
        if not ev["strong"]:
            lines.append("\n\u2139\ufe0f \u0627\u0644\u0625\u0634\u0627\u0631\u0629 \u0645\u062a\u0648\u0633\u0637\u0629 \u0648\u0644\u064a\u0633\u062a \u0642\u0648\u064a\u0629\u060c \u0641\u062e\u0641\u0641 \u062d\u062c\u0645 \u0627\u0644\u0635\u0641\u0642\u0629 \u0623\u0648 \u0627\u0646\u062a\u0638\u0631 \u062a\u0623\u0643\u064a\u062f.")
        lines.append(
            "\n\U0001f3af \u062e\u0637\u0629 \u0639\u0644\u0649 \u0633\u0639\u0631 \u0627\u0644\u0633\u0647\u0645 (\u0628\u0646\u0627\u0621\u064b \u0639\u0644\u0649 ATR):\n"
            f"\u062f\u062e\u0648\u0644: ~${plan['entry']:.2f}\n"
            f"\u0648\u0642\u0641 \u0627\u0644\u062e\u0633\u0627\u0631\u0629: ${plan['stop']:.2f}\n"
            f"\u0647\u062f\u0641 1: ${plan['t1']:.2f}\n"
            f"\u0647\u062f\u0641 2: ${plan['t2']:.2f}\n"
            f"\u0627\u0644\u0645\u062e\u0627\u0637\u0631\u0629:\u0627\u0644\u0639\u0627\u0626\u062f = 1:{plan['rr']:.1f}"
        )
        if c:
            lines.append(
                f"\n\U0001f4dd \u0639\u0642\u062f \u0645\u0642\u062a\u0631\u062d ({c['side']} \u0642\u0631\u064a\u0628 \u0645\u0646 \u0627\u0644\u0633\u0639\u0631):\n"
                f"{ev['symbol']} {c['side']} {c['strike']:g}  \u0627\u0646\u062a\u0647\u0627\u0621 {c['expiry']} ({c['dte']} \u064a\u0648\u0645)\n"
                f"Bid/Ask: ${c['bid']:.2f}/${c['ask']:.2f} | \u0633\u0628\u0631\u064a\u062f {c['spread'] * 100:.1f}%\n"
                f"IV: {c['iv'] * 100:.0f}% | OI: {c['oi']:,} | \u062d\u062c\u0645 \u0627\u0644\u064a\u0648\u0645: {c['volume']:,}"
            )
            if ev["earnings"]:
                lines.append(f"\u26a0\ufe0f \u0641\u064a\u0647 \u0625\u0639\u0644\u0627\u0646 \u0623\u0631\u0628\u0627\u062d \u0628\u062a\u0627\u0631\u064a\u062e {ev['earnings']} \u0642\u0628\u0644 \u0627\u0644\u0627\u0646\u062a\u0647\u0627\u0621\u060c \u0648\u0627\u0644\u062a\u0642\u0644\u0628 \u0628\u0639\u062f\u0647 \u064a\u0643\u0648\u0646 \u0639\u0627\u0644\u064a.")
        else:
            lines.append(
                f"\n\u0645\u0627 \u0644\u0642\u064a\u062a \u0639\u0642\u062f \u0633\u064a\u0648\u0644\u062a\u0647 \u0645\u0646\u0627\u0633\u0628\u0629 ({MIN_DTE}-{MAX_DTE} \u064a\u0648\u0645 \u0648\u0633\u0628\u0631\u064a\u062f \u0623\u0642\u0644 \u0645\u0646 {MAX_SPREAD * 100:.0f}%)."
            )
    lines.append(f"\n{DISCLAIMER}")
    return "\n".join(lines)


ALIASES = {
    "TESLA": "TSLA", "APPLE": "AAPL", "NVIDIA": "NVDA", "AMAZON": "AMZN", "MICROSOFT": "MSFT",
    "GOOGLE": "GOOGL", "ALPHABET": "GOOGL", "FACEBOOK": "META", "NETFLIX": "NFLX", "INTEL": "INTC",
    "PALANTIR": "PLTR", "COINBASE": "COIN",
    "\u062a\u0633\u0644\u0627": "TSLA", "\u0627\u0628\u0644": "AAPL", "\u0623\u0628\u0644": "AAPL", "\u0627\u0646\u0641\u064a\u062f\u064a\u0627": "NVDA", "\u0646\u0641\u064a\u062f\u064a\u0627": "NVDA",
    "\u0627\u0645\u0627\u0632\u0648\u0646": "AMZN", "\u0623\u0645\u0627\u0632\u0648\u0646": "AMZN", "\u0645\u0627\u064a\u0643\u0631\u0648\u0633\u0648\u0641\u062a": "MSFT", "\u0642\u0648\u0642\u0644": "GOOGL", "\u062c\u0648\u062c\u0644": "GOOGL",
    "\u0645\u064a\u062a\u0627": "META", "\u0646\u062a\u0641\u0644\u0643\u0633": "NFLX",
}
RIYADH = ZoneInfo("Asia/Riyadh")
AR_DAYS = ["\u0627\u0644\u0627\u062b\u0646\u064a\u0646", "\u0627\u0644\u062b\u0644\u0627\u062b\u0627\u0621", "\u0627\u0644\u0623\u0631\u0628\u0639\u0627\u0621", "\u0627\u0644\u062e\u0645\u064a\u0633", "\u0627\u0644\u062c\u0645\u0639\u0629", "\u0627\u0644\u0633\u0628\u062a", "\u0627\u0644\u0623\u062d\u062f"]


def _sym(raw):
    s = (raw or "").strip().lstrip("$")
    key = s.upper() if s.isascii() else s
    s2 = ALIASES.get(key, s.upper())
    return s2 if re.fullmatch(r"[A-Z][A-Z.\-]{0,5}", s2) else None


# ---- news -------------------------------------------------------------------
_POS = (
    "beat beats tops surge surges soar soars jump jumps rally rallies record upgrade upgraded upgrades raises raised "
    "outperform bullish strong growth profit profits approval approved partnership expands boost boosts breakthrough "
    "gain gains buyback wins rebound rebounds optimistic accelerates exceeds exceeded climbs rises"
).split()
_NEG = (
    "miss misses missed falls fall drops drop plunge plunges tumble tumbles slump slumps downgrade downgraded "
    "downgrades cuts warning warns probe investigation lawsuit sues sued fine fined recall layoffs fraud bearish weak "
    "decline declines loss losses delay delays halts ban bans tariff tariffs sanctions bankruptcy resigns selloff "
    "slides sinks underperform antitrust concern concerns slowdown disappoints disappointing"
).split()
_POS_RE = re.compile(r"\b(" + "|".join(_POS) + r")\b", re.I)
_NEG_RE = re.compile(r"\b(" + "|".join(_NEG) + r")\b", re.I)


def _clamp(x, lo=-2, hi=2):
    return max(lo, min(hi, x))


def _headline_score(title, summary):
    st = _clamp(len(set(w.lower() for w in _POS_RE.findall(title))) - len(set(w.lower() for w in _NEG_RE.findall(title))))
    ss = _clamp(len(set(w.lower() for w in _POS_RE.findall(summary))) - len(set(w.lower() for w in _NEG_RE.findall(summary))))
    return st + 0.5 * ss


def _parse_news_item(it):
    c = it.get("content") if isinstance(it.get("content"), dict) else it
    title = (c.get("title") or "").strip()
    if not title:
        return None
    summary = (c.get("summary") or c.get("description") or "").strip()
    ts = None
    pub = c.get("pubDate") or c.get("displayTime")
    if pub:
        try:
            ts = datetime.fromisoformat(str(pub).replace("Z", "+00:00"))
        except ValueError:
            ts = None
    if ts is None and it.get("providerPublishTime"):
        try:
            ts = datetime.fromtimestamp(int(it["providerPublishTime"]), tz=timezone.utc)
        except (TypeError, ValueError, OSError):
            ts = None
    prov = ""
    p = c.get("provider")
    if isinstance(p, dict):
        prov = p.get("displayName") or ""
    prov = prov or c.get("publisher") or it.get("publisher") or ""
    return {"title": title, "summary": summary, "ts": ts, "publisher": prov}


def news_sentiment(t):
    raw = t.news or []
    now = datetime.now(timezone.utc)
    items = []
    for it in raw[:30]:
        p = _parse_news_item(it)
        if not p or p["ts"] is None:
            continue
        if p["ts"].tzinfo is None:
            p["ts"] = p["ts"].replace(tzinfo=timezone.utc)
        age_h = max((now - p["ts"]).total_seconds() / 3600, 0.0)
        if age_h > NEWS_HOURS:
            continue
        s = _headline_score(p["title"], p["summary"][:300])
        w = 1.0 if age_h <= 12 else (0.7 if age_h <= 36 else 0.4)
        p.update(age_h=age_h, s=s, w=w)
        items.append(p)
    items.sort(key=lambda x: x["age_h"])
    total = sum(x["s"] * x["w"] for x in items)
    label = "positive" if total >= 1.5 else ("negative" if total <= -1.5 else "neutral")
    return {"score": total, "label": label, "items": items, "count": len(items)}


def format_news(nw):
    lines = [f"\U0001f4f0 \u0627\u0644\u0623\u062e\u0628\u0627\u0631 (\u0622\u062e\u0631 {NEWS_HOURS} \u0633\u0627\u0639\u0629):"]
    if nw is None:
        lines.append("\u062a\u0639\u0630\u0651\u0631 \u062c\u0644\u0628 \u0627\u0644\u0623\u062e\u0628\u0627\u0631 \u0627\u0644\u0622\u0646.")
        return "\n".join(lines)
    if nw["count"] == 0:
        lines.append("\u0645\u0627 \u0641\u064a\u0647 \u0623\u062e\u0628\u0627\u0631 \u062d\u062f\u064a\u062b\u0629 \u0639\u0644\u0649 \u0627\u0644\u0633\u0647\u0645\u060c \u0641\u0627\u0644\u0642\u0631\u0627\u0631 \u0645\u0628\u0646\u064a \u0639\u0644\u0649 \u0627\u0644\u0641\u0646\u064a \u0648\u0627\u0644\u0623\u0648\u0628\u0634\u0646\u0632 \u0641\u0642\u0637.")
        return "\n".join(lines)
    lab = {"positive": "\u0625\u064a\u062c\u0627\u0628\u064a\u0629 \U0001f7e2", "negative": "\u0633\u0644\u0628\u064a\u0629 \U0001f534", "neutral": "\u0645\u062d\u0627\u064a\u062f\u0629 \u26aa"}[nw["label"]]
    lines.append(f"\u0627\u0644\u0645\u064a\u0644 \u0627\u0644\u0639\u0627\u0645: {lab} ({nw['count']} \u062e\u0628\u0631)")
    for it in nw["items"][:4]:
        icon = "\U0001f7e2" if it["s"] >= 1 else ("\U0001f534" if it["s"] <= -1 else "\u26aa")
        age = f"{int(it['age_h'])} \u0633" if it["age_h"] >= 1 else f"{int(it['age_h'] * 60)} \u062f"
        title = it["title"] if len(it["title"]) <= 100 else it["title"][:97] + "..."
        src_name = f" ({it['publisher']})" if it["publisher"] else ""
        lines.append(f"{icon} \u0642\u0628\u0644 {age}: {title}{src_name}")
    lines.append("\u0627\u0644\u062a\u0635\u0646\u064a\u0641 \u062a\u0644\u0642\u0627\u0626\u064a \u0628\u0627\u0644\u0643\u0644\u0645\u0627\u062a \u0627\u0644\u0645\u0641\u062a\u0627\u062d\u064a\u0629\u060c \u0627\u0642\u0631\u0623 \u0627\u0644\u0639\u0646\u0627\u0648\u064a\u0646 \u0628\u0646\u0641\u0633\u0643 \u0642\u0628\u0644 \u0627\u0644\u062f\u062e\u0648\u0644.")
    return "\n".join(lines)


# ---- fresh quote + entry window ---------------------------------------------
def latest_quote(t):
    try:
        h = t.history(period="1d", interval="1m")
        if h is None or h.empty:
            return None
        ts = h.index[-1]
        if ts.tzinfo is None:
            ts = ts.tz_localize(NY)
        age = max((pd.Timestamp.now(tz=NY) - ts).total_seconds() / 60, 0.0)
        return {"price": float(h["Close"].iloc[-1]), "age_min": age, "ts": ts.to_pydatetime()}
    except Exception as e:  # noqa: BLE001
        log.warning("quote failed: %s", type(e).__name__)
        return None


def _fmt_riyadh(dt):
    r = dt.astimezone(RIYADH)
    return f"{AR_DAYS[r.weekday()]} {r:%Y-%m-%d} \u0627\u0644\u0633\u0627\u0639\u0629 {r:%H:%M} \u0628\u062a\u0648\u0642\u064a\u062a \u0627\u0644\u0631\u064a\u0627\u0636"


def _next_session_start(now_ny):
    d = now_ny.date()
    open_t = now_ny.replace(hour=9, minute=30, second=0, microsecond=0)
    cand = d if (now_ny.weekday() < 5 and now_ny < open_t) else d + timedelta(days=1)
    while cand.weekday() >= 5:
        cand += timedelta(days=1)
    return datetime(cand.year, cand.month, cand.day, 9, 30, tzinfo=NY)


def entry_window_text():
    now = datetime.now(NY)
    if is_market_open():
        start_ok = now.replace(hour=10, minute=0, second=0, microsecond=0)
        end_ok = now.replace(hour=15, minute=30, second=0, microsecond=0)
        if now < start_ok:
            return f"\u0627\u0644\u0633\u0648\u0642 \u0645\u0641\u062a\u0648\u062d \u0644\u0643\u0646 \u0623\u0648\u0644 30 \u062f\u0642\u064a\u0642\u0629 \u0645\u062a\u0642\u0644\u0628\u0629. \u0627\u0644\u0623\u0641\u0636\u0644 \u062a\u062f\u062e\u0644 \u0628\u0639\u062f {_fmt_riyadh(start_ok)}."
        if now > end_ok:
            return "\u0622\u062e\u0631 30 \u062f\u0642\u064a\u0642\u0629 \u0645\u0646 \u0627\u0644\u062c\u0644\u0633\u0629 \u0648\u062a\u0642\u0644\u0628\u0647\u0627 \u0639\u0627\u0644\u064a. \u0627\u0644\u0623\u0641\u0636\u0644 \u062a\u0646\u062a\u0638\u0631 \u0627\u0644\u062c\u0644\u0633\u0629 \u0627\u0644\u0642\u0627\u062f\u0645\u0629."
        return f"\u0627\u0644\u062d\u064a\u0646 \u0636\u0645\u0646 \u0623\u0641\u0636\u0644 \u0646\u0627\u0641\u0630\u0629 \u062f\u062e\u0648\u0644 (\u062d\u062a\u0649 {_fmt_riyadh(end_ok)})."
    nxt = _next_session_start(now)
    best = nxt.replace(hour=10, minute=0)
    return (
        f"\u0627\u0644\u0633\u0648\u0642 \u0645\u0633\u0643\u0631. \u0627\u0644\u062c\u0644\u0633\u0629 \u0627\u0644\u0642\u0627\u062f\u0645\u0629 \u062a\u0641\u062a\u062d {_fmt_riyadh(nxt)}\u060c \u0648\u0623\u0641\u0636\u0644 \u0648\u0642\u062a \u062f\u062e\u0648\u0644 \u0628\u0639\u062f {_fmt_riyadh(best)}.\n"
        "\u0623\u0639\u062f \u0625\u0631\u0633\u0627\u0644 \u0631\u0645\u0632 \u0627\u0644\u0633\u0647\u0645 \u0648\u0642\u062a\u0647\u0627 \u0639\u0634\u0627\u0646 \u064a\u062a\u062d\u062f\u062b \u0627\u0644\u062a\u0623\u0643\u064a\u062f. (\u0644\u0627 \u0623\u062d\u0633\u0628 \u0625\u062c\u0627\u0632\u0627\u062a \u0627\u0644\u0628\u0648\u0631\u0635\u0629)"
    )


# ---- confirmation checklist --------------------------------------------------
def build_confirmation(ev):
    checks, conflicts, waits = [], [], []
    side, score = ev["side"], ev["score"]

    if ev["strong"]:
        checks.append(("\u2705", f"\u0627\u0644\u062a\u0642\u064a\u064a\u0645 \u0627\u0644\u0641\u0646\u064a \u0642\u0648\u064a ({score:+d}/{MAX_SCORE}) \u0648\u0627\u0644\u062a\u0631\u0646\u062f \u064a\u0648\u0627\u0641\u0642 \u0627\u0644\u0627\u062a\u062c\u0627\u0647"))
    elif ev["label"] == "\u0645\u062a\u0648\u0633\u0637\u0629":
        checks.append(("\u274c", f"\u0627\u0644\u062a\u0642\u064a\u064a\u0645 \u0645\u062a\u0648\u0633\u0637 \u0641\u0642\u0637 ({score:+d}/{MAX_SCORE})\u060c \u0627\u0644\u0645\u0637\u0644\u0648\u0628 {STRONG_SCORE} \u0623\u0648 \u0623\u0643\u062b\u0631 \u0645\u0639 \u062a\u0631\u0646\u062f \u0648\u0627\u0636\u062d"))
        conflicts.append("\u0627\u0644\u062a\u0642\u064a\u064a\u0645 \u0627\u0644\u0641\u0646\u064a \u0645\u0627 \u0648\u0635\u0644 \u0644\u0644\u0642\u0648\u064a")
    else:
        checks.append(("\u274c", f"\u0627\u0644\u0625\u0634\u0627\u0631\u0627\u062a \u0627\u0644\u0641\u0646\u064a\u0629 \u0645\u062d\u0627\u064a\u062f\u0629 ({score:+d}/{MAX_SCORE})"))
        conflicts.append("\u0645\u0627 \u0641\u064a\u0647 \u0627\u062a\u062c\u0627\u0647 \u0648\u0627\u0636\u062d")

    nw = ev.get("news")
    if nw is None:
        checks.append(("\u2753", "\u0627\u0644\u0623\u062e\u0628\u0627\u0631 \u063a\u064a\u0631 \u0645\u062a\u0648\u0641\u0631\u0629 \u0627\u0644\u0622\u0646"))
        waits.append("\u062c\u0644\u0628 \u0627\u0644\u0623\u062e\u0628\u0627\u0631")
    elif nw["count"] == 0:
        checks.append(("\u2705", "\u0645\u0627 \u0641\u064a\u0647 \u0623\u062e\u0628\u0627\u0631 \u062d\u062f\u064a\u062b\u0629 \u062a\u0639\u0627\u0643\u0633 \u0627\u0644\u0627\u062a\u062c\u0627\u0647"))
    else:
        opposed = (side == "CALL" and nw["label"] == "negative") or (side == "PUT" and nw["label"] == "positive")
        aligned = (side == "CALL" and nw["label"] == "positive") or (side == "PUT" and nw["label"] == "negative")
        if opposed:
            checks.append(("\u274c", "\u0627\u0644\u0623\u062e\u0628\u0627\u0631 \u062a\u0639\u0627\u0643\u0633 \u0627\u062a\u062c\u0627\u0647 \u0627\u0644\u0635\u0641\u0642\u0629"))
            conflicts.append("\u0627\u0644\u0623\u062e\u0628\u0627\u0631 \u062a\u0639\u0627\u0643\u0633 \u0627\u0644\u0627\u062a\u062c\u0627\u0647")
        elif aligned:
            checks.append(("\u2705", "\u0627\u0644\u0623\u062e\u0628\u0627\u0631 \u062a\u062f\u0639\u0645 \u0627\u062a\u062c\u0627\u0647 \u0627\u0644\u0635\u0641\u0642\u0629"))
        else:
            checks.append(("\u2705", "\u0627\u0644\u0623\u062e\u0628\u0627\u0631 \u0645\u062d\u0627\u064a\u062f\u0629 \u0648\u0645\u0627 \u062a\u0639\u0627\u0643\u0633 \u0627\u0644\u0627\u062a\u062c\u0627\u0647"))

    if ev.get("gex") is None:
        checks.append(("\u2753", "\u0627\u0644\u062c\u0627\u0645\u0627 \u063a\u064a\u0631 \u0645\u062a\u0648\u0641\u0631\u0629: " + (ev.get("gex_reason") or "\u0628\u064a\u0627\u0646\u0627\u062a \u0627\u0644\u0623\u0648\u0628\u0634\u0646\u0632 \u0646\u0627\u0642\u0635\u0629")))
        waits.append("\u0628\u064a\u0627\u0646\u0627\u062a \u0627\u0644\u062c\u0627\u0645\u0627")
    elif ev.get("gex_warn"):
        checks.append(("\u274c", ev["gex_warn"]))
        conflicts.append("\u0627\u0644\u062c\u0627\u0645\u0627 \u062a\u0639\u0627\u0643\u0633 \u0627\u0644\u0627\u062a\u062c\u0627\u0647")
    else:
        checks.append(("\u2705", "\u0627\u0644\u062c\u0627\u0645\u0627: \u0645\u0627 \u0641\u064a\u0647 \u062c\u062f\u0627\u0631 \u0642\u0631\u064a\u0628 \u064a\u0639\u0627\u0643\u0633 \u0627\u0644\u0627\u062a\u062c\u0627\u0647"))

    ct = ev.get("contract")
    if ct:
        checks.append(("\u2705", f"\u0641\u064a\u0647 \u0639\u0642\u062f \u0633\u064a\u0648\u0644\u062a\u0647 \u062c\u064a\u062f\u0629 ({ct['dte']} \u064a\u0648\u0645\u060c \u0633\u0628\u0631\u064a\u062f {ct['spread'] * 100:.1f}%)"))
    elif ev.get("plan") is None:
        checks.append(("\u2796", "\u0645\u0627 \u0628\u062d\u062b\u062a \u0639\u0646 \u0639\u0642\u062f \u0644\u0623\u0646 \u0645\u0627 \u0641\u064a\u0647 \u0627\u062a\u062c\u0627\u0647 \u0648\u0627\u0636\u062d"))
    else:
        checks.append(("\u2753", "\u0645\u0627 \u0644\u0642\u064a\u062a \u0639\u0642\u062f \u0645\u0646\u0627\u0633\u0628: " + (ev.get("contract_why") or "\u0628\u064a\u0627\u0646\u0627\u062a \u0627\u0644\u0633\u0644\u0633\u0644\u0629 \u0646\u0627\u0642\u0635\u0629")))
        waits.append("\u0639\u0642\u062f \u0645\u0646\u0627\u0633\u0628")

    plan_ = ev.get("plan")
    if plan_ and plan_["rr"] < 1.5:
        checks.append(("\u274c", f"\u0627\u0644\u0639\u0627\u0626\u062f/\u0627\u0644\u0645\u062e\u0627\u0637\u0631\u0629 1:{plan_['rr']:.1f} \u0636\u0639\u064a\u0641 \u0628\u0639\u062f \u0636\u0628\u0637 \u0627\u0644\u0647\u062f\u0641 \u0639\u0644\u0649 \u0627\u0644\u062c\u0627\u0645\u0627 (\u0627\u0644\u0645\u0637\u0644\u0648\u0628 1.5 \u0623\u0648 \u0623\u0643\u062b\u0631)"))
        conflicts.append("\u0627\u0644\u0639\u0627\u0626\u062f \u0636\u0639\u064a\u0641")
    elif plan_:
        checks.append(("\u2705", f"\u0627\u0644\u0639\u0627\u0626\u062f/\u0627\u0644\u0645\u062e\u0627\u0637\u0631\u0629 1:{plan_['rr']:.1f}"))

    if ev.get("earnings"):
        checks.append(("\u274c", f"\u0625\u0639\u0644\u0627\u0646 \u0623\u0631\u0628\u0627\u062d \u0628\u062a\u0627\u0631\u064a\u062e {ev['earnings']} \u0636\u0645\u0646 \u0641\u062a\u0631\u0629 \u0627\u0644\u0635\u0641\u0642\u0629"))
        conflicts.append("\u0623\u0631\u0628\u0627\u062d \u0642\u0631\u064a\u0628\u0629")
    else:
        checks.append(("\u2705", "\u0645\u0627 \u0641\u064a\u0647 \u0625\u0639\u0644\u0627\u0646 \u0623\u0631\u0628\u0627\u062d \u0636\u0645\u0646 \u0641\u062a\u0631\u0629 \u0627\u0644\u0635\u0641\u0642\u0629"))

    q = ev.get("quote")
    if not is_market_open():
        checks.append(("\u23f3", "\u0627\u0644\u0633\u0648\u0642 \u0645\u0633\u0643\u0631\u060c \u0627\u0644\u062a\u0623\u0643\u064a\u062f \u0627\u0644\u0646\u0647\u0627\u0626\u064a \u064a\u0643\u0648\u0646 \u0623\u062b\u0646\u0627\u0621 \u0627\u0644\u062c\u0644\u0633\u0629"))
        waits.append("\u0641\u062a\u062d \u0627\u0644\u0633\u0648\u0642")
    elif q and q["age_min"] <= FRESH_MAX_MIN:
        checks.append(("\u2705", f"\u0628\u064a\u0627\u0646\u0627\u062a \u0627\u0644\u0633\u0639\u0631 \u062d\u062f\u064a\u062b\u0629 (\u0642\u0628\u0644 {int(q['age_min'])} \u062f)"))
    else:
        checks.append(("\u23f3", "\u0628\u064a\u0627\u0646\u0627\u062a \u0627\u0644\u0633\u0639\u0631 \u0645\u062a\u0623\u062e\u0631\u0629\u060c \u0623\u0639\u062f \u0627\u0644\u0625\u0631\u0633\u0627\u0644 \u0628\u0639\u062f \u062f\u0642\u064a\u0642\u0629"))
        waits.append("\u062a\u062d\u062f\u064a\u062b \u0627\u0644\u0628\u064a\u0627\u0646\u0627\u062a")

    if conflicts:
        verdict = "\u26d4 \u0644\u0627 \u062a\u062f\u062e\u0644 \u0627\u0644\u062d\u064a\u0646"
    elif waits:
        verdict = "\u23f3 \u0627\u0646\u062a\u0638\u0631 \u0627\u0644\u062a\u0623\u0643\u064a\u062f"
    else:
        verdict = f"\u2705 \u062f\u062e\u0648\u0644 \u0645\u0624\u0643\u062f {side}"
    return {"verdict": verdict, "checks": checks, "conflicts": conflicts, "waits": waits,
            "confirmed": not conflicts and not waits}


def format_contract(ev):
    ct = ev["contract"]
    exp = date.fromisoformat(ct["expiry"])
    lines = [
        "\U0001f4dd \u0627\u0644\u0639\u0642\u062f \u0627\u0644\u0645\u0641\u0636\u0644 (\u0645\u0646 \u0627\u0644\u0633\u0644\u0633\u0644\u0629 \u0627\u0644\u062d\u0642\u064a\u0642\u064a\u0629):",
        f"{ev['symbol']} {ct['side']} {ct['strike']:g}",
        f"\u062a\u0627\u0631\u064a\u062e \u0627\u0644\u0627\u0646\u062a\u0647\u0627\u0621: {ct['expiry']} ({AR_DAYS[exp.weekday()]}) \u0628\u0639\u062f {ct['dte']} \u064a\u0648\u0645",
        f"Bid/Ask: ${ct['bid']:.2f}/${ct['ask']:.2f} | \u0633\u0628\u0631\u064a\u062f {ct['spread'] * 100:.1f}%",
        f"IV: {ct['iv'] * 100:.0f}%{' (\u0645\u0642\u062f\u0651\u0631)' if ct.get('est_iv') else ''} | \u062f\u0644\u062a\u0627: {abs(ct['delta']):.2f} | "
        f"OI: {ct['oi']:,} | \u062d\u062c\u0645 \u0627\u0644\u064a\u0648\u0645: {ct['volume']:,}",
        f"\u062a\u0642\u062f\u064a\u0631 \u0627\u0644\u0631\u0628\u062d \u0639\u0646\u062f \u0647\u062f\u0641 1: {ct['gain1'] * 100:+.0f}% | \u0639\u0646\u062f \u0647\u062f\u0641 2: {ct['gain2'] * 100:+.0f}% | "
        f"\u0627\u0644\u062e\u0633\u0627\u0631\u0629 \u0639\u0646\u062f \u0648\u0642\u0641 \u0627\u0644\u0633\u0647\u0645: -{ct['loss'] * 100:.0f}%",
        "\u0644\u064a\u0634 \u0647\u0630\u0627 \u0627\u0644\u062a\u0627\u0631\u064a\u062e \u0648\u0627\u0644\u0633\u062a\u0631\u0627\u064a\u0643\u061f",
    ]
    lines += [f"\u2022 {r}" for r in ct.get("reasons", [])]
    alts = ev.get("alternatives") or []
    if len(alts) > 1:
        lines.append("\u0628\u062f\u0627\u0626\u0644:")
        for a in alts[1:3]:
            lines.append(
                f"\u2022 {a['strike']:g} \u0627\u0646\u062a\u0647\u0627\u0621 {a['expiry']} ({a['dte']} \u064a\u0648\u0645) | ask ${a['ask']:.2f} | "
                f"\u0647\u062f\u0641 1: {a['gain1'] * 100:+.0f}% | \u0648\u0642\u0641: -{a['loss'] * 100:.0f}%"
            )
    if ev.get("next_earnings"):
        lines.append(f"\u0623\u0642\u0631\u0628 \u0625\u0639\u0644\u0627\u0646 \u0623\u0631\u0628\u0627\u062d: {ev['next_earnings']}")
    lines.append(
        f"\u0627\u0644\u062a\u0642\u062f\u064a\u0631\u0627\u062a \u062a\u0641\u062a\u0631\u0636 \u062b\u0628\u0627\u062a IV \u0648\u0648\u0635\u0648\u0644 \u0627\u0644\u0633\u0647\u0645 \u0644\u0644\u0647\u062f\u0641 \u062e\u0644\u0627\u0644 {HOLD_DAYS} \u0623\u064a\u0627\u0645\u060c \u0648\u0647\u064a \u062a\u0642\u0631\u064a\u0628 \u0648\u0644\u064a\u0633\u062a \u0648\u0639\u062f. "
        "\u0644\u0627 \u062a\u062a\u0631\u0643\u0647 \u0644\u0622\u062e\u0631 \u064a\u0648\u0645 \u0642\u0628\u0644 \u0627\u0644\u0627\u0646\u062a\u0647\u0627\u0621."
    )
    return "\n".join(lines)


def format_full(ev, flow):
    c, q = ev["confirm"], ev.get("quote")
    price = q["price"] if q else ev["price"]
    head = f"\U0001f4ca {ev['symbol']}  ${price:.2f}"
    if q:
        r = q["ts"].astimezone(RIYADH)
        if is_market_open():
            head += f"\n\U0001f552 \u0622\u062e\u0631 \u062a\u062d\u062f\u064a\u062b \u0644\u0644\u0633\u0639\u0631: {r:%H:%M} \u0628\u062a\u0648\u0642\u064a\u062a \u0627\u0644\u0631\u064a\u0627\u0636 (\u0642\u0628\u0644 {int(q['age_min'])} \u062f)"
        else:
            head += f"\n\U0001f552 \u0633\u0639\u0631 \u0627\u0644\u0625\u063a\u0644\u0627\u0642: {r:%Y-%m-%d %H:%M} \u0628\u062a\u0648\u0642\u064a\u062a \u0627\u0644\u0631\u064a\u0627\u0636"
    lines = [head, "", f"\u0627\u0644\u0642\u0631\u0627\u0631: {c['verdict']}",
             "(\u0627\u0644\u062a\u0623\u0643\u064a\u062f = \u062a\u062d\u0642\u0642 \u0643\u0644 \u0627\u0644\u0634\u0631\u0648\u0637 \u062a\u062d\u062a\u060c \u0648\u0644\u064a\u0633 \u0636\u0645\u0627\u0646 \u0631\u0628\u062d)", "", "\u2714\ufe0f \u0642\u0627\u0626\u0645\u0629 \u0627\u0644\u062a\u0623\u0643\u064a\u062f:"]
    lines += [f"{i} {t}" for i, t in c["checks"]]
    if c["conflicts"]:
        lines.append("\n\u0627\u0644\u0645\u0627\u0646\u0639: " + "\u060c ".join(c["conflicts"]))
    elif c["waits"]:
        lines.append("\n\u0627\u0644\u0628\u0627\u0642\u064a: " + "\u060c ".join(c["waits"]))

    lines.append("\n\u23f0 \u0648\u0642\u062a \u0627\u0644\u062f\u062e\u0648\u0644:\n" + entry_window_text())

    plan, ct = ev["plan"], ev["contract"]
    if plan:
        tag = "\U0001f3af \u0627\u0644\u062e\u0637\u0629" if c["confirmed"] else "\U0001f3af \u062e\u0637\u0629 \u0645\u0628\u062f\u0626\u064a\u0629 (\u063a\u064a\u0631 \u0645\u0624\u0643\u062f\u0629 \u0628\u0639\u062f)"
        lines.append(
            f"\n{tag} \u0639\u0644\u0649 \u0633\u0639\u0631 \u0627\u0644\u0633\u0647\u0645 (ATR):\n"
            f"\u062f\u062e\u0648\u0644: ~${plan['entry']:.2f}\n"
            f"\u0648\u0642\u0641 \u0627\u0644\u062e\u0633\u0627\u0631\u0629: ${plan['stop']:.2f}\n"
            f"\u0647\u062f\u0641 1: ${plan['t1']:.2f}\n"
            f"\u0647\u062f\u0641 2: ${plan['t2']:.2f}\n"
            f"\u0627\u0644\u0645\u062e\u0627\u0637\u0631\u0629:\u0627\u0644\u0639\u0627\u0626\u062f = 1:{plan['rr']:.1f}"
        )
        if plan.get("note"):
            lines.append("\u26a0\ufe0f " + plan["note"])
        if ct:
            lines.append("\n" + format_contract(ev))

    lines.append("\n" + format_news(ev.get("news")))
    if ev.get("gex"):
        lines.append("\n" + format_gex(ev["gex"], price))
    else:
        lines.append("\n\U0001f9f2 \u0627\u0644\u062c\u0627\u0645\u0627: \u063a\u064a\u0631 \u0645\u062a\u0648\u0641\u0631\u0629. " + (ev.get("gex_reason") or ""))
    top = sorted(flow, key=lambda h: h["premium"], reverse=True)[:3]
    if top:
        lines.append("\n\U0001f525 \u0623\u0639\u0644\u0649 \u0639\u0642\u0648\u062f \u0646\u0634\u0627\u0637\u0627\u064b:\n\n" + "\n\n".join(format_hit(h) for h in top))
    lines.append("\n\U0001f9e0 \u062a\u0641\u0627\u0635\u064a\u0644 \u0627\u0644\u062a\u0642\u064a\u064a\u0645 \u0627\u0644\u0641\u0646\u064a:\n" + "\n".join(f"\u2022 {r}" for r in ev["reasons"]))
    lines.append(f"\n{DISCLAIMER}")
    return "\n".join(lines)


def gamma_report(raw):
    sym = _sym(raw)
    if not sym:
        return "\u0627\u0643\u062a\u0628 \u0627\u0644\u0623\u0645\u0631 \u0645\u0639 \u0631\u0645\u0632 \u0627\u0644\u0633\u0647\u0645\u060c \u0645\u062b\u0644: /gamma TSLA"
    t = get_ticker(sym)
    h = t.history(period="1d")
    if h is None or h.empty:
        return f"\u0645\u0627 \u0644\u0642\u064a\u062a \u0628\u064a\u0627\u0646\u0627\u062a \u0644\u0644\u0631\u0645\u0632 {sym}"
    price = float(h["Close"].iloc[-1])
    try:
        g = compute_gex(t, price, _realized_vol(t))
    except GexUnavailable as e:
        return f"\u0627\u0644\u062c\u0627\u0645\u0627 \u063a\u064a\u0631 \u0645\u062a\u0648\u0641\u0631\u0629 \u0639\u0644\u0649 {sym}: {e}"
    return f"\U0001f4ca {sym}  ${price:.2f}\n\n{format_gex(g, price)}\n\n{DISCLAIMER}"


def news_report(raw):
    sym = _sym(raw)
    if not sym:
        return "\u0627\u0643\u062a\u0628 \u0627\u0644\u0623\u0645\u0631 \u0645\u0639 \u0631\u0645\u0632 \u0627\u0644\u0633\u0647\u0645\u060c \u0645\u062b\u0644: /news TSLA"
    return f"\U0001f4ca {sym}\n\n{format_news(news_sentiment(get_ticker(sym)))}\n\n{DISCLAIMER}"


def analyze(raw):
    sym = _sym(raw)
    if not sym:
        return "\u0627\u0643\u062a\u0628 \u0631\u0645\u0632 \u0633\u0647\u0645 \u0635\u062d\u064a\u062d \u0645\u062b\u0644: AAPL\u060c \u0623\u0648 \u0627\u0633\u0645\u0647 \u0645\u062b\u0644: Tesla"
    try:
        flow = scan_ticker(sym)
    except Exception as e:  # noqa: BLE001
        log.warning("analyze flow failed: %s", type(e).__name__)
        flow = []
    ev = evaluate(sym, flow, full=True)
    if ev is None:
        return f"\u0645\u0627 \u0644\u0642\u064a\u062a \u0628\u064a\u0627\u0646\u0627\u062a \u0643\u0627\u0641\u064a\u0629 \u0644\u0644\u0631\u0645\u0632 {sym}"
    return format_full(ev, flow)


# ----------------------------------------------------------------------------
# Update handling
# ----------------------------------------------------------------------------
HELP = (
    "\u0623\u0648\u0627\u0645\u0631 \u0627\u0644\u0628\u0648\u062a:\n"
    "\u2022 \u0623\u0631\u0633\u0644 \u0631\u0645\u0632 \u0633\u0647\u0645 (\u0645\u062b\u0644 TSLA) \u0648\u064a\u062c\u064a\u0643 \u062a\u0642\u064a\u064a\u0645 + \u062e\u0637\u0629 + \u0639\u0642\u062f \u0645\u0642\u062a\u0631\u062d\n"
    "\u2022 /scan \u0641\u062d\u0635 \u0641\u0648\u0631\u064a \u0644\u0642\u0627\u0626\u0645\u0629 \u0627\u0644\u0645\u0631\u0627\u0642\u0628\u0629 \u0645\u0639 \u0627\u0644\u062a\u0642\u064a\u064a\u0645\n"
    "\u2022 /news TSLA \u0627\u0644\u0623\u062e\u0628\u0627\u0631 \u0648\u062a\u0635\u0646\u064a\u0641\u0647\u0627 (\u0625\u064a\u062c\u0627\u0628\u064a\u0629 \u0623\u0648 \u0633\u0644\u0628\u064a\u0629)\n"
    "\u2022 /gamma TSLA \u0645\u0633\u062a\u0648\u064a\u0627\u062a \u0627\u0644\u062c\u0627\u0645\u0627 \u0648\u0627\u0644\u062c\u062f\u0631\u0627\u0646\n"
    "\u2022 /watch \u0639\u0631\u0636 \u0642\u0627\u0626\u0645\u0629 \u0627\u0644\u0645\u0631\u0627\u0642\u0628\u0629\n"
    "\u2022 /help \u0647\u0630\u064a \u0627\u0644\u0631\u0633\u0627\u0644\u0629\n\n"
    f"\u0627\u0644\u0641\u062d\u0635 \u0627\u0644\u062a\u0644\u0642\u0627\u0626\u064a \u0643\u0644 {SCAN_INTERVAL // 60} \u062f\u0642\u064a\u0642\u0629 \u0623\u062b\u0646\u0627\u0621 \u062c\u0644\u0633\u0629 \u0627\u0644\u0633\u0648\u0642 \u0627\u0644\u0623\u0645\u0631\u064a\u0643\u064a."
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
            tg_send(chat_id, f"\u0631\u0642\u0645 \u0645\u062d\u0627\u062f\u062b\u062a\u0643: {chat_id}\n\u062d\u0637\u0647 \u0641\u064a \u0645\u062a\u063a\u064a\u0631 TELEGRAM_CHAT_ID \u0639\u0644\u0649 \u0627\u0644\u0627\u0633\u062a\u0636\u0627\u0641\u0629.")
            return
        if chat_id != CHAT_ID:
            return  # ignore strangers silently

        cmd = text.split()[0].split("@")[0].lower()
        if cmd in ("/start", "/help"):
            tg_send(chat_id, HELP)
        elif cmd == "/news":
            parts = text.split()
            tg_send(chat_id, "\u23f3 \u0623\u062c\u0644\u0628 \u0627\u0644\u0623\u062e\u0628\u0627\u0631...")
            tg_send(chat_id, news_report(parts[1] if len(parts) > 1 else ""))
        elif cmd == "/gamma":
            parts = text.split()
            tg_send(chat_id, "\u23f3 \u0623\u062d\u0633\u0628 \u0627\u0644\u062c\u0627\u0645\u0627...")
            tg_send(chat_id, gamma_report(parts[1] if len(parts) > 1 else ""))
        elif cmd == "/watch":
            tg_send(chat_id, "\u0642\u0627\u0626\u0645\u0629 \u0627\u0644\u0645\u0631\u0627\u0642\u0628\u0629:\n" + ", ".join(WATCHLIST))
        elif cmd == "/scan":
            tg_send(chat_id, "\u23f3 \u0623\u0641\u062d\u0635 \u0627\u0644\u0633\u0648\u0642...")
            hits = scan_all(dedupe=False)
            if hits:
                tg_send(chat_id, format_hits(hits, rate_symbols(hits)))
            else:
                tg_send(chat_id, "\u0645\u0627 \u0641\u064a\u0647 \u0646\u0634\u0627\u0637 \u063a\u064a\u0631 \u0639\u0627\u062f\u064a \u062d\u0627\u0644\u064a\u0627\u064b \u062d\u0633\u0628 \u0627\u0644\u0641\u0644\u0627\u062a\u0631.")
        elif text.startswith("/"):
            tg_send(chat_id, HELP)
        else:
            tg_send(chat_id, "\u23f3 \u0623\u062d\u0644\u0644...")
            tg_send(chat_id, analyze(text))
    except Exception as e:  # noqa: BLE001
        log.error("handle_update error: %s", type(e).__name__)
        if CHAT_ID and chat_id == CHAT_ID:
            tg_send(chat_id, "\u26a0\ufe0f \u062a\u0639\u0630\u0651\u0631 \u062c\u0644\u0628 \u0627\u0644\u0628\u064a\u0627\u0646\u0627\u062a \u0627\u0644\u0622\u0646\u060c \u063a\u0627\u0644\u0628\u0627\u064b Yahoo \u064a\u062d\u062f\u0651 \u0627\u0644\u0637\u0644\u0628\u0627\u062a. \u062c\u0631\u0628 \u0628\u0639\u062f \u062f\u0642\u064a\u0642\u0629.")


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
