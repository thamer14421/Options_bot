import os
import random
import requests
from datetime import datetime, timedelta

TOKEN = "9192415351:AAGpYxc-tm58spY0ZsAKetX8SkTZ7jVwdik"
CHAT_ID = "913401505"

def get_next_valid_expiry():
    target_date = datetime.now() + timedelta(days=3)
    if target_date.weekday() == 5: # السبت
        target_date += timedelta(days=2)
    elif target_date.weekday() == 6: # الأحد
        target_date += timedelta(days=1)
    return target_date.strftime('%Y-%m-%d')

def generate_clean_signal():
    universe_contracts = [
        "SPX (المؤشر العام - ليلي/صباحي 24 ساعة)",
        "SPY (صندوق المؤشر)",
        "QQQ (مؤشر التقنية)",
        "SPCX / SPTX",
        "NFLX (نتفليكس)",
        "CRWD (كراود سترايك)",
        "CRM (سيلزفورس)",
        "GOOGL (جوجل)",
        "BABA (علي بابا)",
        "MU (ميكرون)",
        "PLTR (بالنتير)",
        "TSLA (تسلا)",
        "NVDA (نيفيديا)",
        "AAPL (أبل)"
    ]
    
    ticker = random.choice(universe_contracts)
    signal_type = random.choice(["CALL", "PUT"])
    
    if signal_type == "CALL":
        emoji = "🟢 **شراء صعود (CALL)**"
    else:
        emoji = "🔴 **شراء هبوط (PUT)**"

    expiry_date = get_next_valid_expiry()

    message = (
        f"📊 **تنبيه صفقة أوبشنز آلي**\n"
        f"━━━━━━━━━━━━━━━━━━━\n"
        f"📌 **اسم العقد/الأصل:** `{ticker}`\n"
        f"🎯 **نوع العقد:** {emoji}\n"
        f"📅 **تاريخ الانتهاء الأكيد:** `{expiry_date}`\n"
        f"━━━━━━━━━━━━━━━━━━━\n"
        f"⚡ *البوت يعمل آلياً وبشكل مستمر على مدار الساعة*"
    )
    
    return message

def send_telegram_message(text):
    url = f"https://api.telegram.org/bot{TOKEN}/sendMessage"
    payload = {
        "chat_id": CHAT_ID,
        "text": text,
        "parse_mode": "Markdown"
    }
    requests.post(url, json=payload)

if __name__ == "__main__":
    signal_msg = generate_clean_signal()
    send_telegram_message(signal_msg)
