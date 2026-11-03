"""
Options Alert Bot - \u064a\u0631\u0633\u0644 \u0625\u0634\u0627\u0631\u0627\u062a CALL / PUT \u0639\u0644\u0649 \u062a\u0644\u064a\u062c\u0631\u0627\u0645 (\u0627\u0644\u062a\u0646\u0641\u064a\u0630 \u064a\u062f\u0648\u064a).
\u0627\u0644\u0628\u064a\u0627\u0646\u0627\u062a: yfinance (\u0645\u062a\u0623\u062e\u0631\u0629 ~15 \u062f\u0642\u064a\u0642\u0629\u060c \u063a\u064a\u0631 \u0631\u0633\u0645\u064a\u0629).
"""
import os
import json
import logging
from datetime import datetime, date, timezone
from zoneinfo import ZoneInfo

import pandas as pd
import requests
import yfinance as yf

# (comment)
TELEGRAM_TOKEN = os.environ["TELEGRAM_TOKEN"]  # (comment)
CHAT_ID = os.environ["CHAT_ID"]                # (comment)

SYMBOLS = ["^SPX", "SPY", "QQQ", "IWM", "AAPL", "TSLA", "NVDA",
           "AMZN", "MSFT", "META", "AMD", "GOOGL"]  # (comment)

INTERVAL = "15m"          # (comment)
SCAN_EVERY_SEC = 300      # (comment)
COOLDOWN_MIN = 120        # (comment)

MIN_DTE, MAX_DTE = 2, 14  # (comment)
MIN_OPEN_INT = 100
MIN_VOLUME = 20
MAX_SPREAD_PCT = 0.15     # (comment)

STOP_LOSS_PCT = 0.30      # (comment)
TAKE_PROFIT_PCT = 0.50    # (comment)
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
    """\u062c\u0644\u0633\u0629 SPX \u0627\u0644\u0644\u064a\u0644\u064a\u0629: 8:15\u0645 - 9:15\u0635 \u0628\u062a\u0648\u0642\u064a\u062a \u0646\u064a\u0648\u064a\u0648\u0631\u0643 (\u0623\u062d\u062f-\u062c\u0645\u0639\u0629)."""
    now = datetime.now(NY)
    wd, t = now.weekday(), now.hour * 60 + now.minute
    if t >= 20 * 60 + 15 and wd in (6, 0, 1, 2, 3):
        return True
    if t < 9 * 60 + 15 and wd in (0, 1, 2, 3, 4):
        return True
    return False


def es_basis() -> float:
    """\u0627\u0644\u0641\u0631\u0642 \u0628\u064a\u0646 \u0639\u0642\u062f ES \u0627\u0644\u0645\u0633\u062a\u0642\u0628\u0644\u064a \u0648\u0645\u0624\u0634\u0631 SPX (\u0646\u062d\u062a\u0627\u062c\u0647 \u0644\u062a\u062d\u062f\u064a\u062f \u0627\u0644\u0633\u062a\u0631\u0627\u064a\u0643 \u0628\u0627\u0644\u0644\u064a\u0644)."""
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
    """\u064a\u0631\u062c\u0639 ('CALL'|'PUT', \u0633\u0639\u0631, rsi) \u0623\u0648 None"""
    src = "ES=F" if use_futures else symbol  # (comment)
    df = yf.Ticker(src).history(period="5d", interval=INTERVAL)
    if df is None or len(df) < 30:
        return None
    close = df["Close"]
    ema9 = close.ewm(span=9, adjust=False).mean()
    ema21 = close.ewm(span=21, adjust=False).mean()
    r = rsi(close)

    # (comment)
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
        for _, row in tbl.sort_values("dist").head(3).iterrows():  # (comment)
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
    emoji = "\U0001f7e2" if side == "CALL" else "\U0001f534"
    night_note = "\U0001f319 \u062c\u0644\u0633\u0629 \u0644\u064a\u0644\u064a\u0629 (\u0627\u0644\u0633\u064a\u0648\u0644\u0629 \u0623\u0642\u0644 \u0648\u0627\u0644\u0633\u0628\u0631\u064a\u062f \u0623\u0648\u0633\u0639)\n" if night else ""
    sl = c["mid"] * (1 - STOP_LOSS_PCT)
    tp = c["mid"] * (1 + TAKE_PROFIT_PCT)
    name = symbol.replace("^", "")
    return (
        f"{emoji} <b>{name} \u2014 {side}</b>\n"
        f"{night_note}"
        f"\u0633\u0639\u0631 \u0627\u0644\u0633\u0647\u0645: {price:.2f} | RSI: {rv:.0f}\n\n"
        f"\u0627\u0644\u0639\u0642\u062f: <b>{c['strike']:.0f} {side}</b> | \u0627\u0646\u062a\u0647\u0627\u0621 {c['exp']} ({c['dte']} \u064a\u0648\u0645)\n"
        f"Bid/Ask: {c['bid']:.2f} / {c['ask']:.2f} | \u062a\u0642\u0631\u064a\u0628\u064a: {c['mid']:.2f}\n"
        f"IV: {c['iv']:.0f}% | OI: {c['oi']} | Vol: {c['vol']}\n\n"
        f"\U0001f6d1 \u0648\u0642\u0641 \u0645\u0642\u062a\u0631\u062d: {sl:.2f} (-{STOP_LOSS_PCT:.0%})\n"
        f"\U0001f3af \u0647\u062f\u0641 \u0645\u0642\u062a\u0631\u062d: {tp:.2f} (+{TAKE_PROFIT_PCT:.0%})\n\n"
        f"\u26a0\ufe0f \u0625\u0634\u0627\u0631\u0629 \u0622\u0644\u064a\u0629 \u0644\u0644\u0645\u0639\u0644\u0648\u0645\u0627\u062a \u0641\u0642\u0637 \u0648\u0644\u064a\u0633\u062a \u062a\u0648\u0635\u064a\u0629. \u0627\u0644\u0628\u064a\u0627\u0646\u0627\u062a \u0645\u062a\u0623\u062e\u0631\u0629 \u062a\u0642\u0631\u064a\u0628\u0627\u064b 15 \u062f\u0642\u064a\u0642\u0629. "
        f"\u0627\u0644\u062a\u062f\u0627\u0648\u0644 \u0628\u0627\u0644\u062e\u064a\u0627\u0631\u0627\u062a \u0639\u0627\u0644\u064a \u0627\u0644\u0645\u062e\u0627\u0637\u0631 \u0648\u0642\u062f \u062a\u062e\u0633\u0631 \u0643\u0627\u0645\u0644 \u0627\u0644\u0645\u0628\u0644\u063a."
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
                price -= es_basis()  # (comment)
            key = f"{sym}-{side}"
            if key in last_sent and (now - datetime.fromisoformat(last_sent[key])).total_seconds() < COOLDOWN_MIN * 60:
                continue
            contract = pick_contract(sym, side, price)
            if not contract:
                logging.info("%s %s: \u0645\u0627 \u0644\u0642\u064a\u062a \u0639\u0642\u062f \u0633\u064a\u0648\u0644\u062a\u0647 \u0643\u0627\u0641\u064a\u0629", sym, side)
                continue
            send(build_message(sym, side, price, rv, contract, night=use_fut))
            last_sent[key] = now.isoformat()
            logging.info("\u0623\u0631\u0633\u0644\u062a %s", key)
        except Exception as e:
            logging.warning("%s \u062e\u0637\u0623: %s", sym, e)


if __name__ == "__main__":
    if market_open() or spx_night_open():
        scan()
        save_state(last_sent)
    else:
        logging.info("\u0627\u0644\u0633\u0648\u0642 \u0645\u0633\u0643\u0631")
