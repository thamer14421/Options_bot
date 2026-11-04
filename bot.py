import os
import time
import requests
from datetime import datetime, timedelta

TOKEN = "9192415351:AAGpYxc-tm58spY0ZsAKetX8SkTZ7jVwdik"

def get_stock_data_and_analysis(ticker):
    """جلب بيانات السهم وتحليله فنياً عبر مؤشرات حقيقية مع استراتيجيات تأكيد متقدمة"""
    try:
        url = f"https://query1.finance.yahoo.com/v8/finance/chart/{ticker.upper()}?interval=15m&range=5d"
        headers = {'User-Agent': 'Mozilla/5.0'}
        response = requests.get(url, headers=headers, timeout=5).json()
        
        result = response['chart']['result'][0]
        prices = result['indicators']['quote'][0]['close']
        prices = [p for p in prices if p is not None]
        
        if len(prices) < 14:
            return None
        
        current_price = round(prices[-1], 2)
        
        # حساب مؤشر RSI (14 فترة)
        gains = []
        losses = []
        for i in range(1, 15):
            change = prices[-i] - prices[-(i+1)]
            if change >= 0:
                gains.append(change)
                losses.append(0)
            else:
                gains.append(0)
                losses.append(abs(change))
                
        avg_gain = sum(gains) / 14
        avg_loss = sum(losses) / 14
        
        if avg_loss == 0:
            rsi = 100
        else:
            rs = avg_gain / avg_loss
            rsi = round(100 - (100 / (1 + rs)), 1)
            
        # حساب متوسط الحركة SMA (20 فترة)
        sma_20 = sum(prices[-20:]) / min(len(prices), 20)
        
        # استراتيجيات تحديد الاتجاه والتأكيد الفني
        distance_from_sma = round(((current_price - sma_20) / sma_20) * 100, 2)
        
        if current_price > sma_20 and rsi < 65:
            signal_type = "CALL"
            sentiment = "تأكيد إيجابي قوي (صاعد) 📈"
            reason = f"السهم فوق المتوسط بـ ({distance_from_sma}%) مع استقرار RSI عند ({rsi}) ضمن نطاق الشراء الآمن."
        elif current_price < sma_20 and rsi > 35:
            signal_type = "PUT"
            sentiment = "تأكيد سلبي قوي (هابط) 📉"
            reason = f"السهم تحت المتوسط بـ ({abs(distance_from_sma)}%) مع بقاء RSI عند ({rsi}) في نطاق الضغط البيعي."
        else:
            if current_price >= sma_20:
                signal_type = "CALL"
                sentiment = "اتجاه صاعد حذِر 📊"
                reason = f"اختبار لمناطق المقاومة مع تسجيل RSI لـ ({rsi})."
            else:
                signal_type = "PUT"
                sentiment = "اتجاه هابط حذِر 📊"
                reason = f"اختبار لمناطق الدعم مع تسجيل RSI لـ ({rsi})."
                
        return {
            "price": current_price,
            "signal": signal_type,
            "sentiment": sentiment,
            "reason": reason,
            "rsi": rsi,
            "sma": round(sma_20, 2),
            "distance": distance_from_sma
        }
    except Exception as e:
        print(f"Error analyzing {ticker}: {e}")
        return None

def get_next_valid_expiry():
    """تحديد أقرب تاريخ انتهاء عقد أوبشن رسمي (يوم الجمعة)"""
    today = datetime.now()
    days_ahead = 4 - today.weekday()
    if days_ahead <= 0:
        days_ahead += 7
    target_date = today + timedelta(days=days_ahead)
    return target_date.strftime('%Y-%m-%d')

def generate_confirmed_signal(ticker_name):
    ticker = ticker_name.upper().strip()
    analysis = get_stock_data_and_analysis(ticker)
    
    if not analysis:
        return f"⚠️ **تنبيه:** تعذر جلب البيانات التأكيدية لسهم `{ticker}`. تأكد من الرمز وحاول مجدداً."

    price = analysis["price"]
    signal_type = analysis["signal"]
    expiry_date = get_next_valid_expiry()

    if signal_type == "CALL":
        emoji = "🟢 **شراء صعود (CALL)**"
        strike_price = round(price * 1.01, 1)
        target_1 = round(price * 1.015, 2)
        target_2 = round(price * 1.03, 2)
        stop_loss = round(price * 0.99, 2)
        strat_note = "📌 **استراتيجية العقد:** الزخم الشرائي يدعم صعود السهم واختراق الأهداف."
    else:
        emoji = "🔴 **شراء هبوط (PUT)**"
        strike_price = round(price * 0.99, 1)
        target_1 = round(price * 0.985, 2)
        target_2 = round(price * 0.97, 2)
        stop_loss = round(price * 1.01, 2)
        strat_note = "📌 **استراتيجية العقد:** الضغط البيعي واستمرار التداول تحت المتوسط يرجح الهبوط."

    message = (
        f"🎯 **تحليل صفقات الأوبشن الاستراتيجي ({ticker})**\n"
        f"━━━━━━━━━━━━━━━━━━━\n"
        f"💰 **السعر اللحظي:** `${price}` | 📉 **المتوسط (SMA20):** `${analysis['sma']}`\n"
        f"📊 **مؤشر القوة RSI:** `{analysis['rsi']}`\n"
        f"🔍 **حالة الاتجاه:** {analysis['sentiment']}\n"
        f"💡 **المعيار الاستراتيجي:** {analysis['reason']}\n"
        f"━━━━━━━━━━━━━━━━━━━\n"
        f"🚀 **التوصية المؤكدة:** {emoji}\n"
        f"{strat_note}\n"
        f"📌 **السترايك المقترح:** `${strike_price}`\n"
        f"📅 **تاريخ الانتهاء:** `{expiry_date}`\n"
        f"━━━━━━━━━━━━━━━━━━━\n"
        f"🎯 **الهدف الأول:** `${target_1}`\n"
        f"🚀 **الهدف الثاني:** `${target_2}`\n"
        f"🛡️ **وقف الخسارة:** `${stop_loss}`\n"
        f"━━━━━━━━━━━━━━━━━━━\n"
        f"⚡ *نظام الاستطلاع المباشر - أداء دقيق ومستقر!*"
    )
    return message

def send_telegram_message(text, target_chat_id):
    url = f"https://api.telegram.org/bot{TOKEN}/sendMessage"
    payload = {
        "chat_id": target_chat_id,
        "text": text,
        "parse_mode": "Markdown"
    }
    requests.post(url, json=payload)

def run_polling():
    """نظام الاستطلاع المباشر (Polling) بدون أي ويب هوك"""
    print("Bot started polling successfully...")
    offset = 0
    while True:
        try:
            url = f"https://api.telegram.org/bot{TOKEN}/getUpdates?offset={offset}&timeout=30"
            response = requests.get(url, timeout=35).json()
            
            if "result" in response:
                for update in response["result"]:
                    offset = update["update_id"] + 1
                    if "message" in update and "text" in update["message"]:
                        user_text = update["message"]["text"].strip()
                        chat_id = update["message"]["chat"]["id"]
                        
                        if user_text.startswith("/start"):
                            welcome_msg = (
                                "👋 **أهلاً بك في بوت تحليل صفقات الأوبشن الاستراتيجي!**\n\n"
                                "أرسل رمز السهم (مثل: `TSLA`, `NVDA`, `SPY`) لتحصل على تحليل استراتيجي متكامل يدعم قرارك (CALL / PUT) مع الأهداف والسترايك بدقة. 🚀"
                            )
                            send_telegram_message(welcome_msg, chat_id)
                        elif not user_text.startswith("/"):
                            reply = generate_confirmed_signal(user_text)
                            send_telegram_message(reply, chat_id)
        except Exception as e:
            print(f"Polling error: {e}")
            time.sleep(3)

if __name__ == "__main__":
    run_polling()
