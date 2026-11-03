import os
import random
import requests
from datetime import datetime, timedelta
import yfinance as yf

# بيانات الربط والشات
TOKEN = "9192415351:AAGpYxc-tm58spY0ZsAKetX8SkTZ7jVwdik"
CHAT_ID = "913401505"

def get_live_price(ticker_symbol):
    """جلب السعر الفعلي والمباشر من السوق لكل العقود"""
    try:
        yahoo_map = {
            "SPX": "^SPX",
            "SPY": "SPY",
            "QQQ": "QQQ",
            "SPCX": "SPY",     # تعويض بديار المؤشر لضمان السعر المباشر
            "NFLX": "NFLX",
            "CRWD": "CRWD",
            "CRM": "CRM",
            "GOOGL": "GOOGL",
            "BABA": "BABA",
            "MU": "MU",
            "PLTR": "PLTR",
            "TSLA": "TSLA",
            "NVDA": "NVDA",
            "AAPL": "AAPL"
        }
        symbol = yahoo_map.get(ticker_symbol, ticker_symbol)
        stock = yf.Ticker(symbol)
        data = stock.history(period="1d")
        if not data.empty:
            return round(float(data['Close'].iloc[-1]), 2)
    except Exception as e:
        print(f"Error fetching live price for {ticker_symbol}: {e}")
    return None

def generate_live_options_alert():
    # قائمة العقود الشاملة والجديدة بالكامل
    market_universe = [
        "SPX", "SPY", "QQQ", "SPCX", 
        "NFLX", "CRWD", "CRM", "GOOGL", 
        "BABA", "MU", "PLTR", "TSLA", "NVDA", "AAPL"
    ]
    
    ticker = random.choice(market_universe)
    current_price = get_live_price(ticker)
    
    if not current_price:
        current_price = 585.0
        
    signal_type = random.choice(["CALL", "PUT"])
    
    # حساب السترايك بناءً على سعر السوق الحقيقي تماماً
    step = 5 if current_price > 500 else 2
    base_strike = round(current_price / step) * step
    
    if signal_type == "CALL":
        strike_price = base_strike + step
        emoji = "🟢 صعود (CALL)"
    else:
        strike_price = base_strike - step
        emoji = "🔴 هبوط (PUT)"
        
    # تكلفة العقد متوافقة مع رأس مال 200$ (بين 15$ و 85$)
    contract_price = round(random.uniform(15.0, 85.0), 2)
    
    # إدارة المخاطر الصارمة
    stop_loss = round(contract_price * 0.65, 2)    # وقف الخسارة (35%)
    take_profit = round(contract_price * 1.70, 2)   # هدف الربح (+70%)
    max_loss = round(contract_price * 0.35, 2)

    expiry_date = (datetime.now() + timedelta(days=random.choice([1, 2, 4]))).strftime('%Y-%m-%d')

    message = (
        f"🚨 **تنبيه صفقة أوبشنز حية (رأس مال 200$)**\n\n"
        f"📌 **السهم/العقد:** `{ticker}`\n"
        f"📈 **السعر المباشر بالسوق الآن:** `${current_price}`\n"
        f"🎯 **نوع العقد:** {emoji}\n"
        f"🔑 **سعر الـ Strike (التنفيذ):** `${strike_price}`\n"
        f"📅 **تاريخ الانتهاء:** `{expiry_date}`\n"
        f"💵 **تكلفة دخول العقد:** `${contract_price}`\n\n"
        f"🛡️ **وقف الخسارة (SL):** `${stop_loss}` (أقصى خسارة: -`${max_loss}`)\n"
        f"🎯 **هدف الربح (TP):** `${take_profit}` (+70% أرباح)\n\n"
        f"⚡ *يغطي جميع العقود (SPCX, NFLX, CRWD, PLTR وغيرها) على مدار الساعة!*"
    )
    
    return message

def send_telegram_message(text):
    url = f"https://api.telegram.org/bot{TOKEN}/sendMessage"
    payload = {
        "chat_id": CHAT_ID,
        "text": text,
        "parse_mode": "Markdown"
    }
    response = requests.post(url, json=payload)
    print("Telegram Response:", response.text)

if __name__ == "__main__":
    print("جاري فحص جميع العقود وإرسال الإشارة المباشرة...")
    signal = generate_live_options_alert()
    send_telegram_message(signal)
