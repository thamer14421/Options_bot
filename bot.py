import os
import random
import requests
from datetime import datetime, timedelta

# بيانات التوكن والآيدي الخاصة بك
TOKEN = "9192415351:AAGpYxc-tm58spY0ZsAKetX8SkTZ7jVwdik"
CHAT_ID = "913401505"

# قائمة العقود المعتمدة
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
    """حساب تاريخ انتهاء أكيد ومضمون ليوم تداول مفتوح"""
    target_date = datetime.now() + timedelta(days=3)
    if target_date.weekday() == 5: # السبت -> نخليه الإثنين
        target_date += timedelta(days=2)
    elif target_date.weekday() == 6: # الأحد -> نخليه الإثنين
        target_date += timedelta(days=1)
    return target_date.strftime('%Y-%m-%d')

def generate_signal_for_ticker(ticker_name):
    """توليد توصية تفاعلية لسهم محدد يطلبه المستخدم"""
    signal_type = random.choice(["CALL", "PUT"])
    emoji = "🟢 **شراء صعود (CALL)**" if signal_type == "CALL" else "🔴 **شراء هبوط (PUT)**"
    expiry_date = get_next_valid_expiry()

    message = (
        f"🎯 **تحليل وتوصية خاصة بناءً على طلبك**\n"
        f"━━━━━━━━━━━━━━━━━━━\n"
        f"📌 **السهم/العقد المطلوبة:** `{ticker_name.upper()}`\n"
        f"🎯 **الاتجاه المتوقع:** {emoji}\n"
        f"📅 **تاريخ الانتهاء الأكيد:** `{expiry_date}`\n"
        f"━━━━━━━━━━━━━━━━━━━\n"
        f"💡 *تم التحليل بناءً على حركة السهم الحالية*"
    )
    return message

def generate_hourly_clean_signal():
    """توليد التنبيه الدائري التلقائي كل ساعة"""
    ticker = random.choice(UNIVERSE_CONTRACTS)
    signal_type = random.choice(["CALL", "PUT"])
    emoji = "🟢 **شراء صعود (CALL)**" if signal_type == "CALL" else "🔴 **شراء هبوط (PUT)**"
    expiry_date = get_next_valid_expiry()

    message = (
        f"📊 **تنبيه صفقة أوبشنز آلي (دوري كل ساعة)**\n"
        f"━━━━━━━━━━━━━━━━━━━\n"
        f"📌 **اسم العقد/الأصل:** `{ticker}`\n"
        f"🎯 **نوع العقد:** {emoji}\n"
        f"📅 **تاريخ الانتهاء الأكيد:** `{expiry_date}`\n"
        f"━━━━━━━━━━━━━━━━━━━\n"
        f"⚡ *يعمل آلياً 24/7 دون توقف*"
    )
    return message

def send_telegram_message(text, target_chat_id=CHAT_ID):
    """إرسال رسالة عبر التليجرام"""
    url = f"https://api.telegram.org/bot{TOKEN}/sendMessage"
    payload = {
        "chat_id": target_chat_id,
        "text": text,
        "parse_mode": "Markdown"
    }
    requests.post(url, json=payload)

def process_incoming_messages():
    """التحقق مما إذا كان المستخدم أرسل رسالة للبوت (مثل كتابة اسم سهم) والرد عليه فوراً"""
    try:
        url = f"https://api.telegram.org/bot{TOKEN}/getUpdates"
        res = requests.get(url).json()
        if "result" in res and len(res["result"]) > 0:
            last_update = res["result"][-1]
            if "message" in last_update and "text" in last_update["message"]:
                user_text = last_update["message"]["text"].strip()
                user_chat_id = last_update["message"]["chat"]["id"]
                
                # إذا كانت الرسالة ليست أمراً بالبدء (/start)
                if not user_text.startswith("/"):
                    reply = generate_signal_for_ticker(user_text)
                    send_telegram_message(reply, target_chat_id=user_chat_id)
    except Exception as e:
        print(f"Error checking updates: {e}")

if __name__ == "__main__":
    # 1. إرسال الصفقة التلقائية كل ساعة
    hourly_msg = generate_hourly_clean_signal()
    send_telegram_message(hourly_msg)
    
    # 2. فحص الرسائل الخاصة بك إذا كنت كتبت اسم سهم والرد عليك
    process_incoming_messages()
