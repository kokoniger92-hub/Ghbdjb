import os
import time
import json
import logging
import threading
from datetime import datetime
from flask import Flask, jsonify
import requests

# ========== КОНФИГ ==========
BOT_TOKEN = os.getenv("BOT_TOKEN", "8897748741:AAG2f8sHicGX_wxGEBPm2gqgbULhkGp4weE")
ADMIN_CHAT_ID = int(os.getenv("ADMIN_CHAT_ID", "5979001063"))
PORT = int(os.getenv("PORT", 10000))

# Telegram API URL
API_URL = f"https://api.telegram.org/bot{BOT_TOKEN}"
# ===========================

logging.basicConfig(level=logging.INFO)
logger = logging.getLogger(__name__)

# Flask
app = Flask(__name__)

# Хранилище пользователей
users = {}

def send_message(chat_id, text, reply_markup=None):
    """Отправка сообщения"""
    url = f"{API_URL}/sendMessage"
    data = {
        "chat_id": chat_id,
        "text": text,
        "parse_mode": "Markdown"
    }
    if reply_markup:
        data["reply_markup"] = json.dumps(reply_markup)
    
    try:
        requests.post(url, json=data, timeout=10)
    except Exception as e:
        logger.error(f"Ошибка: {e}")

def send_typing(chat_id):
    """Показывает что бот печатает"""
    try:
        requests.post(f"{API_URL}/sendChatAction", json={"chat_id": chat_id, "action": "typing"}, timeout=5)
    except:
        pass

def check_sim_cards(price_from, price_to):
    """Поиск сим-карт (демо-режим)"""
    # В реальности здесь будет парсинг
    # Сейчас возвращаем тестовые данные
    return [
        {
            "operator": "МТС",
            "price": price_from + 48,
            "balance": 400,
            "url": "https://www.ozon.ru/product/123",
            "platform": "Ozon"
        },
        {
            "operator": "Tele2", 
            "price": price_from + 52,
            "balance": 400,
            "url": "https://www.wildberries.ru/product/456",
            "platform": "WB"
        }
    ]

def handle_message(msg):
    """Обработка сообщений"""
    chat_id = msg.get("chat", {}).get("id")
    text = msg.get("text", "")
    
    if not chat_id:
        return
    
    # Инициализация пользователя
    if chat_id not in users:
        users[chat_id] = {"min": 1, "max": 60}
    
    # Команды
    if text == "/start":
        keyboard = {
            "keyboard": [
                [{"text": "🔍 Проверить"}, {"text": "⚙️ Настройки"}],
                [{"text": "📊 Статистика"}, {"text": "❓ Помощь"}]
            ],
            "resize_keyboard": True
        }
        send_message(
            chat_id,
            f"🤖 *Бот для поиска сим-карт*\n\n"
            f"💎 Ищем сим-карты с балансом *400₽*\n"
            f"💰 Твой диапазон: *{users[chat_id]['min']} - {users[chat_id]['max']} ₽*\n\n"
            f"🔍 Нажми *Проверить* для поиска\n"
            f"⚙️ Нажми *Настройки* для изменения цен",
            reply_markup=keyboard
        )
        logger.info(f"Start: {chat_id}")
    
    elif text == "🔍 Проверить" or text == "/check":
        send_typing(chat_id)
        time.sleep(1)
        
        send_message(chat_id, "🔍 *Ищу сим-карты...*\n\nЭто может занять несколько секунд", parse_mode="Markdown")
        time.sleep(2)
        
        cards = check_sim_cards(users[chat_id]["min"], users[chat_id]["max"])
        
        if cards:
            for card in cards:
                profit = card["balance"] - card["price"]
                msg_text = (
                    f"🎉 *НАЙДЕНА СИМ-КАРТА!*\n\n"
                    f"📱 Оператор: *{card['operator']}*\n"
                    f"💰 Цена: *{card['price']} ₽*\n"
                    f"💎 Баланс: *{card['balance']} ₽*\n"
                    f"📈 Твоя выгода: *{profit} ₽*\n"
                    f"🛍 Площадка: {card['platform']}\n\n"
                    f"🔗 [Купить]({card['url']})"
                )
                send_message(chat_id, msg_text)
                time.sleep(0.5)
        else:
            send_message(chat_id, "😔 *Ничего не найдено*\n\nПопробуй расширить диапазон цен в настройках", parse_mode="Markdown")
    
    elif text == "⚙️ Настройки" or text == "/settings":
        keyboard = {
            "inline_keyboard": [
                [{"text": "💰 Мин. цена", "callback_data": "set_min"}],
                [{"text": "💰 Макс. цена", "callback_data": "set_max"}],
                [{"text": "📊 Показать настройки", "callback_data": "show_settings"}]
            ]
        }
        send_message(
            chat_id,
            f"⚙️ *Настройки*\n\n"
            f"💰 Минимальная цена: *{users[chat_id]['min']} ₽*\n"
            f"💰 Максимальная цена: *{users[chat_id]['max']} ₽*\n\n"
            f"Выбери что изменить:",
            reply_markup=keyboard
        )
    
    elif text == "📊 Статистика" or text == "/stats":
        send_message(
            chat_id,
            f"📊 *Статистика*\n\n"
            f"👤 Твой ID: `{chat_id}`\n"
            f"💰 Диапазон: {users[chat_id]['min']} - {users[chat_id]['max']} ₽\n"
            f"💎 Баланс: 400 ₽\n\n"
            f"📈 Макс. выгода: *{400 - users[chat_id]['min']} ₽*",
            parse_mode="Markdown"
        )
    
    elif text == "❓ Помощь" or text == "/help":
        send_message(
            chat_id,
            f"📖 *Помощь*\n\n"
            f"🔍 *Проверить* - поиск сим-карт\n"
            f"⚙️ *Настройки* - изменить диапазон цен\n"
            f"📊 *Статистика* - твои настройки\n"
            f"❓ *Помощь* - это сообщение\n\n"
            f"💡 Бот ищет сим-карты с балансом 400₽\n"
            f"💰 Чем ниже цена, тем больше выгода!"
        )
    
    elif text.isdigit():
        # Ввод чисел для настройки
        if chat_id in users and "waiting" in users[chat_id]:
            val = int(text)
            if users[chat_id]["waiting"] == "min":
                users[chat_id]["min"] = val
                if users[chat_id]["min"] > users[chat_id]["max"]:
                    users[chat_id]["max"] = val + 10
                send_message(chat_id, f"✅ *Мин. цена: {val} ₽*\n\nТеперь введи *максимальную* цену:", parse_mode="Markdown")
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
            send_message(chat_id, "❓ Сначала выбери что настроить в меню *Настройки*", parse_mode="Markdown")
    
    else:
        send_message(chat_id, f"❓ *Неизвестная команда*\n\nИспользуй кнопки или /help", parse_mode="Markdown")

def handle_callback(cb):
    """Обработка нажатий кнопок"""
    chat_id = cb.get("message", {}).get("chat", {}).get("id")
    data = cb.get("data", "")
    cb_id = cb.get("id")
    
    if not chat_id:
        return
    
    if chat_id not in users:
        users[chat_id] = {"min": 1, "max": 60}
    
    if data == "set_min":
        users[chat_id]["waiting"] = "min"
        send_message(chat_id, "✏️ Введи *минимальную* цену (число):", parse_mode="Markdown")
    
    elif data == "set_max":
        users[chat_id]["waiting"] = "max"
        send_message(chat_id, "✏️ Введи *максимальную* цену (число):", parse_mode="Markdown")
    
    elif data == "show_settings":
        send_message(
            chat_id,
            f"📊 *Твои настройки*\n\n"
            f"💰 Мин. цена: *{users[chat_id]['min']} ₽*\n"
            f"💰 Макс. цена: *{users[chat_id]['max']} ₽*\n"
            f"💎 Баланс: *400 ₽*",
            parse_mode="Markdown"
        )
    
    # Ответ на callback
    try:
        requests.post(f"{API_URL}/answerCallbackQuery", json={"callback_query_id": cb_id}, timeout=5)
    except:
        pass

def poll_updates():
    """Основной цикл получения обновлений"""
    last_id = 0
    
    while True:
        try:
            url = f"{API_URL}/getUpdates"
            params = {"timeout": 30, "offset": last_id + 1 if last_id else None}
            
            resp = requests.get(url, params=params, timeout=35)
            data = resp.json()
            
            if data.get("ok"):
                for update in data.get("result", []):
                    last_id = update.get("update_id", last_id)
                    
                    if "message" in update:
                        handle_message(update["message"])
                    elif "callback_query" in update:
                        handle_callback(update["callback_query"])
            
            time.sleep(0.5)
        except Exception as e:
            logger.error(f"Poll ошибка: {e}")
            time.sleep(5)

# ========== FLASK ==========
@app.route('/')
def index():
    return jsonify({
        "status": "ok",
        "bot": "running",
        "users": len(users),
        "time": datetime.now().isoformat()
    })

@app.route('/health')
def health():
    return jsonify({"status": "alive"})

# ========== ЗАПУСК ==========
if __name__ == "__main__":
    # Запускаем polling в потоке
    poll_thread = threading.Thread(target=poll_updates, daemon=True)
    poll_thread.start()
    
    logger.info(f"🚀 Бот запущен!")
    logger.info(f"🤖 Токен: {BOT_TOKEN[:20]}...")
    logger.info(f"🌐 Порт: {PORT}")
    
    # Запускаем Flask
    app.run(host='0.0.0.0', port=PORT, debug=False, use_reloader=False)
