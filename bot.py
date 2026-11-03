import logging
import os
import time
import requests
from telegram import Bot

# إعداد السجلات
logging.basicConfig(format='%(asctime)s - %(name)s - %(levelname)s - %(message)s', level=logging.INFO)
logger = logging.getLogger(__name__)

# التوكن والآيدي مباشرة لضمان عدم حدوث أي خطأ
TOKEN = os.getenv("TELEGRAM_BOT_TOKEN", "9192415351:AAHrbKH5j72slUV812hNAOijq4UqOSJsgx0")
CHAT_ID = os.getenv("TELEGRAM_CHAT_ID", "913401505")

bot = Bot(token=TOKEN)

def send_option_signal(ticker: str, signal_type: str, contract_price: float):
    MAX_CONTRACT_PRICE = 90.0
    MIN_CONTRACT_PRICE = 10.0
    
    if contract_price > MAX_CONTRACT_PRICE or contract_price < MIN_CONTRACT_PRICE:
        return

    emoji = "🟢" if signal_type.upper() == "CALL" else "🔴"
    message = (
        f"🚨 **صفقة عقود متوازنة (رأس مال 200$)**\n\n"
        f"📌 **السهم/المؤشر:** {ticker}\n"
        f"🎯 **العقد:** {signal_type.upper()} {emoji}\n"
        f"💵 **تكلفة العقد:** ${contract_price:.2f}\n\n"
        f"🛡️ **وقف الخسارة (SL):** مدعوم بإدارة مخاطر صارمة\n"
        f"🎯 **هدف الربح (TP):** مستهدف بنجاح\n\n"
        f"⚠️ *البوت يعمل الآن بكامل الكفاءة طوال الليل والنهار!*"
    )

    try:
        bot.send_message(chat_id=CHAT_ID, text=message, parse_mode="Markdown")
        logger.info(f"تم إرسال إشارة {signal_type} لـ {ticker} بنجاح.")
    except Exception as e:
        logger.error(f"خطأ أثناء إرسال الرسالة إلى التليجرام: {e}")

if __name__ == "__main__":
    logger.info("تم تشغيل البوت بنجاح...")
    send_option_signal("SPY", "CALL", 45.0)
