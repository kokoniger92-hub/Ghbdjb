import os
import json
import logging
import requests
from datetime import datetime
from flask import Flask, request, jsonify

# ========== КОНФИГ ==========
BOT_TOKEN = os.getenv("BOT_TOKEN", "8897748741:AAG2f8sHicGX_wxGEBPm2gqgbULhkGp4weE")
ADMIN_CHAT_ID = int(os.getenv("ADMIN_CHAT_ID", "5979001063"))
PORT = int(os.getenv("PORT", 10000))

# Твой URL на Render (без слеша в конце!)
RENDER_URL = "https://ghbdjb.onrender.com"
WEBHOOK_URL = f"{RENDER_URL}/{BOT_TOKEN}"

API_URL = f"https://api.telegram.org/bot{BOT_TOKEN}"
# ===========================

logging.basicConfig(level=logging.INFO)
logger = logging.getLogger(__name__)

app = Flask(__name__)

# Хранилище пользователей (в реальном боте лучше использовать БД)
users = {}

def send_message(chat_id, text, reply_markup=None):
    """Отправка сообщения в Telegram"""
    url = f"{API_URL}/sendMessage"
    payload = {
        "chat_id": chat_id,
        "text": text,
        "parse_mode": "Markdown"
    }
    if reply_markup:
        payload["reply_markup"] = json.dumps(reply_markup)
    
    try:
        r = requests.post(url, json=payload, timeout=10)
        logger.info(f"Сообщение отправлено в {chat_id}: {r.status_code}")
        return r.json()
    except Exception as e:
        logger.error(f"Ошибка отправки: {e}")
        return None

def set_webhook():
    """Принудительная установка вебхука"""
    url = f"{API_URL}/setWebhook"
    payload = {"url": WEBHOOK_URL}
    
    try:
        r = requests.post(url, json=payload, timeout=10)
        result = r.json()
        if result.get("ok"):
            logger.info(f"✅ Вебхук установлен: {WEBHOOK_URL}")
        else:
            logger.error(f"❌ Ошибка вебхука: {result}")
        return result
    except Exception as e:
        logger.error(f"❌ Ошибка запроса: {e}")
        return None

def delete_webhook():
    """Удаление старого вебхука (на всякий случай)"""
    url = f"{API_URL}/deleteWebhook"
    try:
        r = requests.post(url, timeout=10)
        logger.info(f"Старый вебхук удалён: {r.json()}")
    except:
        pass

def get_webhook_info():
    """Проверка статуса вебхука"""
    url = f"{API_URL}/getWebhookInfo"
    try:
        r = requests.get(url, timeout=10)
        info = r.json()
        logger.info(f"Инфо о вебхуке: {info}")
        return info
    except:
        return None

def check_sim_cards(price_min, price_max):
    """Поиск сим-карт (демо-режим)"""
    # Здесь ты потом добавишь реальный парсинг Ozon/WB
    return [
        {
            "operator": "МТС",
            "price": price_min + 45,
            "balance": 400,
            "url": "https://www.ozon.ru/product/sim-karta-mts-400-123456789/",
            "platform": "Ozon",
            "profit": 400 - (price_min + 45)
        },
        {
            "operator": "Tele2",
            "price": price_min + 52,
            "balance": 400,
            "url": "https://www.wildberries.ru/catalog/sim-karta-tele2-987654321/",
            "platform": "Wildberries",
            "profit": 400 - (price_min + 52)
        }
    ]

@app.route(f'/{BOT_TOKEN}', methods=['POST'])
def webhook():
    """Главный вход для всех обновлений от Telegram"""
    try:
        update = request.get_json()
        if not update:
            logger.warning("Пустой запрос")
            return "OK", 200
        
        logger.info(f"Получено обновление: {update.get('update_id')}")
        
        # --- Обработка сообщений ---
        if "message" in update:
            msg = update["message"]
            chat_id = msg.get("chat", {}).get("id")
            text = msg.get("text", "")
            
            if not chat_id:
                return "OK", 200
            
            # Инициализация пользователя
            if chat_id not in users:
                users[chat_id] = {"min": 1, "max": 60}
                logger.info(f"Новый пользователь: {chat_id}")
            
            # Команда /start
            if text == "/start":
                keyboard = {
                    "keyboard": [
                        [{"text": "🔍 Проверить"}],
                        [{"text": "⚙️ Настройки"}],
                        [{"text": "❓ Помощь"}]
                    ],
                    "resize_keyboard": True
                }
                send_message(
                    chat_id,
                    f"🤖 *Бот для поиска сим-карт*\n\n"
                    f"💎 Ищу сим-карты с балансом *400₽*\n"
                    f"💰 Твой диапазон: *{users[chat_id]['min']} - {users[chat_id]['max']} ₽*\n\n"
                    f"🔍 Нажми *Проверить* для поиска\n"
                    f"⚙️ *Настройки* — изменить цены",
                    reply_markup=keyboard
                )
                logger.info(f"Start для {chat_id}")
            
            # Проверить
            elif text == "🔍 Проверить" or text == "/check":
                send_message(chat_id, "🔍 *Ищу сим-карты...*\nЭто может занять пару секунд", parse_mode="Markdown")
                
                cards = check_sim_cards(users[chat_id]["min"], users[chat_id]["max"])
                
                if cards:
                    for card in cards:
                        msg_text = (
                            f"🎉 *НАЙДЕНА СИМ-КАРТА!*\n\n"
                            f"📱 Оператор: *{card['operator']}*\n"
                            f"💰 Цена: *{card['price']} ₽*\n"
                            f"💎 Баланс: *{card['balance']} ₽*\n"
                            f"📈 Твоя выгода: *{card['profit']} ₽*\n"
                            f"🛍 Платформа: {card['platform']}\n\n"
                            f"🔗 [Купить]({card['url']})"
                        )
                        send_message(chat_id, msg_text)
                else:
                    send_message(chat_id, "😔 *Ничего не найдено*\nПопробуй расширить диапазон в настройках", parse_mode="Markdown")
            
            # Настройки
            elif text == "⚙️ Настройки" or text == "/settings":
                keyboard = {
                    "inline_keyboard": [
                        [{"text": "💰 Мин. цена", "callback_data": "set_min"}],
                        [{"text": "💰 Макс. цена", "callback_data": "set_max"}],
                        [{"text": "📊 Мои настройки", "callback_data": "show"}]
                    ]
                }
                send_message(
                    chat_id,
                    f"⚙️ *Настройки*\n\n"
                    f"💰 Текущий диапазон: *{users[chat_id]['min']} - {users[chat_id]['max']} ₽*\n\n"
                    f"Выбери, что хочешь изменить:",
                    reply_markup=keyboard
                )
            
            # Помощь
            elif text == "❓ Помощь" or text == "/help":
                send_message(
                    chat_id,
                    f"📖 *Команды бота*\n\n"
                    f"🔍 *Проверить* — найти сим-карты\n"
                    f"⚙️ *Настройки* — изменить диапазон цен\n"
                    f"❓ *Помощь* — это сообщение\n\n"
                    f"💡 Бот ищет сим-карты с балансом 400₽\n"
                    f"💰 Чем ниже цена, тем больше выгода!"
                )
            
            # Ввод чисел
            elif text.isdigit():
                val = int(text)
                if "waiting" in users[chat_id]:
                    if users[chat_id]["waiting"] == "min":
                        users[chat_id]["min"] = val
                        if users[chat_id]["min"] > users[chat_id]["max"]:
                            users[chat_id]["max"] = val + 10
                        send_message(chat_id, f"✅ *Мин. цена: {val} ₽*\nТеперь введи *максимальную* цену:", parse_mode="Markdown")
                        users[chat_id]["waiting"] = "max"
                    elif users[chat_id]["waiting"] == "max":
                        users[chat_id]["max"] = val
                        if users[chat_id]["min"] > users[chat_id]["max"]:
                            users[chat_id]["min"] = val - 10
                        del users[chat_id]["waiting"]
                        send_message(
                            chat_id,
                            f"✅ *Диапазон сохранён!*\n\n"
                            f"💰 {users[chat_id]['min']} - {users[chat_id]['max']} ₽\n\n"
                            f"🔍 Нажми *Проверить* для поиска",
                            parse_mode="Markdown"
                        )
                else:
                    send_message(chat_id, "❓ Сначала выбери, что настроить: *Настройки* → *Мин. цена* или *Макс. цена*", parse_mode="Markdown")
            
            else:
                send_message(chat_id, f"❓ *Неизвестная команда*\n\nИспользуй кнопки или /help", parse_mode="Markdown")
        
        # --- Обработка нажатий на кнопки ---
        elif "callback_query" in update:
            cb = update["callback_query"]
            chat_id = cb.get("message", {}).get("chat", {}).get("id")
            data = cb.get("data", "")
            cb_id = cb.get("id")
            
            if not chat_id:
                return "OK", 200
            
            if chat_id not in users:
                users[chat_id] = {"min": 1, "max": 60}
            
            if data == "set_min":
                users[chat_id]["waiting"] = "min"
                send_message(chat_id, "✏️ Введи *минимальную* цену (число):", parse_mode="Markdown")
            
            elif data == "set_max":
                users[chat_id]["waiting"] = "max"
                send_message(chat_id, "✏️ Введи *максимальную* цену (число):", parse_mode="Markdown")
            
            elif data == "show":
                send_message(
                    chat_id,
                    f"📊 *Твои настройки*\n\n"
                    f"💰 Диапазон: *{users[chat_id]['min']} - {users[chat_id]['max']} ₽*\n"
                    f"💎 Баланс: *400 ₽*"
                )
            
            # Отвечаем на callback (чтобы убрать часики на кнопке)
            try:
                requests.post(f"{API_URL}/answerCallbackQuery", json={"callback_query_id": cb_id}, timeout=5)
            except:
                pass
        
        return "OK", 200
    
    except Exception as e:
        logger.error(f"Ошибка в webhook: {e}")
        return "OK", 200

@app.route('/')
def index():
    info = get_webhook_info()
    return jsonify({
        "status": "ok",
        "bot": "running",
        "users": len(users),
        "webhook_set": WEBHOOK_URL,
        "webhook_info": info
    })

@app.route('/health')
def health():
    return jsonify({"status": "alive"})

@app.route('/set_webhook')
def manual_set_webhook():
    """Ручная установка вебхука (на случай проблем)"""
    delete_webhook()
    result = set_webhook()
    return jsonify(result)

if __name__ == "__main__":
    # Сначала удаляем старый вебхук, потом устанавливаем новый
    delete_webhook()
    set_webhook()
    
    logger.info(f"🚀 Бот запущен!")
    logger.info(f"🔗 Вебхук: {WEBHOOK_URL}")
    logger.info(f"📡 Статус: {get_webhook_info()}")
    
    app.run(host='0.0.0.0', port=PORT, debug=False)
