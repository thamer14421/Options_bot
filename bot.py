import os
import requests
from datetime import datetime, timedelta
from flask import Flask, request

app = Flask(__name__)

TOKEN = "9192415351:AAGpYxc-tm58spY0ZsAKetX8SkTZ7jVwdik"

def get_stock_data_and_analysis(ticker):
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
            
        sma_20 = sum(prices[-20:]) / min(len(prices), 20)
        distance_from_sma = round(((current_price - sma_20) / sma_20) * 100, 2)
        
        if current_price > sma_20 and rsi < 65:
            signal_type = "CALL"
            sentiment = "تأكيد إيجابي قوي (صاعد) 📈"
            reason = f"السهم يتداول فوق المتوسط بـ ({distance_from_sma}%) مع استقرار RSI عند ({rsi})."
        else:
            signal_type = "PUT"
            sentiment = "تأكيد سلبي قوي (هابط) 📉"
            reason = f"السهم يتداول تحت المتوسط بـ ({abs(distance_from_sma)}%) مع بقاء RSI عند ({rsi})."
                
        return {
            "price": current_price, "signal": signal_type,
            "sentiment": sentiment, "reason": reason,
            "rsi": rsi, "sma": round(sma_20, 2)
        }
    except Exception as e:
        print(f"Error: {e}")
        return None

def get_next_valid_expiry():
    today = datetime.now()
    days_ahead = 4 - today.weekday()
    if days_ahead <= 0: days_ahead += 7
    return (today + timedelta(days=days_ahead)).strftime('%Y-%m-%d')

def generate_confirmed_signal(ticker_name):
    ticker = ticker_name.upper().strip()
    analysis = get_stock_data_and_analysis(ticker)
    
    if not analysis:
        return f"⚠️ **تنبيه:** تعذر جلب البيانات للسهم `{ticker}`. تأكد من الرمز."

    price = analysis["price"]
    signal_type = analysis["signal"]
    expiry_date = get_next_valid_expiry()

    if signal_type == "CALL":
        emoji = "🟢 **شراء صعود (CALL)**"
        strike = round(price * 1.01, 1)
        t1 = round(price * 1.015, 2)
        t2 = round(price * 1.03, 2)
        sl = round(price * 0.99, 2)
        note = "📌 **الاستراتيجية:** الزخم الشرائي يدعم صعود السهم واختراق الأهداف."
    else:
        emoji = "🔴 **شراء هبوط (PUT)**"
        strike = round(price * 0.99, 1)
        t1 = round(price * 0.985, 2)
        t2 = round(price * 0.97, 2)
        sl = round(price * 1.01, 2)
        note = "📌 **الاستراتيجية:** الضغط البيعي واستمرار التداول تحت المتوسط يرجح الهبوط."

    return (
        f"🎯 **تحليل صفقات الأوبشن ({ticker})**\n"
        f"━━━━━━━━━━━━━━━━━━━\n"
        f"💰 **السعر:** `${price}` | 📉 **SMA20:** `${analysis['sma']}`\n"
        f"📊 **RSI:** `{analysis['rsi']}`\n"
        f"🔍 **الاتجاه:** {analysis['sentiment']}\n"
        f"💡 **المعيار:** {analysis['reason']}\n"
        f"━━━━━━━━━━━━━━━━━━━\n"
        f"🚀 **التوصية:** {emoji}\n"
        f"{note}\n"
        f"📌 **السترايك:** `${strike}` | 📅 **الانتهاء:** `{expiry_date}`\n"
        f"━━━━━━━━━━━━━━━━━━━\n"
        f"🎯 **الهدف 1:** `${t1}` | 🚀 **الهدف 2:** `${t2}`\n"
        f"🛡️ **وقف الخسارة:** `${sl}`"
    )

def send_telegram_message(text, chat_id):
    url = f"https://api.telegram.org/bot{TOKEN}/sendMessage"
    requests.post(url, json={"chat_id": chat_id, "text": text, "parse_mode": "Markdown"})

# مسار عام يستقبل أي رسالة بدون تعقيد الشروط
@app.route('/', methods=['POST'])
def webhook():
    try:
        json_data = request.get_json(force=True)
        if "message" in json_data and "text" in json_data["message"]:
            text = json_data["message"]["text"].strip()
            chat_id = json_data["message"]["chat"]["id"]
            
            if text.startswith("/start"):
                send_telegram_message("👋 أهلاً بك! أرسل رمز السهم (مثل TSLA) لتحليل الأوبشن.", chat_id)
            elif not text.startswith("/"):
                reply = generate_confirmed_signal(text)
                send_telegram_message(reply, chat_id)
    except Exception as e:
        print(f"Webhook Error: {e}")
    return "OK", 200

@app.route('/status', methods=['GET'])
def home():
    return "Bot is running perfectly!"

if __name__ == "__main__":
    port = int(os.environ.get("PORT", 10000))
    app.run(host="0.0.0.0", port=port)
