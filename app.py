import os
import json
import logging
import requests
import time
import random
import re
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

# Поисковые запросы
SEARCH_QUERIES = [
    "сим карта баланс 400",
    "сим карта стартовый баланс 400",
    "симка 400 рублей"
]
# ===========================

logging.basicConfig(level=logging.INFO)
logger = logging.getLogger(__name__)

app = Flask(__name__)
users = {}
found_links = set()  # Чтобы не дублировать уведомления

def send_message(chat_id, text, reply_markup=None):
    url = f"{API_URL}/sendMessage"
    payload = {"chat_id": chat_id, "text": text, "parse_mode": "Markdown"}
    if reply_markup:
        payload["reply_markup"] = json.dumps(reply_markup)
    try:
        requests.post(url, json=payload, timeout=10)
    except Exception as e:
        logger.error(f"Ошибка: {e}")

def search_ozon(query, price_min, price_max):
    """Поиск на Ozon (реальный парсинг)"""
    results = []
    try:
        headers = {
            'User-Agent': 'Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36'
        }
        url = f"https://www.ozon.ru/search/?text={query.replace(' ', '+')}"
        resp = requests.get(url, headers=headers, timeout=15)
        
        if resp.status_code != 200:
            return []
        
        soup = BeautifulSoup(resp.text, 'html.parser')
        
        # Ищем карточки товаров
        items = soup.find_all('div', class_='tile-root') or soup.find_all('div', {'data-widget': 'searchProductsV2'})
        
        for item in items[:10]:
            try:
                # Название
                title_elem = item.find('span', class_='tsBody500Medium')
                if not title_elem:
                    continue
                title = title_elem.text.strip()
                
                # Цена
                price = 0
                price_elem = item.find('span', class_='tsHeadline500Medium')
                if price_elem:
                    price_text = re.sub(r'[^\d]', '', price_elem.text)
                    price = int(price_text) if price_text else 0
                
                # Ссылка
                link_elem = item.find('a', href=True)
                if link_elem:
                    link = "https://www.ozon.ru" + link_elem['href']
                else:
                    continue
                
                # Проверка баланса 400 в названии
                if 'баланс 400' in title.lower() or 'стартовый баланс 400' in title.lower():
                    if price_min <= price <= price_max:
                        results.append({
                            'platform': 'Ozon',
                            'name': title[:80],
                            'price': price,
                            'balance': 400,
                            'url': link
                        })
                        logger.info(f"✅ Ozon: {price}₽ - {title[:50]}")
            except Exception as e:
                continue
    except Exception as e:
        logger.error(f"Ozon ошибка: {e}")
    
    return results

def search_wb(query, price_min, price_max):
    """Поиск на Wildberries (реальный парсинг)"""
    results = []
    try:
        headers = {
            'User-Agent': 'Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36'
        }
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
                        logger.info(f"✅ WB: {price}₽ - {title[:50]}")
            except Exception as e:
                continue
    except Exception as e:
        logger.error(f"WB ошибка: {e}")
    
    return results

def find_sim_cards(price_min, price_max):
    """Поиск на всех площадках"""
    all_results = []
    
    for query in SEARCH_QUERIES:
        ozon_results = search_ozon(query, price_min, price_max)
        wb_results = search_wb(query, price_min, price_max)
        all_results.extend(ozon_results)
        all_results.extend(wb_results)
        time.sleep(1)  # Пауза между запросами
    
    # Убираем дубликаты по ссылке
    unique = {}
    for item in all_results:
        if item['url'] not in unique:
            unique[item['url']] = item
    
    return list(unique.values())

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
                f"🔍 Нажми *Проверить* — бот найдёт реальные предложения на Ozon и WB!",
                reply_markup=keyboard
            )
        
        # Проверить (реальный поиск!)
        elif text == "🔍 Проверить" or text == "/check":
            send_message(chat_id, "🔍 *Ищу реальные предложения на Ozon и WB...*\n\nЭто может занять 10-15 секунд", parse_mode="Markdown")
            
            cards = find_sim_cards(users[chat_id]["min"], users[chat_id]["max"])
            
            if cards:
                for card in cards:
                    profit = card['balance'] - card['price']
                    msg_text = (
                        f"🎉 *НАЙДЕНА СИМ-КАРТА!*\n\n"
                        f"🛍 *Платформа:* {card['platform']}\n"
                        f"📱 *Название:* {card['name']}\n"
                        f"💰 *Цена:* {card['price']} ₽\n"
                        f"💎 *Баланс:* {card['balance']} ₽\n"
                        f"📈 *Твоя выгода:* {profit} ₽\n\n"
                        f"🔗 [Купить]({card['url']})"
                    )
                    send_message(chat_id, msg_text)
                    time.sleep(0.5)
                send_message(chat_id, f"✅ *Найдено {len(cards)} предложений!*")
            else:
                send_message(
                    chat_id,
                    f"😔 *Ничего не найдено* в диапазоне {users[chat_id]['min']}-{users[chat_id]['max']} ₽\n\n"
                    f"💡 Попробуй:\n"
                    f"• Расширить диапазон в *Настройки*\n"
                    f"• Проверить позже — предложения появляются каждый день"
                )
        
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
                f"⚙️ *Настройки*\n\n💰 Твой диапазон: *{users[chat_id]['min']} - {users[chat_id]['max']} ₽*",
                reply_markup=keyboard
            )
        
        elif text == "/help":
            send_message(chat_id, "📖 *Команды*\n/start - Запуск\n/check - Поиск\n/settings - Настройки")
        
        elif text.isdigit():
            val = int(text)
            if "waiting" in users[chat_id]:
                if users[chat_id]["waiting"] == "min":
                    users[chat_id]["min"] = val
                    send_message(chat_id, f"✅ Мин. цена: {val} ₽\nТеперь введи *макс.* цену:", parse_mode="Markdown")
                    users[chat_id]["waiting"] = "max"
                elif users[chat_id]["waiting"] == "max":
                    users[chat_id]["max"] = val
                    del users[chat_id]["waiting"]
                    send_message(chat_id, f"✅ *Сохранено!*\n💰 {users[chat_id]['min']} - {users[chat_id]['max']} ₽\n\n🔍 Нажми *Проверить*", parse_mode="Markdown")
            else:
                send_message(chat_id, "❓ Сначала выбери *Настройки*")
        
        else:
            send_message(chat_id, "❓ Используй кнопки или /help")
    
    elif "callback_query" in update:
        cb = update["callback_query"]
        chat_id = cb.get("message", {}).get("chat", {}).get("id")
        data = cb.get("data", "")
        cb_id = cb.get("id")
        
        if data == "set_min":
            users[chat_id]["waiting"] = "min"
            send_message(chat_id, "✏️ Введи *минимальную* цену:", parse_mode="Markdown")
        elif data == "set_max":
            users[chat_id]["waiting"] = "max"
            send_message(chat_id, "✏️ Введи *максимальную* цену:", parse_mode="Markdown")
        elif data == "show":
            send_message(chat_id, f"📊 *Настройки*\n💰 {users[chat_id]['min']} - {users[chat_id]['max']} ₽\n💎 Баланс: 400 ₽")
        
        requests.post(f"{API_URL}/answerCallbackQuery", json={"callback_query_id": cb_id})
    
    return "OK", 200

@app.route('/')
def index():
    return jsonify({"status": "ok", "users": len(users)})

@app.route('/set_webhook')
def set_webhook():
    url = f"{API_URL}/setWebhook"
    resp = requests.post(url, json={"url": WEBHOOK_URL})
    return jsonify(resp.json())

if __name__ == "__main__":
    requests.post(f"{API_URL}/deleteWebhook")
    requests.post(f"{API_URL}/setWebhook", json={"url": WEBHOOK_URL})
    logger.info(f"🚀 Бот запущен! Вебхук: {WEBHOOK_URL}")
    app.run(host='0.0.0.0', port=PORT, debug=False)
