import os
import requests
from datetime import datetime, timedelta
from flask import Flask, request

app = Flask(__name__)

TOKEN = "9192415351:AAGpYxc-tm58spY0ZsAKetX8SkTZ7jVwdik"
CHAT_ID = "913401505"

def get_stock_data_and_analysis(ticker):
    """جلب بيانات السهم وتحليله فنياً عبر مؤشرات حقيقية (RSI & Moving Averages)"""
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
        
        if current_price > sma_20 and rsi < 65:
            signal_type = "CALL"
            sentiment = "تأكيد إيجابي (صاعد) 📈"
            reason = f"السهم يتداول فوق المتوسط و RSI دافئ ({rsi})"
        elif current_price < sma_20 and rsi > 35:
            signal_type = "PUT"
            sentiment = "تأكيد سلبي (هابط) 📉"
            reason = f"السهم يتداول تحت المتوسط و RSI مرتفع ({rsi})"
        else:
            if current_price >= sma_20:
                signal_type = "CALL"
                sentiment = "اتجاه صاعد مع حذر 📊"
                reason = f"مستويات RSI عند ({rsi})"
            else:
                signal_type = "PUT"
                sentiment = "اتجاه هابط مع حذر 📊"
                reason = f"مستويات RSI عند ({rsi})"
                
        return {
            "price": current_price,
            "signal": signal_type,
            "sentiment": sentiment,
            "reason": reason,
            "rsi": rsi
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
    else:
        emoji = "🔴 **شراء هبوط (PUT)**"
        strike_price = round(price * 0.99, 1)
        target_1 = round(price * 0.985, 2)
        target_2 = round(price * 0.97, 2)
        stop_loss = round(price * 1.01, 2)

    message = (
        f"🎯 **تأكيد تحليل فني لسهم ({ticker})**\n"
        f"━━━━━━━━━━━━━━━━━━━\n"
        f"💰 **السعر اللحظي:** `${price}`\n"
        f"📊 **مؤشر RSI:** `{analysis['rsi']}`\n"
        f"🔍 **التأكيد الفني:** {analysis['sentiment']}\n"
        f"💡 **سبب الإشارة:** {analysis['reason']}\n"
        f"━━━━━━━━━━━━━━━━━━━\n"
        f"🚀 **توصية العقد التأكيدية:** {emoji}\n"
        f"📌 **السترايك المناسب:** `${strike_price}`\n"
        f"📅 **تاريخ الانتهاء الأكيد:** `{expiry_date}`\n"
        f"━━━━━━━━━━━━━━━━━━━\n"
        f"🎯 **الهدف الأول:** `${target_1}`\n"
        f"🚀 **الهدف الثاني:** `${target_2}`\n"
        f"🛡️ **وقف الخسارة:** `${stop_loss}`\n"
        f"━━━━━━━━━━━━━━━━━━━\n"
        f"⚡ *تحليل فني مؤكد بناءً على المؤشرات اللحظية!*"
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

@app.route(f'/{TOKEN}', methods=['POST'])
def webhook():
    """استقبال الرسائل فوراً عبر نظام Webhook من تليجرام"""
    json_data = request.get_json()
    if json_data and "message" in json_data:
        message = json_data["message"]
        if "text" in message:
            user_text = message["text"].strip()
            user_chat_id = message["chat"]["id"]
            
            if not user_text.startswith("/"):
                reply = generate_confirmed_signal(user_text)
                send_telegram_message(reply, target_chat_id=user_chat_id)
    return "OK", 200

@app.route('/')
def home():
    return "Webhook Options Bot is Active & Running 24/7!"

if __name__ == "__main__":
    port = int(os.environ.get("PORT", 10000))
    app.run(host="0.0.0.0", port=port)
