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
