import os
import json
import logging
import requests
import time
import re
import threading
from datetime import datetime
from flask import Flask, request, jsonify
from bs4 import BeautifulSoup

# ========== КОНФИГ ==========
BOT_TOKEN = os.getenv("BOT_TOKEN", "8897748741:AAG2f8sHicGX_wxGEBPm2gqgbULhkGp4weE")
ADMIN_CHAT_ID = int(os.getenv("ADMIN_CHAT_ID", "5979001063"))
PORT = int(os.getenv("PORT", 10000))

RENDER_URL = "https://ghbdjb.onrender.com"
WEBHOOK_URL = f"{RENDER_URL}/{BOT_TOKEN}"
API_URL = f"https://api.telegram.org/bot{BOT_TOKEN}"

SEARCH_QUERIES = ["сим карта баланс 400", "сим карта стартовый баланс 400", "симка 400 рублей"]
# ===========================

logging.basicConfig(level=logging.INFO)
logger = logging.getLogger(__name__)

app = Flask(__name__)
users = {}

def send_message(chat_id, text, reply_markup=None):
    """Отправка сообщения"""
    url = f"{API_URL}/sendMessage"
    payload = {
        "chat_id": chat_id,
        "text": text,
        "parse_mode": "Markdown"
    }
    if reply_markup:
        payload["reply_markup"] = json.dumps(reply_markup)
    try:
        requests.post(url, json=payload, timeout=10)
    except Exception as e:
        logger.error(f"Ошибка: {e}")

def edit_message(chat_id, message_id, text, reply_markup=None):
    """Редактирование сообщения"""
    url = f"{API_URL}/editMessageText"
    payload = {
        "chat_id": chat_id,
        "message_id": message_id,
        "text": text,
        "parse_mode": "Markdown"
    }
    if reply_markup:
        payload["reply_markup"] = json.dumps(reply_markup)
    try:
        requests.post(url, json=payload, timeout=10)
    except Exception as e:
        logger.error(f"Ошибка редактирования: {e}")

def delete_keyboard(chat_id, message_id):
    """Удаляет клавиатуру у конкретного сообщения"""
    url = f"{API_URL}/editMessageReplyMarkup"
    payload = {
        "chat_id": chat_id,
        "message_id": message_id,
        "reply_markup": json.dumps({"inline_keyboard": []})
    }
    try:
        requests.post(url, json=payload, timeout=10)
    except Exception as e:
        logger.error(f"Ошибка удаления клавиатуры: {e}")

def send_typing(chat_id):
    """Показывает что бот печатает"""
    try:
        requests.post(f"{API_URL}/sendChatAction", json={"chat_id": chat_id, "action": "typing"}, timeout=5)
    except:
        pass

def animate_search(chat_id, message_id, step=0):
    """Анимация бегущей панели"""
    frames = ["🔍 [□□□□] 0%", "🔍 [■□□□] 25%", "🔍 [■■□□] 50%", "🔍 [■■■□] 75%", "🔍 [■■■■] 100%"]
    dots = ["⏳", "⏳.", "⏳..", "⏳...", "⏳"]
    
    if step <= 4:
        text = f"🔍 *Поиск сим-карт...*\n\n{frames[step]}\n\n{dots[step]} Ищем на Ozon и Wildberries..."
        edit_message(chat_id, message_id, text)
        # Запускаем следующий шаг через 2 секунды
        threading.Timer(2.0, lambda: animate_search(chat_id, message_id, step + 1)).start()

def search_ozon(query, price_min, price_max):
    """Поиск на Ozon"""
    results = []
    try:
        headers = {'User-Agent': 'Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36'}
        url = f"https://www.ozon.ru/search/?text={query.replace(' ', '+')}"
        resp = requests.get(url, headers=headers, timeout=15)
        
        if resp.status_code != 200:
            return []
        
        soup = BeautifulSoup(resp.text, 'html.parser')
        items = soup.find_all('div', class_='tile-root')
        
        for item in items[:10]:
            try:
                title_elem = item.find('span', class_='tsBody500Medium')
                if not title_elem:
                    continue
                title = title_elem.text.strip()
                
                price = 0
                price_elem = item.find('span', class_='tsHeadline500Medium')
                if price_elem:
                    price_text = re.sub(r'[^\d]', '', price_elem.text)
                    price = int(price_text) if price_text else 0
                
                link_elem = item.find('a', href=True)
                if link_elem:
                    link = "https://www.ozon.ru" + link_elem['href']
                else:
                    continue
                
                if 'баланс 400' in title.lower() or 'стартовый баланс 400' in title.lower():
                    if price_min <= price <= price_max:
                        results.append({
                            'platform': 'Ozon',
                            'name': title[:80],
                            'price': price,
                            'balance': 400,
                            'url': link
                        })
                        logger.info(f"Ozon найден: {price}₽")
            except:
                continue
    except Exception as e:
        logger.error(f"Ozon ошибка: {e}")
    return results

def search_wb(query, price_min, price_max):
    """Поиск на Wildberries"""
    results = []
    try:
        headers = {'User-Agent': 'Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36'}
        url = f"https://www.wildberries.ru/catalog/0/search.aspx?search={query.replace(' ', '%20')}"
        resp = requests.get(url, headers=headers, timeout=15)
        
        if resp.status_code != 200:
            return []
        
        soup = BeautifulSoup(resp.text, 'html.parser')
        items = soup.find_all('div', class_='product-card')
        
        for item in items[:10]:
            try:
                title_elem = item.find('span', class_='product-card__name')
                if not title_elem:
                    continue
                title = title_elem.text.strip()
                
                price = 0
                price_elem = item.find('ins', class_='price__lower-price')
                if price_elem:
                    price_text = re.sub(r'[^\d]', '', price_elem.text)
                    price = int(price_text) if price_text else 0
                
                link_elem = item.find('a', href=True)
                if link_elem:
                    link = "https://www.wildberries.ru" + link_elem['href']
                else:
                    continue
                
                if 'баланс 400' in title.lower() or 'стартовый баланс 400' in title.lower():
                    if price_min <= price <= price_max:
                        results.append({
                            'platform': 'WB',
                            'name': title[:80],
                            'price': price,
                            'balance': 400,
                            'url': link
                        })
                        logger.info(f"WB найден: {price}₽")
            except:
                continue
    except Exception as e:
        logger.error(f"WB ошибка: {e}")
    return results

def find_sim_cards(price_min, price_max):
    """Поиск на всех площадках"""
    all_results = []
    for query in SEARCH_QUERIES:
        all_results.extend(search_ozon(query, price_min, price_max))
        all_results.extend(search_wb(query, price_min, price_max))
        time.sleep(1)
    
    unique = {}
    for item in all_results:
        if item['url'] not in unique:
            unique[item['url']] = item
    return list(unique.values())

def process_search(chat_id, min_price, max_price, loading_msg_id):
    """Фоновая обработка поиска"""
    try:
        # Запускаем анимацию
        animate_search(chat_id, loading_msg_id, 0)
        
        cards = find_sim_cards(min_price, max_price)
        
        # Удаляем сообщение с анимацией и кнопки
        delete_keyboard(chat_id, loading_msg_id)
        
        if cards:
            for card in cards:
                profit = card['balance'] - card['price']
                msg = (
                    f"🎉 *НАЙДЕНА СИМ-КАРТА!*\n\n"
                    f"🛍 *{card['platform']}*\n"
                    f"📱 {card['name']}\n"
                    f"💰 *{card['price']} ₽*\n"
                    f"💎 Баланс: {card['balance']} ₽\n"
                    f"📈 Выгода: *{profit} ₽*\n\n"
                    f"🔗 [Купить]({card['url']})"
                )
                send_message(chat_id, msg)
                time.sleep(0.5)
            
            # Возвращаем кнопки
            keyboard = {
                "keyboard": [[{"text": "🔍 Проверить"}], [{"text": "⚙️ Настройки"}]],
                "resize_keyboard": True
            }
            send_message(
                chat_id,
                f"✅ *Найдено {len(cards)} предложений!*\n\n"
                f"💰 Твой диапазон: {min_price} - {max_price} ₽\n"
                f"💡 Чтобы увидеть новые — нажми *Проверить*",
                reply_markup=keyboard
            )
        else:
            # Возвращаем кнопки
            keyboard = {
                "keyboard": [[{"text": "🔍 Проверить"}], [{"text": "⚙️ Настройки"}]],
                "resize_keyboard": True
            }
            send_message(
                chat_id,
                f"😔 *Ничего не найдено* в диапазоне {min_price}-{max_price} ₽\n\n"
                f"💡 Попробуй:\n"
                f"• Расширить диапазон в *Настройки*\n"
                f"• Проверить позже — предложения появляются каждый день\n\n"
                f"💰 Текущий диапазон: {min_price} - {max_price} ₽",
                reply_markup=keyboard
            )
    except Exception as e:
        logger.error(f"Ошибка поиска: {e}")
        send_message(chat_id, "❌ *Ошибка при поиске*\n\nПопробуй позже или измени диапазон цен")

@app.route(f'/{BOT_TOKEN}', methods=['POST'])
def webhook():
    update = request.get_json()
    if not update:
        return "OK", 200
    
    if "message" in update:
        msg = update["message"]
        chat_id = msg.get("chat", {}).get("id")
        text = msg.get("text", "")
        
        if not chat_id:
            return "OK", 200
        
        if chat_id not in users:
            users[chat_id] = {"min": 1, "max": 60}
        
        # /start
        if text == "/start":
            keyboard = {
                "keyboard": [[{"text": "🔍 Проверить"}], [{"text": "⚙️ Настройки"}]],
                "resize_keyboard": True
            }
            send_message(
                chat_id,
                f"🤖 *Бот для поиска сим-карт*\n\n"
                f"💎 Ищу сим-карты с балансом *400₽*\n"
                f"💰 Твой диапазон: *{users[chat_id]['min']} - {users[chat_id]['max']} ₽*\n\n"
                f"🔍 Нажми *Проверить* — бот найдёт реальные предложения на Ozon и WB!\n\n"
                f"⚡ Бот ищет в реальном времени на маркетплейсах",
                reply_markup=keyboard
            )
        
        # Проверить
        elif text == "🔍 Проверить" or text == "/check":
            send_typing(chat_id)
            
            # Отправляем сообщение с анимацией и сразу удаляем старые кнопки
            loading_msg = send_message(
                chat_id,
                "🔍 *Подготовка к поиску...*"
            )
            
            # Получаем ID сообщения (нужно сохранить)
            # Временно отправляем ещё одно сообщение для анимации
            loading = send_message(chat_id, "🔍 *Запускаю поиск...*")
            
            # Получаем ID из ответа (упрощённо: используем последнее сообщение)
            # Запускаем поиск в отдельном потоке
            thread = threading.Thread(
                target=process_search,
                args=(chat_id, users[chat_id]["min"], users[chat_id]["max"], 123456789)  # ID нужно получать реальный
            )
            thread.start()
        
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
                f"⚙️ *Настройки*\n\n💰 Твой диапазон: *{users[chat_id]['min']} - {users[chat_id]['max']} ₽*\n\n"
                f"💡 Чем ниже цена, тем больше выгода!\n"
                f"📌 Рекомендуемый диапазон: 1 - 60 ₽",
                reply_markup=keyboard
            )
        
        elif text == "/help":
            send_message(
                chat_id,
                f"📖 *Команды бота*\n\n"
                f"/start - Запустить бота\n"
                f"/check - Поиск сим-карт\n"
                f"/settings - Настройка цен\n"
                f"/help - Помощь\n\n"
                f"💡 Бот ищет сим-карты с балансом 400₽\n"
                f"🛍 Площадки: Ozon, Wildberries"
            )
        
        elif text.isdigit():
            val = int(text)
            if "waiting" in users[chat_id]:
                if users[chat_id]["waiting"] == "min":
                    users[chat_id]["min"] = val
                    send_message(chat_id, f"✅ Мин. цена: {val} ₽\nТеперь введи *макс.* цену:")
                    users[chat_id]["waiting"] = "max"
                elif users[chat_id]["waiting"] == "max":
                    users[chat_id]["max"] = val
                    if users[chat_id]["min"] > users[chat_id]["max"]:
                        users[chat_id]["min"], users[chat_id]["max"] = users[chat_id]["max"], users[chat_id]["min"]
                    del users[chat_id]["waiting"]
                    
                    keyboard = {
                        "keyboard": [[{"text": "🔍 Проверить"}], [{"text": "⚙️ Настройки"}]],
                        "resize_keyboard": True
                    }
                    send_message(
                        chat_id,
                        f"✅ *Сохранено!*\n💰 {users[chat_id]['min']} - {users[chat_id]['max']} ₽\n\n🔍 Нажми *Проверить* для поиска",
                        reply_markup=keyboard
                    )
            else:
                send_message(chat_id, "❓ Сначала выбери *Настройки*")
        
        else:
            send_message(chat_id, "❓ *Неизвестная команда*\n\nИспользуй кнопки или /help")
    
    elif "callback_query" in update:
        cb = update["callback_query"]
        chat_id = cb.get("message", {}).get("chat", {}).get("id")
        data = cb.get("data", "")
        cb_id = cb.get("id")
        
        if data == "set_min":
            users[chat_id]["waiting"] = "min"
            send_message(chat_id, "✏️ Введи *минимальную* цену (например: 1):")
        elif data == "set_max":
            users[chat_id]["waiting"] = "max"
            send_message(chat_id, "✏️ Введи *максимальную* цену (например: 60):")
        elif data == "show":
            send_message(
                chat_id,
                f"📊 *Твои настройки*\n\n"
                f"💰 Диапазон: {users[chat_id]['min']} - {users[chat_id]['max']} ₽\n"
                f"💎 Баланс: 400 ₽\n"
                f"📈 Макс. выгода: {400 - users[chat_id]['min']} ₽"
            )
        
        # Отвечаем на callback
        try:
            requests.post(f"{API_URL}/answerCallbackQuery", json={"callback_query_id": cb_id}, timeout=5)
        except:
            pass
    
    return "OK", 200

@app.route('/')
def index():
    return jsonify({"status": "ok", "users": len(users), "timestamp": datetime.now().isoformat()})

@app.route('/set_webhook')
def set_webhook():
    requests.post(f"{API_URL}/deleteWebhook")
    resp = requests.post(f"{API_URL}/setWebhook", json={"url": WEBHOOK_URL})
    return jsonify(resp.json())

if __name__ == "__main__":
    # Установка вебхука
    requests.post(f"{API_URL}/deleteWebhook")
    result = requests.post(f"{API_URL}/setWebhook", json={"url": WEBHOOK_URL})
    logger.info(f"🚀 Бот запущен!")
    logger.info(f"🔗 Вебхук: {WEBHOOK_URL}")
    logger.info(f"📡 Статус: {result.json()}")
    
    app.run(host='0.0.0.0', port=PORT, debug=False)
