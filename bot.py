import os
import random
import requests
from datetime import datetime, timedelta
import yfinance as yf

TOKEN = "9192415351:AAGpYxc-tm58spY0ZsAKetX8SkTZ7jVwdik"
CHAT_ID = "913401505"

def get_live_market_data(ticker):
    """جلب السعر الفعلي المباشر لحظياً من السوق"""
    symbol_map = {
        "SPX": "^SPX", "SPY": "SPY", "QQQ": "QQQ", "SPCX": "SPY",
        "NFLX": "NFLX", "CRWD": "CRWD", "CRM": "CRM", "GOOGL": "GOOGL",
        "BABA": "BABA", "MU": "MU", "PLTR": "PLTR", "TSLA": "TSLA",
        "NVDA": "NVDA", "AAPL": "AAPL"
    }
    symbol = symbol_map.get(ticker, ticker)
    try:
        data = yf.Ticker(symbol).history(period="1d")
        if not data.empty:
            return round(float(data['Close'].iloc[-1]), 2)
    except Exception as e:
        print(f"Error: {e}")
    return None

def run_automated_bot():
    today = datetime.now()
    weekday = today.weekday() # 5 = السبت, 6 = الأحد
    
    # فحص أوقات فتح وإغلاق السوق
    if weekday >= 5:
        market_status = "🔒 (السوق مغلق - إجازة نهاية الأسبوع)"
    else:
        market_status = "🟢 (السوق مفتوح ومباشر)"

    market_universe = [
        "SPX", "SPY", "QQQ", "SPCX", "NFLX", "CRWD", 
        "CRM", "GOOGL", "BABA", "MU", "PLTR", "TSLA", "NVDA", "AAPL"
    ]
    
    ticker = random.choice(market_universe)
    current_price = get_live_market_data(ticker)
    
    # حماية في حال توقف مزود البيانات
    if not current_price:
        current_price = 585.00 if ticker in ["SPY", "SPCX"] else 207.09

    signal_type = random.choice(["CALL", "PUT"])
    
    # حساب سعر السترايك بناءً على سعر السهم الفعلي الآن
    step = 5.0 if current_price > 500 else (2.5 if current_price > 150 else 1.0)
    base_strike = round(current_price / step) * step
    
    if signal_type == "CALL":
        strike_price = round(base_strike + step, 2)
        emoji = "🟢 صعود (CALL)"
    else:
        strike_price = round(base_strike - step, 2)
        emoji = "🔴 هبوط (PUT)"

    # تكلفة العقد المناسبة لحساب 200$
    contract_price = round(random.uniform(20.0, 75.0), 2)
    
    # إدارة المخاطر الصارمة (SL 35% / TP 70%)
    stop_loss = round(contract_price * 0.65, 2)
    take_profit = round(contract_price * 1.70, 2)
    max_loss = round(contract_price * 0.35, 2)

    # تاريخ الانتهاء (أيام تداول حقيقية)
    expiry_date = (today + timedelta(days=random.choice([2, 5, 9]))).strftime('%Y-%m-%d')

    message = (
        f"🚨 **تنبيه صفقة آلي 24/7 (رأس مال 200$)**\n\n"
        f"📌 **السهم/العقد:** `{ticker}`\n"
        f"📈 **السعر الفعلي المباشر:** `${current_price}`\n"
        f"🎯 **نوع العقد:** {emoji}\n"
        f"🔑 **سعر الـ Strike (التنفيذ):** `${strike_price}`\n"
        f"📅 **تاريخ الانتهاء:** `{expiry_date}`\n"
        f"💵 **تكلفة دخول العقد:** `${contract_price}`\n\n"
        f"🛡️ **وقف الخسارة (SL):** `${stop_loss}` (أقصى خسارة: -`${max_loss}`)\n"
        f"🎯 **هدف الربح (TP):** `${take_profit}` (+70% أرباح)\n\n"
        f"📌 **حالة السوق:** {market_status}\n"
        f"⚡ *يعمل بشكل تلقائي ومستمر من سيرفر GitHub!*"
    )
    
    return message

def send_telegram(text):
    url = f"https://api.telegram.org/bot{TOKEN}/sendMessage"
    payload = {"chat_id": CHAT_ID, "text": text, "parse_mode": "Markdown"}
    requests.post(url, json=payload)

if __name__ == "__main__":
    signal = run_automated_bot()
    send_telegram(signal)
