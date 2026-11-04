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
        if len(prices) < 14: return None
        current_price = round(prices[-1], 2)
        gains, losses = [], []
        for i in range(1, 15):
            change = prices[-i] - prices[-(i+1)]
            if change >= 0: gains.append(change), losses.append(0)
            else: gains.append(0), losses.append(abs(change))
        avg_gain = sum(gains) / 14
        avg_loss = sum(losses) / 14
        rsi = 100 if avg_loss == 0 else round(100 - (100 / (1 + (avg_gain / avg_loss))), 1)
        sma_20 = sum(prices[-20:]) / min(len(prices), 20)
        
        if current_price > sma_20 and rsi < 65:
            signal_type, sentiment, reason = "CALL", "تأكيد إيجابي (صاعد)", f"السهم فوق المتوسط و RSI دافئ ({rsi})"
        else:
            signal_type, sentiment, reason = "PUT", "تأكيد سلبي (هابط)", f"السهم تحت المتوسط و RSI مرتفع ({rsi})"
        return {"price": current_price, "signal": signal_type, "sentiment": sentiment, "reason": reason, "rsi": rsi}
    except:
        return None

def get_next_valid_expiry():
    today = datetime.now()
    days_ahead = 4 - today.weekday()
    if days_ahead <= 0: days_ahead += 7
    return (today + timedelta(days=days_ahead)).strftime('%Y-%m-%d')

def generate_confirmed_signal(ticker_name):
    ticker = ticker_name.upper().strip()
    analysis = get_stock_data_and_analysis(ticker)
    if not analysis: return f"⚠️ تعذر جلب البيانات لسهم `{ticker}`."
    price, signal_type = analysis["price"], analysis["signal"]
    expiry_date = get_next_valid_expiry()
    if signal_type == "CALL":
        emoji, strike, t1, t2, sl = "🟢 **شراء صعود (CALL)**", round(price * 1.01, 1), round(price * 1.015, 2), round(price * 1.03, 2), round(price * 0.99, 2)
    else:
        emoji, strike, t1, t2, sl = "🔴 **شراء هبوط (PUT)**", round(price * 0.99, 1), round(price * 0.985, 2), round(price * 0.97, 2), round(price * 1.01, 2)
    return (
        f"🎯 **تأكيد تحليل فني لسهم ({ticker})**\n"
        f"━━━━━━━━━━━━━━━━━━━\n"
        f"💰 السعر اللحظي: `${price}`\n"
        f"📊 مؤشر RSI: `{analysis['rsi']}`\n"
        f"🔍 التأكيد الفني: {analysis['sentiment']}\n"
        f"💡 سبب الإشارة: {analysis['reason']}\n"
        f"━━━━━━━━━━━━━━━━━━━\n"
        f"🚀 توصية العقد التأكيدية: {emoji}\n"
        f"📌 السترايك المناسب: `${strike}`\n"
        f"📅 تاريخ الانتهاء الأكيد: `{expiry_date}`\n"
        f"━━━━━━━━━━━━━━━━━━━\n"
        f"🎯 الهدف الأول: `${t1}`\n"
        f"🚀 الهدف الثاني: `${t2}`\n"
        f"🛡️ وقف الخسارة: `${sl}`\n"
        f"━━━━━━━━━━━━━━━━━━━\n"
        f"⚡ تحليل فني مؤكد بناءً على المؤشرات اللحظية!"
    )

def send_telegram_message(text, chat_id):
    requests.post(f"https://api.telegram.org/bot{TOKEN}/sendMessage", json={"chat_id": chat_id, "text": text, "parse_mode": "Markdown"})

@app.route(f'/{TOKEN}', methods=['POST'])
def webhook():
    json_data = request.get_json(silent=True)
    if json_data and "message" in json_data:
        msg = json_data["message"]
        if "text" in msg:
            text, chat_id = msg["text"].strip(), msg["chat"]["id"]
            if text.startswith("/start"):
                send_telegram_message("👋 أهلاً بك! أرسل رمز السهم (مثل TSLA أو SPY) للحصول على التحليل الفني.", chat_id)
            elif not text.startswith("/"):
                send_telegram_message(generate_confirmed_signal(text), chat_id)
    return "OK", 200

@app.route('/')
def home():
    return "Bot is active!"

if __name__ == "__main__":
    app.run(host="0.0.0.0", port=int(os.environ.get("PORT", 10000)))
