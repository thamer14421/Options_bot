import logging
import time
import requests
from telegram import Bot
from config import (
    TELEGRAM_BOT_TOKEN, 
    TELEGRAM_CHAT_ID, 
    MAX_CONTRACT_PRICE, 
    MIN_CONTRACT_PRICE,
    STOP_LOSS_PERCENT,
    TAKE_PROFIT_PERCENT
)

# إعداد سجل البيانات
logging.basicConfig(format='%(asctime)s - %(name)s - %(levelname)s - %(message)s', level=logging.INFO)
logger = logging.getLogger(__name__)

# إنشاء كائن البوت
bot = Bot(token=TELEGRAM_BOT_TOKEN)

def send_option_signal(ticker: str, signal_type: str, contract_price: float):
    """
    فحص سعر العقد وإرساله للتليجرام إذا كان بين 10$ و 90$
    """
    if contract_price > MAX_CONTRACT_PRICE or contract_price < MIN_CONTRACT_PRICE:
        return

    stop_loss_val = contract_price * (1 - STOP_LOSS_PERCENT)
    take_profit_val = contract_price * (1 + TAKE_PROFIT_PERCENT)
    max_risk_usd = contract_price * STOP_LOSS_PERCENT

    emoji = "🟢" if signal_type.upper() == "CALL" else "🔴"
    message = (
        f"🚨 **صفقة عقود متوازنة (رأس مال 200$)**\n\n"
        f"📌 **السهم/المؤشر:** {ticker}\n"
        f"🎯 **العقد:** {signal_type.upper()} {emoji}\n"
        f"💵 **تكلفة العقد:** ${contract_price:.2f}\n\n"
        f"🛡️ **وقف الخسارة (SL):** ${stop_loss_val:.2f} (خسارة متوقعة: -${max_risk_usd:.2f})\n"
        f"🎯 **هدف الربح (TP):** ${take_profit_val:.2f}\n\n"
        f"⚠️ *إدارة المخاطر: العقد يمثل جزءاً أميناً من حسابك.*"
    )

    try:
        bot.send_message(chat_id=TELEGRAM_CHAT_ID, text=message, parse_mode="Markdown")
        logger.info(f"تم إرسال إشارة {signal_type} لـ {ticker} بنجاح.")
    except Exception as e:
        logger.error(f"خطأ أثناء إرسال الرسالة إلى التليجرام: {e}")

if __name__ == "__main__":
    logger.info("تم تشغيل البوت بنجاح...")
    send_option_signal("SPY", "CALL", 45.0)
