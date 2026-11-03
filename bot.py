import os
import requests

# التوكن والآيدي الخاص بك
TOKEN = "9192415351:AAGpYxc-tm58spY0ZsAKetX8SkTZ7jVwdik"
CHAT_ID = "913401505"

def send_telegram_message():
    message = (
        "🚨 *صفقة عقود متوازنة (رأس مال 200$)*\n\n"
        "📌 *السهم/المؤشر:* SPY\n"
        "🎯 *العقد:* CALL 🟢\n"
        "💵 *تكلفة العقد:* $45.00\n\n"
        "🛡️ *وقف الخسارة (SL):* مدعوم بإدارة مخاطر صارمة\n"
        "🎯 *هدف الربح (TP):* مستهدف بنجاح\n\n"
        "⚠️ *البوت يعمل الآن بكامل الكفاءة وتصلك هذه الرسالة كتأكيد للربط!*"
    )
    
    url = f"https://api.telegram.org/bot{TOKEN}/sendMessage"
    payload = {
        "chat_id": CHAT_ID,
        "text": message,
        "parse_mode": "Markdown"
    }
    
    response = requests.post(url, json=payload)
    print("Telegram Response:", response.text)

if __name__ == "__main__":
    print("جاري إرسال التجربة عبر API التليجرام...")
    send_telegram_message()
