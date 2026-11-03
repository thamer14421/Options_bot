import threading # أضف هذه المكتبة في أعلى الملف إذا لم تكن موجودة

@app.route(f'/{TOKEN}', methods=['POST'])
def webhook():
    """استقبال الرسائل ومعالجتها بمسارات متعددة لضمان عدم ضياع أي سهم"""
    json_data = request.get_json()
    if json_data and "message" in json_data:
        message = json_data["message"]
        if "text" in message:
            user_text = message["text"].strip()
            user_chat_id = message["chat"]["id"]
            
            if not user_text.startswith("/"):
                # تشغيل المعالجة في مسار مستقل لكي لا تتعارض الرسائل
                def process_request():
                    reply = generate_confirmed_signal(user_text)
                    send_telegram_message(reply, target_chat_id=user_chat_id)
                
                threading.Thread(target=process_request).start()
                
    return "OK", 200
