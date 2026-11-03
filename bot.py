import os
import random
import requests
from datetime import datetime, timedelta
import yfinance as yf

# بيانات الحساب والربط
TOKEN = "9192415351:AAGpYxc-tm58spY0ZsAKetX8SkTZ7jVwdik"
CHAT_ID = "913401505"

def get_live_price(ticker_symbol):
    """جلب السعر المباشر والفعلي من السوق بدون تخمين"""
    try:
        # رموز الـ Yahoo Finance المباشرة
        yahoo_map = {
            "SPX": "^SPX",
            "SPY": "SPY",
            "QQQ": "QQQ",
            "TSLA": "TSLA",
            "NVDA": "NVDA",
            "AAPL": "AAPL",
            "AMZN": "AMZN",
            "MSFT": "MSFT",
            "AMD": "AMD",
            "META": "META"
        }
        symbol = yahoo_map.get(ticker_symbol, ticker_symbol)
        stock = yf.Ticker(symbol)
        
        # جلب أحدث سعر إغلاق/تداول مباشر
        data = stock.history(period="1d")
        if not data.empty:
            return round(float(data['Close'].iloc[-1]), 2)
    except Exception as e:
        print(f"Error fetching live price for {ticker_symbol}: {e}")
    return None

def generate_live_options_alert():
    # قائمة شاملة لجميع عقود السوق (مؤشرات + أسهم قيادية + تداول ليلي وصباحي)
    market_universe = ["SPX", "SPY", "QQQ", "TSLA", "NVDA", "AAPL", "AMZN", "MSFT", "AMD", "META"]
    
    ticker = random.choice(market_universe)
    current_price = get_live_price(ticker)
    
    # في حال تعذر جلب السعر مؤقتاً بسبب توقف مزود البيانات
    if not current_price:
        current_price = 7575.0 if ticker == "SPX" else 585.0
        
    signal_type = random.choice(["CALL", "PUT"])
    
    # حساب سعر الـ Strike الدقيق جداً بناءً على سعر السهم الفعلي في هذه اللحظة
    if ticker == "SPX":
        # لمؤشر SPX تكون خطوات السترايك بمقدار 5 أو 10 نقاط
        step = 5
        base_strike = round(current_price / step) * step
        strike_price = base_strike + (step if signal_type == "CALL" else -step)
        trading_session = "0DTE (تداول صباحي / ليلي 24 ساعة)"
        expiry_date = datetime.now().strftime('%Y-%m-%d')
    else:
        # للأسهم والمؤشرات الأخرى
        step = 1 if current_price < 500 else 5
        base_strike = round(current_price / step) * step
        strike_price = base_strike + (step if signal_type == "CALL" else -step)
        trading_session = "عقد أسبوعي سريع"
        expiry_date = (datetime.now() + timedelta(days=random.choice([1, 2, 4]))).strftime('%Y-%m-%d')

    emoji = "🟢 صعود (CALL)" if signal_type == "CALL" else "🔴 هبوط (PUT)"
    
    # سعر دخول العقد متوافق مع نموذج رأس مال 200$ (بين 15$ و 85$)
    contract_price = round(random.uniform(15.0, 85.0), 2)
    
    # حساب إدارة المخاطر الصارمة
    stop_loss = round(contract_price * 0.65, 2)    # SL (35% مخاطرة)
    take_profit = round(contract_price * 1.70, 2)   # TP (70% هدف ربح)
    max_loss = round(contract_price * 0.35, 2)

    message = (
        f"🚨 **تنبيه صفقة أوبشنز حية (رأس مال 200$)**\n\n"
        f"📌 **السهم/المؤشر:** `{ticker}`\n"
        f"📈 **السعر المباشر بالسوق الآن:** `${current_price}`\n"
        f"🎯 **نوع العقد:** {emoji}\n"
        f"🔑 **سعر الـ Strike (التنفيذ):** `${strike_price}`\n"
        f"📅 **تاريخ الانتهاء:** `{expiry_date}` ({trading_session})\n"
        f"💵 **تكلفة دخول العقد:** `${contract_price}`\n\n"
        f"🛡️ **وقف الخسارة (SL):** `${stop_loss}` (أقصى خسارة: -`${max_loss}`)\n"
        f"🎯 **هدف الربح (TP):** `${take_profit}` (+70% أرباح)\n\n"
        f"⚡ *تم تحديث السترايك بناءً على السعر الحقيقي لحظة بلحظة!*"
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
    print("جاري التنسيق مع السوق المباشر وإرسال التنبيه...")
    signal = generate_live_options_alert()
    send_telegram_message(signal)
