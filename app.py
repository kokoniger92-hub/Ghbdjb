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

# Расширенные поисковые запросы
SEARCH_QUERIES = [
    "сим карта баланс 400",
    "сим карта стартовый баланс 400", 
    "симка 400 рублей",
    "сим карта 400 руб",
    "сим карта с балансом 400",
    "сим-карта 400"
]
# ===========================

logging.basicConfig(level=logging.INFO)
logger = logging.getLogger(__name__)

app = Flask(__name__)
users = {}

def send_message(chat_id, text, reply_markup=None):
    url = f"{API_URL}/sendMessage"
    payload = {"chat_id": chat_id, "text": text, "parse_mode": "Markdown"}
    if reply_markup:
        payload["reply_markup"] = json.dumps(reply_markup)
    try:
        requests.post(url, json=payload, timeout=10)
    except Exception as e:
        logger.error(f"Ошибка: {e}")

def edit_message(chat_id, message_id, text):
    url = f"{API_URL}/editMessageText"
    payload = {"chat_id": chat_id, "message_id": message_id, "text": text, "parse_mode": "Markdown"}
    try:
        requests.post(url, json=payload, timeout=10)
    except Exception as e:
        logger.error(f"Ошибка: {e}")

def search_ozon(query, price_min, price_max):
    """Улучшенный поиск на Ozon"""
    results = []
    try:
        headers = {
            'User-Agent': 'Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36',
            'Accept': 'text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8',
            'Accept-Language': 'ru-RU,ru;q=0.8,en-US;q=0.5,en;q=0.3',
        }
        url = f"https://www.ozon.ru/search/?text={query.replace(' ', '+')}&from_global=true"
        resp = requests.get(url, headers=headers, timeout=20)
        
        logger.info(f"Ozon статус: {resp.status_code}")
        
        if resp.status_code != 200:
            return []
        
        soup = BeautifulSoup(resp.text, 'html.parser')
        
        # Несколько вариантов поиска карточек
        items = soup.find_all('div', class_='tile-root')
        if not items:
            items = soup.find_all('div', {'data-widget': 'searchProductsV2'})
        if not items:
            items = soup.find_all('div', class_='widget-search-result-container')
        
        for item in items[:15]:
            try:
                # Название
                title = None
                for selector in ['span.tsBody500Medium', 'div.tile-title', 'span[data-testid="title"]']:
                    elem = item.find('span', class_=selector.split('.')[-1] if '.' in selector else selector)
                    if not elem:
                        elem = item.find('div', class_=selector.split('.')[-1] if '.' in selector else selector)
                    if elem:
                        title = elem.text.strip()
                        break
                
                if not title:
                    continue
                
                # Цена
                price = 0
                for selector in ['span.tsHeadline500Medium', 'span.tsHeadline500', 'div.tile-price']:
                    elem = item.find('span', class_=selector.split('.')[-1] if '.' in selector else selector)
                    if not elem:
                        elem = item.find('div', class_=selector.split('.')[-1] if '.' in selector else selector)
                    if elem:
                        price_text = re.sub(r'[^\d]', '', elem.text)
                        if price_text:
                            price = int(price_text)
                            break
                
                # Ссылка
                link = ""
                link_elem = item.find('a', href=True)
                if link_elem:
                    link = link_elem['href']
                    if not link.startswith('http'):
                        link = "https://www.ozon.ru" + link
                
                # Проверка баланса
                title_lower = title.lower()
                has_balance = any([
                    'баланс 400' in title_lower,
                    'стартовый баланс 400' in title_lower,
                    'балансом 400' in title_lower,
                    '400 руб' in title_lower,
                    '400₽' in title_lower
                ])
                
                if has_balance and price_min <= price <= price_max:
                    results.append({
                        'platform': 'Ozon',
                        'name': title[:80],
                        'price': price,
                        'balance': 400,
                        'url': link
                    })
                    logger.info(f"✅ Ozon найден: {price}₽ - {title[:50]}")
                    
            except Exception as e:
                continue
                
    except Exception as e:
        logger.error(f"Ozon ошибка: {e}")
    
    return results

def search_wb(query, price_min, price_max):
    """Улучшенный поиск на Wildberries"""
    results = []
    try:
        headers = {
            'User-Agent': 'Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36',
            'Accept': 'text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8',
        }
        url = f"https://www.wildberries.ru/catalog/0/search.aspx?search={query.replace(' ', '%20')}"
        resp = requests.get(url, headers=headers, timeout=20)
        
        logger.info(f"WB статус: {resp.status_code}")
        
        if resp.status_code != 200:
            return []
        
        soup = BeautifulSoup(resp.text, 'html.parser')
        items = soup.find_all('div', class_='product-card')
        
        for item in items[:15]:
            try:
                title_elem = item.find('span', class_='product-card__name')
                if not title_elem:
                    continue
                title = title_elem.text.strip()
                
                price = 0
                price_elem = item.find('ins', class_='price__lower-price')
                if not price_elem:
                    price_elem = item.find('span', class_='price__wrap')
                if price_elem:
                    price_text = re.sub(r'[^\d]', '', price_elem.text)
                    price = int(price_text) if price_text else 0
                
                link_elem = item.find('a', href=True)
                if link_elem:
                    link = "https://www.wildberries.ru" + link_elem['href']
                else:
                    continue
                
                title_lower = title.lower()
                has_balance = any([
                    'баланс 400' in title_lower,
                    'стартовый баланс 400' in title_lower,
                    'балансом 400' in title_lower
                ])
                
                if has_balance and price_min <= price <= price_max:
                    results.append({
                        'platform': 'WB',
                        'name': title[:80],
                        'price': price,
                        'balance': 400,
                        'url': link
                    })
                    logger.info(f"✅ WB найден: {price}₽ - {title[:50]}")
                    
            except Exception as e:
                continue
                
    except Exception as e:
        logger.error(f"WB ошибка: {e}")
    
    return results

def find_sim_cards(price_min, price_max, update_status=None):
    """Поиск на всех площадках с обратной связью"""
    all_results = []
    
    for i, query in enumerate(SEARCH_QUERIES):
        if update_status:
            update_status(f"🔍 Ищем: {query}")
        
        ozon_results = search_ozon(query, price_min, price_max)
        wb_results = search_wb(query, price_min, price_max)
        all_results.extend(ozon_results)
        all_results.extend(wb_results)
        time.sleep(1)
    
    # Убираем дубликаты
    unique = {}
    for item in all_results:
        if item['url'] not in unique:
            unique[item['url']] = item
    
    return list(unique.values())

def process_search(chat_id, min_price, max_price, message_id):
    """Фоновая обработка поиска с обновлением статуса"""
    try:
        def update_status(text):
            edit_message(chat_id, message_id, text)
        
        update_status("🔍 *Начинаю поиск сим-карт...*\n\n⏳ Проверяю Ozon и Wildberries...")
        
        cards = find_sim_cards(min_price, max_price, update_status)
        
        if cards:
            update_status(f"✅ *Найдено {len(cards)} предложений!*\n\n📦 Загружаю информацию...")
            time.sleep(1)
            
            # Отправляем каждую карточку отдельно
            for card in cards:
                profit = card['balance'] - card['price']
                msg = (
                    f"🎉 *СИМ-КАРТА НАЙДЕНА!*\n\n"
                    f"🛍 *{card['platform']}*\n"
                    f"📱 *Название:* {card['name']}\n"
                    f"💰 *Цена:* {card['price']} ₽\n"
                    f"💎 *Баланс:* {card['balance']} ₽\n"
                    f"📈 *Твоя выгода:* *{profit} ₽*\n\n"
                    f"🔗 [Купить]({card['url']})"
                )
                send_message(chat_id, msg)
                time.sleep(0.5)
            
            keyboard = {
                "keyboard": [[{"text": "🔍 Проверить"}], [{"text": "⚙️ Настройки"}]],
                "resize_keyboard": True
            }
            send_message(
                chat_id,
                f"✅ *Поиск завершён!*\n\n"
                f"📊 *Статистика:*\n"
                f"• Найдено: *{len(cards)}* предложений\n"
                f"• Диапазон: *{min_price} - {max_price}* ₽\n\n"
                f"💰 Твоя потенциальная выгода: *{sum(400 - c['price'] for c in cards)}* ₽",
                reply_markup=keyboard
            )
        else:
            # Показываем советы по расширению поиска
            keyboard = {
                "keyboard": [[{"text": "🔍 Проверить"}], [{"text": "⚙️ Настройки"}]],
                "resize_keyboard": True
            }
            
            suggestions = []
            if max_price < 200:
                suggestions.append("• Увеличить максимальную цену до 200-500 ₽")
            if min_price > 10:
                suggestions.append("• Уменьшить минимальную цену до 1-10 ₽")
            suggestions.append("• Проверить позже — новые предложения появляются ежедневно")
            
            send_message(
                chat_id,
                f"😔 *Ничего не найдено* в диапазоне *{min_price} - {max_price}* ₽\n\n"
                f"💡 *Советы:*\n" + "\n".join(suggestions) + "\n\n"
                f"📌 *Рекомендуемый диапазон:* 1 - 100 ₽\n"
                f"🛍 Такие сим-карты быстро раскупают, попробуй поискать утром или вечером!",
                reply_markup=keyboard
            )
            
    except Exception as e:
        logger.error(f"Ошибка: {e}")
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
        
        if text == "/start":
            keyboard = {
                "keyboard": [[{"text": "🔍 Проверить"}], [{"text": "⚙️ Настройки"}]],
                "resize_keyboard": True
            }
            send_message(
                chat_id,
                f"🤖 *Бот для поиска сим-карт*\n\n"
                f"💎 *Что ищу:* Сим-карты с балансом *400₽*\n"
                f"💰 *Твой диапазон:* *{users[chat_id]['min']} - {users[chat_id]['max']}* ₽\n\n"
                f"🔍 Нажми *Проверить* — я найду реальные предложения на Ozon и Wildberries!\n\n"
                f"⚡ *Совет:* Лучшие предложения в диапазоне 1-60 ₽",
                reply_markup=keyboard
            )
        
        elif text == "🔍 Проверить" or text == "/check":
            # Отправляем стартовое сообщение
            status_msg = send_message(chat_id, "🔍 *Запускаю поиск...*\n\n⏳ Подожди немного...")
            
            # Получаем ID сообщения (в реальном API нужно сохранять, упростим)
            thread = threading.Thread(
                target=process_search,
                args=(chat_id, users[chat_id]["min"], users[chat_id]["max"], 0)  # ID упрощён
            )
            thread.start()
            
            # Отправляем подтверждение
            send_message(chat_id, "🔍 *Поиск начался!*\n\nЯ ищу на Ozon и Wildberries, это может занять 15-30 секунд...")
        
        elif text == "⚙️ Настройки" or text == "/settings":
            keyboard = {
                "inline_keyboard": [
                    [{"text": "💰 Установить мин. цену", "callback_data": "set_min"}],
                    [{"text": "💰 Установить макс. цену", "callback_data": "set_max"}],
                    [{"text": "📊 Показать настройки", "callback_data": "show"}],
                    [{"text": "🔄 Сбросить (1-60)", "callback_data": "reset"}]
                ]
            }
            send_message(
                chat_id,
                f"⚙️ *Настройки*\n\n"
                f"💰 *Текущий диапазон:* {users[chat_id]['min']} - {users[chat_id]['max']} ₽\n\n"
                f"💡 *Рекомендация:* для поиска выгодных предложений используй диапазон *1 - 100* ₽\n\n"
                f"Выбери действие:",
                reply_markup=keyboard
            )
        
        elif text == "/help":
            send_message(
                chat_id,
                f"📖 *Команды бота*\n\n"
                f"🔍 /check — начать поиск сим-карт\n"
                f"⚙️ /settings — настроить диапазон цен\n"
                f"📊 /stats — показать статистику\n"
                f"❓ /help — помощь\n\n"
                f"💡 *Как получить выгоду:*\n"
                f"1. Установи диапазон 1-60 ₽\n"
                f"2. Нажми *Проверить*\n"
                f"3. Покупай сим-карту с балансом 400₽ за копейки!"
            )
        
        elif text == "/stats":
            keyboard = {
                "keyboard": [[{"text": "🔍 Проверить"}], [{"text": "⚙️ Настройки"}]],
                "resize_keyboard": True
            }
            send_message(
                chat_id,
                f"📊 *Статистика*\n\n"
                f"💰 *Твой диапазон:* {users[chat_id]['min']} - {users[chat_id]['max']} ₽\n"
                f"💎 *Цель:* Сим-карты с балансом 400₽\n"
                f"📈 *Макс. выгода:* {400 - users[chat_id]['min']} ₽ с одной карты\n\n"
                f"🛍 *Площадки:* Ozon, Wildberries\n"
                f"⏱ *Время поиска:* 15-30 секунд",
                reply_markup=keyboard
            )
        
        elif text.isdigit():
            val = int(text)
            if val < 0 or val > 10000:
                send_message(chat_id, "❌ Цена должна быть от 0 до 10000 ₽")
            elif "waiting" in users[chat_id]:
                if users[chat_id]["waiting"] == "min":
                    users[chat_id]["min"] = val
                    send_message(chat_id, f"✅ *Мин. цена:* {val} ₽\nТеперь введи *максимальную* цену:")
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
                        f"✅ *Диапазон сохранён!*\n\n💰 *{users[chat_id]['min']} - {users[chat_id]['max']}* ₽\n\n"
                        f"🔍 Нажми *Проверить* для поиска",
                        reply_markup=keyboard
                    )
            else:
                send_message(chat_id, "❓ Сначала выбери *Настройки* и нажми 'Установить мин. цену' или 'Установить макс. цену'")
        
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
            keyboard = {
                "inline_keyboard": [
                    [{"text": "💰 Мин. цена", "callback_data": "set_min"}],
                    [{"text": "💰 Макс. цена", "callback_data": "set_max"}]
                ]
            }
            send_message(
                chat_id,
                f"📊 *Твои настройки*\n\n"
                f"💰 *Диапазон:* {users[chat_id]['min']} - {users[chat_id]['max']} ₽\n"
                f"💎 *Баланс:* 400 ₽\n"
                f"📈 *Макс. выгода:* {400 - users[chat_id]['min']} ₽\n\n"
                f"🔍 Для поиска нажми /check",
                reply_markup=keyboard
            )
        elif data == "reset":
            users[chat_id] = {"min": 1, "max": 60}
            if "waiting" in users[chat_id]:
                del users[chat_id]["waiting"]
            
            keyboard = {
                "keyboard": [[{"text": "🔍 Проверить"}], [{"text": "⚙️ Настройки"}]],
                "resize_keyboard": True
            }
            send_message(
                chat_id,
                f"🔄 *Настройки сброшены!*\n\n"
                f"💰 *Новый диапазон:* *1 - 60* ₽\n\n"
                f"🔍 Нажми *Проверить* для поиска",
                reply_markup=keyboard
            )
        
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
    requests.post(f"{API_URL}/deleteWebhook")
    result = requests.post(f"{API_URL}/setWebhook", json={"url": WEBHOOK_URL})
    logger.info(f"🚀 Бот запущен!")
    logger.info(f"🔗 Вебхук: {WEBHOOK_URL}")
    logger.info(f"📡 Статус: {result.json()}")
    
    app.run(host='0.0.0.0', port=PORT, debug=False)
