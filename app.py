import asyncio
import time
import random
import re
import os
import logging
from datetime import datetime
from threading import Thread
from flask import Flask
from aiogram import Bot, Dispatcher, types
from aiogram.contrib.middlewares.logging import LoggingMiddleware
from aiogram.types import InlineKeyboardMarkup, InlineKeyboardButton, CallbackQuery
from aiogram.utils import executor
from selenium import webdriver
from selenium.webdriver.chrome.service import Service
from selenium.webdriver.chrome.options import Options
from bs4 import BeautifulSoup

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
# ===============================

# Настройка логов
logging.basicConfig(level=logging.INFO)
logger = logging.getLogger(__name__)

# Flask для пингов
flask_app = Flask(__name__)

@flask_app.route('/')
def health_check():
    return "✅ Бот для поиска сим-карт работает!", 200

@flask_app.route('/webhook/<token>')
def webhook(token):
    if token == BOT_TOKEN:
        return "OK", 200
    return "Unauthorized", 403

def run_flask():
    flask_app.run(host='0.0.0.0', port=10000)

# Запускаем Flask в отдельном потоке
Thread(target=run_flask, daemon=True).start()

# Инициализация бота
bot = Bot(token=BOT_TOKEN)
dp = Dispatcher(bot)
dp.middleware.setup(LoggingMiddleware())

# Хранилище данных пользователей
user_settings = {}  # {user_id: {'min_price': int, 'max_price': int}}
notified_products = set()

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
    
    try:
        service = Service("/usr/bin/chromedriver")
        driver = webdriver.Chrome(service=service, options=chrome_options)
        return driver
    except:
        try:
            service = Service("/usr/bin/chromium-browser")
            driver = webdriver.Chrome(service=service, options=chrome_options)
            return driver
        except:
            from webdriver_manager.chrome import ChromeDriverManager
            service = Service(ChromeDriverManager().install())
            driver = webdriver.Chrome(service=service, options=chrome_options)
            return driver

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
    """Отправляет уведомление в Telegram"""
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
        logger.info(f"✅ Уведомление для {user_id}: {item['name'][:50]} - {item['price']}₽")
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
                    logger.info(f"🎯 Найдено на Ozon: {title[:50]} - {price}₽")
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
                    logger.info(f"🎯 Найдено на WB: {title[:50]} - {price}₽")
            except Exception:
                continue
    except Exception as e:
        logger.error(f"WB ошибка: {e}")
    return results

async def check_all_for_user(user_id, min_price, max_price):
    """Проверка для конкретного пользователя"""
    driver = None
    try:
        driver = create_driver()
        logger.info(f"🔍 Проверка для {user_id}: {min_price}-{max_price}₽")
        
        all_items = []
        for query in SEARCH_QUERIES:
            ozon_items = search_ozon(driver, query, min_price, max_price)
            wb_items = search_wildberries(driver, query, min_price, max_price)
            all_items.extend(ozon_items)
            all_items.extend(wb_items)
            await asyncio.sleep(1)
        
        # Отправляем уведомления о новых
        for item in all_items:
            key = f"{user_id}_{item['platform']}_{item['url']}"
            if key not in notified_products:
                await send_notification(user_id, item)
                notified_products.add(key)
        
        logger.info(f"✅ Проверка для {user_id} завершена. Найдено: {len(all_items)}")
        
        if len(notified_products) > 1000:
            # Очищаем старые записи (но не все)
            to_remove = list(notified_products)[:500]
            for key in to_remove:
                notified_products.remove(key)
            
    except Exception as e:
        logger.error(f"❌ Ошибка проверки для {user_id}: {e}")
    finally:
        if driver:
            driver.quit()

async def periodic_check():
    """Фоновая проверка для всех пользователей"""
    while True:
        try:
            for user_id, settings in user_settings.items():
                await check_all_for_user(user_id, settings['min_price'], settings['max_price'])
                await asyncio.sleep(10)  # Пауза между пользователями
        except Exception as e:
            logger.error(f"Ошибка в periodic_check: {e}")
        await asyncio.sleep(CHECK_INTERVAL)

def get_settings_keyboard(user_id):
    """Клавиатура для настроек"""
    settings = user_settings.get(user_id, {'min_price': DEFAULT_MIN_PRICE, 'max_price': DEFAULT_MAX_PRICE})
    keyboard = InlineKeyboardMarkup(row_width=2)
    keyboard.add(
        InlineKeyboardButton("💰 Мин. цена", callback_data="set_min"),
        InlineKeyboardButton("💰 Макс. цена", callback_data="set_max")
    )
    keyboard.add(
        InlineKeyboardButton("📊 Текущий диапазон", callback_data="show_range"),
        InlineKeyboardButton("🔄 Сбросить", callback_data="reset")
    )
    keyboard.add(
        InlineKeyboardButton("🔍 Проверить сейчас", callback_data="check_now")
    )
    return keyboard

@dp.message_handler(commands=['start'])
async def start_cmd(message: types.Message):
    user_id = message.from_user.id
    if user_id not in user_settings:
        user_settings[user_id] = {'min_price': DEFAULT_MIN_PRICE, 'max_price': DEFAULT_MAX_PRICE}
    
    await message.answer(
        f"🤖 *Бот для поиска дешёвых сим-карт!*\n\n"
        f"💎 *Что ищем:* Сим-карты с балансом 400₽\n"
        f"💰 *Текущий диапазон:* {user_settings[user_id]['min_price']} - {user_settings[user_id]['max_price']} ₽\n"
        f"⏱ *Проверка:* каждые {CHECK_INTERVAL // 60} минут\n"
        f"🛍 *Площадки:* Ozon, Wildberries\n\n"
        f"👇 *Настрой диапазон цен кнопками ниже:*",
        parse_mode="Markdown",
        reply_markup=get_settings_keyboard(user_id)
    )

@dp.message_handler(commands=['settings'])
async def settings_cmd(message: types.Message):
    user_id = message.from_user.id
    if user_id not in user_settings:
        user_settings[user_id] = {'min_price': DEFAULT_MIN_PRICE, 'max_price': DEFAULT_MAX_PRICE}
    
    await message.answer(
        f"⚙️ *Настройки*\n\n"
        f"💰 Мин. цена: {user_settings[user_id]['min_price']} ₽\n"
        f"💰 Макс. цена: {user_settings[user_id]['max_price']} ₽\n\n"
        f"Выбери действие:",
        parse_mode="Markdown",
        reply_markup=get_settings_keyboard(user_id)
    )

@dp.message_handler(commands=['check'])
async def check_cmd(message: types.Message):
    user_id = message.from_user.id
    if user_id not in user_settings:
        user_settings[user_id] = {'min_price': DEFAULT_MIN_PRICE, 'max_price': DEFAULT_MAX_PRICE}
    
    msg = await message.answer("🔄 Запускаю ручную проверку...")
    await check_all_for_user(user_id, user_settings[user_id]['min_price'], user_settings[user_id]['max_price'])
    await msg.edit_text(f"✅ Ручная проверка завершена!")

@dp.message_handler(commands=['stats'])
async def stats_cmd(message: types.Message):
    user_id = message.from_user.id
    settings = user_settings.get(user_id, {'min_price': DEFAULT_MIN_PRICE, 'max_price': DEFAULT_MAX_PRICE})
    
    # Считаем сколько уведомлений для этого пользователя
    user_notifications = sum(1 for key in notified_products if key.startswith(str(user_id)))
    
    await message.answer(
        f"📊 *Статистика*\n\n"
        f"📨 Уведомлений получено: {user_notifications}\n"
        f"💰 Твой диапазон: {settings['min_price']} - {settings['max_price']} ₽\n"
        f"💎 Баланс: 400 ₽\n"
        f"⏱ Интервал проверки: {CHECK_INTERVAL // 60} минут\n\n"
        f"📈 Потенциальная выгода: до {user_notifications * 340} ₽",
        parse_mode="Markdown"
    )

@dp.callback_query_handler(lambda c: True)
async def process_callback(callback_query: CallbackQuery):
    user_id = callback_query.from_user.id
    data = callback_query.data
    
    if user_id not in user_settings:
        user_settings[user_id] = {'min_price': DEFAULT_MIN_PRICE, 'max_price': DEFAULT_MAX_PRICE}
    
    if data == "set_min":
        await bot.send_message(user_id, "✏️ Введи *минимальную цену* в рублях (например: 1):", parse_mode="Markdown")
        await bot.answer_callback_query(callback_query.id)
        # Устанавливаем состояние ожидания ввода
        dp.register_message_handler(lambda m: set_min_price(m, user_id), lambda m: m.text and m.text.isdigit())
        
    elif data == "set_max":
        await bot.send_message(user_id, "✏️ Введи *максимальную цену* в рублях (например: 60):", parse_mode="Markdown")
        await bot.answer_callback_query(callback_query.id)
        dp.register_message_handler(lambda m: set_max_price(m, user_id), lambda m: m.text and m.text.isdigit())
        
    elif data == "show_range":
        settings = user_settings[user_id]
        await bot.send_message(
            user_id,
            f"📊 *Текущий диапазон*\n\n"
            f"💰 Минимальная цена: {settings['min_price']} ₽\n"
            f"💰 Максимальная цена: {settings['max_price']} ₽\n\n"
            f"Ищем сим-карты с балансом 400₽ в этом диапазоне.",
            parse_mode="Markdown"
        )
        await bot.answer_callback_query(callback_query.id)
        
    elif data == "reset":
        user_settings[user_id] = {'min_price': DEFAULT_MIN_PRICE, 'max_price': DEFAULT_MAX_PRICE}
        await bot.send_message(
            user_id,
            f"🔄 *Настройки сброшены!*\n\n"
            f"💰 Диапазон: {DEFAULT_MIN_PRICE} - {DEFAULT_MAX_PRICE} ₽",
            parse_mode="Markdown",
            reply_markup=get_settings_keyboard(user_id)
        )
        await bot.answer_callback_query(callback_query.id)
        
    elif data == "check_now":
        await bot.answer_callback_query(callback_query.id, "🔍 Проверка запущена...")
        msg = await bot.send_message(user_id, "🔄 Запускаю ручную проверку...")
        await check_all_for_user(user_id, user_settings[user_id]['min_price'], user_settings[user_id]['max_price'])
        await msg.edit_text(f"✅ Ручная проверка завершена!")

async def set_min_price(message: types.Message, user_id):
    """Устанавливает минимальную цену"""
    try:
        min_price = int(message.text)
        if min_price < 0:
            await message.answer("❌ Цена не может быть отрицательной! Введи число от 0 до 1000:")
            return
        
        user_settings[user_id]['min_price'] = min_price
        
        # Проверяем что min <= max
        if user_settings[user_id]['min_price'] > user_settings[user_id]['max_price']:
            user_settings[user_id]['max_price'] = min_price + 100
        
        await message.answer(
            f"✅ Минимальная цена установлена: {min_price} ₽\n\n"
            f"💰 Текущий диапазон: {user_settings[user_id]['min_price']} - {user_settings[user_id]['max_price']} ₽",
            reply_markup=get_settings_keyboard(user_id)
        )
    except ValueError:
        await message.answer("❌ Введи корректное число!")

async def set_max_price(message: types.Message, user_id):
    """Устанавливает максимальную цену"""
    try:
        max_price = int(message.text)
        if max_price < 0:
            await message.answer("❌ Цена не может быть отрицательной! Введи число от 0 до 1000:")
            return
        
        user_settings[user_id]['max_price'] = max_price
        
        # Проверяем что min <= max
        if user_settings[user_id]['min_price'] > user_settings[user_id]['max_price']:
            user_settings[user_id]['min_price'] = max_price - 100 if max_price > 100 else 1
        
        await message.answer(
            f"✅ Максимальная цена установлена: {max_price} ₽\n\n"
            f"💰 Текущий диапазон: {user_settings[user_id]['min_price']} - {user_settings[user_id]['max_price']} ₽",
            reply_markup=get_settings_keyboard(user_id)
        )
    except ValueError:
        await message.answer("❌ Введи корректное число!")

if __name__ == "__main__":
    logger.info("🚀 Бот запущен!")
    logger.info(f"🔍 Отслеживаем сим-карты с балансом 400₽")
    
    # Запускаем фоновую проверку
    loop = asyncio.get_event_loop()
    loop.create_task(periodic_check())
    
    # Запускаем бота
    executor.start_polling(dp, skip_updates=True)
