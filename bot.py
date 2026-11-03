import os
import random
import requests
import threading
import time
from datetime import datetime, timedelta
from flask import Flask

app = Flask(__name__)

TOKEN = "9192415351:AAGpYxc-tm58spY0ZsAKetX8SkTZ7jVwdik"
CHAT_ID = "913401505"

UNIVERSE_CONTRACTS = [
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

def get_next_valid_expiry():
    target_date = datetime.now() + timedelta(days=3)
    if target_date.weekday() == 5:
        target_date += timedelta(days=2)
    elif target_date.weekday() == 6:
        target_date += timedelta(days=1)
    return target_date.strftime('%Y-%m-%d')

def generate_signal(ticker_name):
    signal_type = random.choice(["CALL", "PUT"])
    emoji = "🟢 **شراء صعود (CALL)**" if signal_type == "CALL" else "🔴 **شراء هبوط (PUT)**"
    expiry_date = get_next_valid_expiry()

    message = (
        f"🎯 **تحليل وعقد خاص لـ ({ticker_name.upper()})**\n"
        f"━━━━━━━━━━━━━━━━━━━\n"
        f"📌 **اسم العقد/الأصل:** `{ticker_name.upper()}`\n"
        f"🎯 **نوع العقد:** {emoji}\n"
        f"📅 **تاريخ الانتهاء الأكيد:** `{expiry_date}`\n"
        f"━━━━━━━━━━━━━━━━━━━\n"
        f"⚡ *تم الرد فورياً بناءً على طلبك!*"
    )
    return message

def send_telegram_message(text, target_chat_id=CHAT_ID):
    url = f"https://api.telegram.org/bot{TOKEN}/sendMessage"
    payload = {
        "chat_id": target_chat_id,
        "text": text,
        "parse_mode": "Markdown"
    }
    requests.post(url, json=payload)

def start_telegram_bot():
    """حلقة الاستماع الفوري للرسائل في الخلفية"""
    offset = 0
    print("البوت بدأ الاستماع للرسائل...")
    while True:
        try:
            url = f"https://api.telegram.org/bot{TOKEN}/getUpdates?offset={offset}&timeout=10"
            res = requests.get(url, timeout=15).json()
            if "result" in res:
                for update in res["result"]:
                    offset = update["update_id"] + 1
                    if "message" in update and "text" in update["message"]:
                        user_text = update["message"]["text"].strip()
                        user_chat_id = update["message"]["chat"]["id"]
                        
                        if not user_text.startswith("/"):
                            reply = generate_signal(user_text)
                            send_telegram_message(reply, target_chat_id=user_chat_id)
        except Exception as e:
            print(f"Error in polling: {e}")
            time.sleep(3)

@app.route('/')
def home():
    return "Bot is running 24/7!"

if __name__ == "__main__":
    # تشغيل بوت التيليجرام في خيط (Thread) منفصل
    t = threading.Thread(target=start_telegram_bot)
    t.daemon = True
    t.start()
    
    # تشغيل سيرفر الويب الخاص بـ Render
    port = int(os.environ.get("PORT", 10000))
    app.run(host="0.0.0.0", port=port)
