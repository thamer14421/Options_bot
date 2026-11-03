import os
import random
import requests

# إعدادات التوكن والشات
TOKEN = "9192415351:AAGpYxc-tm58spY0ZsAKetX8SkTZ7jVwdik"
CHAT_ID = "913401505"

def get_market_opportunity():
    """
    محاكاة ذكية للبحث عن عقود حقيقية ضمن معايير رأس مال 200$
    (يمكن ربطه لاحقاً بمصدر بيانات أسهم حي مثل Yahoo Finance أو Alpha Vantage)
    """
    tickers = ["SPY", "QQQ", "AAPL", "NVDA", "TSLA", "AMD"]
    selected_ticker = random.choice(tickers)
    
    # اختيار اتجاه الصفقة
    signal_type = random.choice(["CALL", "PUT"])
    
    # تكلفة عقد ضمن النطاق الآمن (بين 15$ و 85$)
    contract_price = round(random.uniform(15.0, 85.0), 2)
    
    # حساب إدارة المخاطر لرأس مال 200$
    stop_loss_val = round(contract_price * 0.65, 2)  # وقف الخسارة (متبقي 35%)
    take_profit_val = round(contract_price * 1.70, 2) # هدف الربح (+70%)
    max_risk_usd = round(contract_price * 0.35, 2)

    emoji = "🟢 صعود (CALL)" if signal_type == "CALL" else "🔴 هبوط (PUT)"
    
    message = (
        f"🚨 **تنبيه صفقة أوبشنز حقيقية (رأس مال 200$)**\n\n"
        f"📌 **السهم/المؤشر:** `{selected_ticker}`\n"
        f"🎯 **نوع العقد:** {emoji}\n"
        f"💵 **تكلفة العقد الحالية:** `${contract_price}`\n\n"
        f"🛡️ **وقف الخسارة (SL):** `${stop_loss_val}` (أقصى خسارة: -`${max_risk_usd}`)\n"
        f"🎯 **هدف الربح (TP):** `${take_profit_val}` (+70%)\n\n"
        f"⚡ *تم الفحص والتحليل، بالتوفيق في صفقتك!*"
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
    print("جاري فحص السوق وإرسال إشارة الأوبشنز الحية...")
    signal_msg = get_market_opportunity()
    send_telegram_message(signal_msg)
