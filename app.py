import asyncio
import time
import random
import re
import os
import logging
from datetime import datetime
from flask import Flask, jsonify
from aiogram import Bot, Dispatcher, types
from aiogram.contrib.middlewares.logging import LoggingMiddleware
from aiogram.types import InlineKeyboardMarkup, InlineKeyboardButton
from bs4 import BeautifulSoup
from selenium import webdriver
from selenium.webdriver.chrome.service import Service
from selenium.webdriver.chrome.options import Options

# ========== НАСТРОЙКИ ==========
BOT_TOKEN = os.getenv("BOT_TOKEN", "YOUR_BOT_TOKEN_HERE")
ADMIN_CHAT_ID = int(os.getenv("ADMIN_CHAT_ID", "123456789"))

# Значения по умолчанию
DEFAULT_MIN_PRICE = 1
DEFAULT_MAX_PRICE = 60

SEARCH_QUERIES = [
    "сим карта баланс 400",
    "сим карта стартовый баланс 400",
    "симка 400 рублей",
    "сим карта 400р баланс"
]

CHECK_INTERVAL = 300  # 5 минут
PORT = int(os.getenv("PORT", 10000))
# ===============================

# Настройка логов
logging.basicConfig(level=logging.INFO)
logger = logging.getLogger(__name__)

# Flask приложение
app = Flask(__name__)

# Хранилище данных пользователей
user_settings = {}
notified_products = set()

# Инициализация бота
bot = Bot(token=BOT_TOKEN)
dp = Dispatcher(bot)
dp.middleware.setup(LoggingMiddleware())

def create_driver():
    """Создаёт браузер для Render"""
    chrome_options = Options()
    chrome_options.add_argument("--headless=new")
    chrome_options.add_argument("--no-sandbox")
    chrome_options.add_argument("--disable-dev-shm-usage")
    chrome_options.add_argument("--disable-gpu")
    chrome_options.add_argument("--disable-blink-features=AutomationControlled")
    chrome_options.add_argument("--window-size=1920,1080")
    chrome_options.add_argument("--remote-debugging-port=9222")
    chrome_options.add_argument(
        "user-agent=Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
        "AppleWebKit/537.36 (KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36"
    )
    
    # Пробуем разные пути к Chrome
    paths = ["/usr/bin/chromedriver", "/usr/bin/chromium-browser", "/usr/bin/chromium"]
    for path in paths:
        try:
            service = Service(path)
            driver = webdriver.Chrome(service=service, options=chrome_options)
            logger.info(f"✅ Драйвер создан: {path}")
            return driver
        except:
            continue
    
    try:
        from webdriver_manager.chrome import ChromeDriverManager
        service = Service(ChromeDriverManager().install())
        driver = webdriver.Chrome(service=service, options=chrome_options)
        return driver
    except Exception as e:
        logger.error(f"❌ Ошибка драйвера: {e}")
        raise

def check_balance_in_text(text):
    """Проверяет упоминание баланса 400"""
    if not text:
        return False
    text_lower = text.lower()
    patterns = [
        r'баланс\s*400', r'стартовый\s*баланс\s*400',
        r'баланс\s*400р', r'400\s*руб', r'400₽', r'балансом\s*400'
    ]
    for pattern in patterns:
        if re.search(pattern, text_lower):
            return True
    return False

async def send_notification(user_id, item):
    """Отправляет уведомление"""
    profit = 400 - item['price']
    message = (
        f"🎉 *НАЙДЕНА ВЫГОДНАЯ СИМ-КАРТА!*\n\n"
        f"🛍 *Платформа:* {item['platform']}\n"
        f"📱 *Название:* {item['name'][:100]}\n"
        f"💰 *Цена:* {item['price']} ₽\n"
        f"💎 *Баланс:* 400 ₽\n"
        f"📈 *Выгода:* {profit} ₽\n"
        f"🕐 *Найдено:* {datetime.now().strftime('%H:%M:%S')}\n\n"
        f"🔗 [Купить на {item['platform']}]({item['url']})"
    )
    try:
        await bot.send_message(user_id, message, parse_mode="Markdown")
        logger.info(f"✅ Уведомление для {user_id}: {item['name'][:50]}")
    except Exception as e:
        logger.error(f"❌ Ошибка отправки: {e}")

def search_ozon(driver, query, min_price, max_price):
    """Парсинг Ozon"""
    results = []
    try:
        url = f"https://www.ozon.ru/search/?text={query.replace(' ', '+')}"
        driver.get(url)
        time.sleep(random.uniform(2, 4))
        
        soup = BeautifulSoup(driver.page_source, 'html.parser')
        items = soup.find_all('div', {'data-widget': 'searchProductsV2'})
        if not items:
            items = soup.find_all('div', class_='tile-root')
        
        for item in items[:10]:
            try:
                title_elem = item.find('span', class_='tsBody500Medium') or item.find('div', class_='tile-title')
                if not title_elem:
                    continue
                title = title_elem.text.strip()
                
                price = 0
                price_elem = item.find('span', class_='tsHeadline500Medium') or item.find('span', class_='tsHeadline500')
                if price_elem:
                    price_text = re.sub(r'[^\d]', '', price_elem.text)
                    price = int(price_text) if price_text else 0
                
                link_elem = item.find('a', href=True)
                url = "https://www.ozon.ru" + link_elem['href'] if link_elem else ""
                
                if min_price <= price <= max_price and check_balance_in_text(title):
                    results.append({'platform': 'Ozon', 'name': title, 'price': price, 'url': url})
            except Exception:
                continue
    except Exception as e:
        logger.error(f"Ozon ошибка: {e}")
    return results

def search_wildberries(driver, query, min_price, max_price):
    """Парсинг Wildberries"""
    results = []
    try:
        url = f"https://www.wildberries.ru/catalog/0/search.aspx?search={query.replace(' ', '%20')}"
        driver.get(url)
        time.sleep(random.uniform(2, 4))
        
        soup = BeautifulSoup(driver.page_source, 'html.parser')
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
                url = "https://www.wildberries.ru" + link_elem['href'] if link_elem else ""
                
                if min_price <= price <= max_price and check_balance_in_text(title):
                    results.append({'platform': 'Wildberries', 'name': title, 'price': price, 'url': url})
            except Exception:
                continue
    except Exception as e:
        logger.error(f"WB ошибка: {e}")
    return results

async def check_all_for_user(user_id, min_price, max_price):
    """Проверка для пользователя"""
    driver = None
    try:
        driver = create_driver()
        all_items = []
        for query in SEARCH_QUERIES:
            all_items.extend(search_ozon(driver, query, min_price, max_price))
            all_items.extend(search_wildberries(driver, query, min_price, max_price))
            await asyncio.sleep(1)
        
        for item in all_items:
            key = f"{user_id}_{item['platform']}_{item['url']}"
            if key not in notified_products:
                await send_notification(user_id, item)
                notified_products.add(key)
        
        if len(notified_products) > 1000:
            to_remove = list(notified_products)[:500]
            for key in to_remove:
                notified_products.remove(key)
    except Exception as e:
        logger.error(f"Ошибка: {e}")
    finally:
        if driver:
            driver.quit()

def get_settings_keyboard(user_id):
    """Клавиатура настроек"""
    settings = user_settings.get(user_id, {'min_price': DEFAULT_MIN_PRICE, 'max_price': DEFAULT_MAX_PRICE})
    keyboard = InlineKeyboardMarkup(row_width=2)
    keyboard.add(
        InlineKeyboardButton("💰 Мин. цена", callback_data=f"set_min_{user_id}"),
        InlineKeyboardButton("💰 Макс. цена", callback_data=f"set_max_{user_id}")
    )
    keyboard.add(
        InlineKeyboardButton("📊 Диапазон", callback_data=f"show_{user_id}"),
        InlineKeyboardButton("🔄 Сброс", callback_data=f"reset_{user_id}")
    )
    keyboard.add(
        InlineKeyboardButton("🔍 Проверить", callback_data=f"check_{user_id}")
    )
    return keyboard

# Flask маршруты
@app.route('/')
def index():
    return jsonify({"status": "ok", "message": "Бот работает"}), 200

@app.route('/health')
def health():
    return jsonify({"status": "alive"}), 200

# Обработчики команд
@dp.message_handler(commands=['start'])
async def start_cmd(message: types.Message):
    user_id = message.from_user.id
    if user_id not in user_settings:
        user_settings[user_id] = {'min_price': DEFAULT_MIN_PRICE, 'max_price': DEFAULT_MAX_PRICE}
    
    await message.answer(
        f"🤖 *Бот для поиска сим-карт*\n\n"
        f"💰 Диапазон: {user_settings[user_id]['min_price']}-{user_settings[user_id]['max_price']}₽\n"
        f"💎 Баланс: 400₽\n"
        f"⏱ Проверка: каждые {CHECK_INTERVAL//60} мин\n\n"
        f"👇 Настрой кнопками:",
        parse_mode="Markdown",
        reply_markup=get_settings_keyboard(user_id)
    )

# Callback обработчики
@dp.callback_query_handler(lambda c: c.data.startswith('set_min_'))
async def set_min_callback(callback: types.CallbackQuery):
    user_id = int(callback.data.split('_')[2])
    await bot.send_message(user_id, "✏️ Введи *минимальную цену* (число):", parse_mode="Markdown")
    await callback.answer()

@dp.callback_query_handler(lambda c: c.data.startswith('set_max_'))
async def set_max_callback(callback: types.CallbackQuery):
    user_id = int(callback.data.split('_')[2])
    await bot.send_message(user_id, "✏️ Введи *максимальную цену* (число):", parse_mode="Markdown")
    await callback.answer()

@dp.callback_query_handler(lambda c: c.data.startswith('show_'))
async def show_callback(callback: types.CallbackQuery):
    user_id = int(callback.data.split('_')[1])
    settings = user_settings.get(user_id, {'min_price': DEFAULT_MIN_PRICE, 'max_price': DEFAULT_MAX_PRICE})
    await bot.send_message(
        user_id,
        f"📊 *Текущий диапазон*\n💰 {settings['min_price']} - {settings['max_price']} ₽",
        parse_mode="Markdown"
    )
    await callback.answer()

@dp.callback_query_handler(lambda c: c.data.startswith('reset_'))
async def reset_callback(callback: types.CallbackQuery):
    user_id = int(callback.data.split('_')[1])
    user_settings[user_id] = {'min_price': DEFAULT_MIN_PRICE, 'max_price': DEFAULT_MAX_PRICE}
    await bot.send_message(
        user_id,
        f"🔄 Сброшено! Диапазон: {DEFAULT_MIN_PRICE}-{DEFAULT_MAX_PRICE}₽",
        reply_markup=get_settings_keyboard(user_id)
    )
    await callback.answer()

@dp.callback_query_handler(lambda c: c.data.startswith('check_'))
async def check_callback(callback: types.CallbackQuery):
    user_id = int(callback.data.split('_')[1])
    await callback.answer("🔍 Проверка...")
    msg = await bot.send_message(user_id, "🔄 Ищу...")
    await check_all_for_user(user_id, user_settings[user_id]['min_price'], user_settings[user_id]['max_price'])
    await msg.edit_text("✅ Готово!")

@dp.message_handler(lambda msg: msg.text and msg.text.isdigit())
async def price_input(message: types.Message):
    user_id = message.from_user.id
    value = int(message.text)
    
    # Простое определение: если ввели число, спрашиваем что менять
    await message.answer(
        f"Ты ввел {value}₽\nЧто сделать?\n/set_min - установить как мин. цену\n/set_max - установить как макс. цену"
    )
    # Сохраняем последнее введенное значение
    if not hasattr(price_input, 'last_value'):
        price_input.last_value = {}
    price_input.last_value[user_id] = value

@dp.message_handler(commands=['set_min'])
async def set_min(message: types.Message):
    user_id = message.from_user.id
    if hasattr(price_input, 'last_value') and user_id in price_input.last_value:
        value = price_input.last_value[user_id]
        user_settings[user_id]['min_price'] = value
        await message.answer(f"✅ Мин. цена: {value}₽")
    else:
        await message.answer("❌ Сначала введи число")

@dp.message_handler(commands=['set_max'])
async def set_max(message: types.Message):
    user_id = message.from_user.id
    if hasattr(price_input, 'last_value') and user_id in price_input.last_value:
        value = price_input.last_value[user_id]
        user_settings[user_id]['max_price'] = value
        await message.answer(f"✅ Макс. цена: {value}₽")
    else:
        await message.answer("❌ Сначала введи число")

async def background_check():
    """Фоновая проверка"""
    while True:
        try:
            for user_id, settings in user_settings.items():
                await check_all_for_user(user_id, settings['min_price'], settings['max_price'])
                await asyncio.sleep(10)
        except Exception as e:
            logger.error(f"Ошибка: {e}")
        await asyncio.sleep(CHECK_INTERVAL)

# Запуск
async def main():
    # Запускаем фоновую проверку
    asyncio.create_task(background_check())
    
    # Запускаем polling
    from aiogram.utils import executor
    executor.start_polling(dp, skip_updates=True)

if __name__ == "__main__":
    # Запускаем Flask в отдельном потоке для health check
    from threading import Thread
    def run_flask():
        app.run(host='0.0.0.0', port=PORT, debug=False)
    
    flask_thread = Thread(target=run_flask, daemon=True)
    flask_thread.start()
    
    # Запускаем бота
    loop = asyncio.get_event_loop()
    loop.create_task(background_check())
    executor.start_polling(dp, skip_updates=True)
