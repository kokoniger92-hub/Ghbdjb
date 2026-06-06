import os
import sys
import time
import re
import logging
import threading
import asyncio
from datetime import datetime
from flask import Flask, jsonify
from aiogram import Bot, Dispatcher, types
from aiogram.types import InlineKeyboardMarkup, InlineKeyboardButton
from aiogram.utils import executor
from bs4 import BeautifulSoup

# Пробуем импортировать selenium с обработкой ошибок
try:
    from selenium import webdriver
    from selenium.webdriver.chrome.service import Service
    from selenium.webdriver.chrome.options import Options
    SELENIUM_AVAILABLE = True
except ImportError:
    SELENIUM_AVAILABLE = False
    print("⚠️ Selenium не установлен, работаем в демо-режиме")

# ========== НАСТРОЙКИ ==========
BOT_TOKEN = os.getenv("BOT_TOKEN", "YOUR_BOT_TOKEN_HERE")
ADMIN_CHAT_ID = int(os.getenv("ADMIN_CHAT_ID", "123456789"))

# Диапазон цен по умолчанию
DEFAULT_MIN_PRICE = 1
DEFAULT_MAX_PRICE = 60

# Поисковые запросы
SEARCH_QUERIES = [
    "сим карта баланс 400",
    "сим карта стартовый баланс 400",
    "симка 400 рублей"
]

CHECK_INTERVAL = 300  # 5 минут
PORT = int(os.getenv("PORT", 10000))
# ===============================

# Настройка логов
logging.basicConfig(level=logging.INFO)
logger = logging.getLogger(__name__)

# Flask приложение (ОСНОВНОЙ поток)
flask_app = Flask(__name__)

# Хранилище
user_settings = {}
notified_products = set()
bot = Bot(token=BOT_TOKEN)
dp = Dispatcher(bot)

# ========== ФУНКЦИИ ДЛЯ ПАРСИНГА ==========
def create_driver():
    """Создаёт браузер для парсинга"""
    if not SELENIUM_AVAILABLE:
        return None
    
    chrome_options = Options()
    chrome_options.add_argument("--headless=new")
    chrome_options.add_argument("--no-sandbox")
    chrome_options.add_argument("--disable-dev-shm-usage")
    chrome_options.add_argument("--disable-gpu")
    chrome_options.add_argument("--window-size=1920,1080")
    
    # Пробуем разные пути
    paths = [
        "/usr/bin/chromedriver",
        "/usr/bin/chromium-browser", 
        "/usr/bin/chromium",
        "/snap/bin/chromium"
    ]
    
    for path in paths:
        try:
            if os.path.exists(path):
                service = Service(path)
                driver = webdriver.Chrome(service=service, options=chrome_options)
                logger.info(f"✅ Драйвер создан: {path}")
                return driver
        except:
            continue
    
    # Пробуем через webdriver-manager
    try:
        from webdriver_manager.chrome import ChromeDriverManager
        service = Service(ChromeDriverManager().install())
        driver = webdriver.Chrome(service=service, options=chrome_options)
        logger.info("✅ Драйвер создан через webdriver-manager")
        return driver
    except Exception as e:
        logger.error(f"❌ Ошибка драйвера: {e}")
        return None

def check_balance_in_text(text):
    """Проверяет упоминание баланса 400"""
    if not text:
        return False
    text_lower = text.lower()
    patterns = [
        r'баланс\s*400', r'стартовый\s*баланс\s*400',
        r'баланс\s*400р', r'400\s*руб', r'400₽'
    ]
    return any(re.search(pattern, text_lower) for pattern in patterns)

def search_ozon(min_price, max_price):
    """Поиск на Ozon (упрощённая версия)"""
    results = []
    if not SELENIUM_AVAILABLE:
        # Демо-режим
        results.append({
            'platform': 'Ozon (демо)',
            'name': 'Сим-карта с балансом 400₽',
            'price': 50,
            'url': 'https://www.ozon.ru'
        })
        return results
    
    driver = None
    try:
        driver = create_driver()
        if not driver:
            return results
        
        for query in SEARCH_QUERIES:
            url = f"https://www.ozon.ru/search/?text={query.replace(' ', '+')}"
            driver.get(url)
            time.sleep(3)
            
            soup = BeautifulSoup(driver.page_source, 'html.parser')
            items = soup.find_all('div', class_='tile-root')[:5]
            
            for item in items:
                try:
                    title_elem = item.find('span', class_='tsBody500Medium')
                    if not title_elem:
                        continue
                    title = title_elem.text.strip()
                    
                    price_elem = item.find('span', class_='tsHeadline500Medium')
                    price = 0
                    if price_elem:
                        price_text = re.sub(r'[^\d]', '', price_elem.text)
                        price = int(price_text) if price_text else 0
                    
                    link_elem = item.find('a', href=True)
                    url = "https://www.ozon.ru" + link_elem['href'] if link_elem else ""
                    
                    if min_price <= price <= max_price and check_balance_in_text(title):
                        results.append({
                            'platform': 'Ozon',
                            'name': title[:100],
                            'price': price,
                            'url': url
                        })
                        logger.info(f"🎯 Найдено на Ozon: {price}₽")
                except Exception as e:
                    continue
    except Exception as e:
        logger.error(f"Ozon ошибка: {e}")
    finally:
        if driver:
            driver.quit()
    
    return results

def search_wildberries(min_price, max_price):
    """Поиск на Wildberries (упрощённая версия)"""
    results = []
    if not SELENIUM_AVAILABLE:
        return results
    
    driver = None
    try:
        driver = create_driver()
        if not driver:
            return results
        
        for query in SEARCH_QUERIES:
            url = f"https://www.wildberries.ru/catalog/0/search.aspx?search={query.replace(' ', '%20')}"
            driver.get(url)
            time.sleep(3)
            
            soup = BeautifulSoup(driver.page_source, 'html.parser')
            items = soup.find_all('div', class_='product-card')[:5]
            
            for item in items:
                try:
                    title_elem = item.find('span', class_='product-card__name')
                    if not title_elem:
                        continue
                    title = title_elem.text.strip()
                    
                    price_elem = item.find('ins', class_='price__lower-price')
                    price = 0
                    if price_elem:
                        price_text = re.sub(r'[^\d]', '', price_elem.text)
                        price = int(price_text) if price_text else 0
                    
                    link_elem = item.find('a', href=True)
                    url = "https://www.wildberries.ru" + link_elem['href'] if link_elem else ""
                    
                    if min_price <= price <= max_price and check_balance_in_text(title):
                        results.append({
                            'platform': 'WB',
                            'name': title[:100],
                            'price': price,
                            'url': url
                        })
                        logger.info(f"🎯 Найдено на WB: {price}₽")
                except Exception as e:
                    continue
    except Exception as e:
        logger.error(f"WB ошибка: {e}")
    finally:
        if driver:
            driver.quit()
    
    return results

async def check_all_for_user(user_id, min_price, max_price):
    """Проверка для пользователя"""
    logger.info(f"🔍 Проверка для {user_id}: {min_price}-{max_price}₽")
    
    # Поиск товаров
    ozon_items = search_ozon(min_price, max_price)
    wb_items = search_wildberries(min_price, max_price)
    all_items = ozon_items + wb_items
    
    # Отправка уведомлений
    for item in all_items:
        key = f"{user_id}_{item['platform']}_{item['url']}"
        if key not in notified_products:
            profit = 400 - item['price']
            message = (
                f"🎉 *НАЙДЕНА СИМ-КАРТА!*\n\n"
                f"🛍 *Платформа:* {item['platform']}\n"
                f"📱 *Название:* {item['name']}\n"
                f"💰 *Цена:* {item['price']} ₽\n"
                f"💎 *Баланс:* 400 ₽\n"
                f"📈 *Выгода:* {profit} ₽\n\n"
                f"🔗 [Купить]({item['url']})"
            )
            try:
                await bot.send_message(user_id, message, parse_mode="Markdown")
                notified_products.add(key)
                logger.info(f"✅ Уведомление отправлено {user_id}")
            except Exception as e:
                logger.error(f"Ошибка отправки: {e}")
    
    logger.info(f"✅ Проверка завершена, найдено: {len(all_items)}")

def run_bot():
    """Запуск бота в отдельном потоке"""
    loop = asyncio.new_event_loop()
    asyncio.set_event_loop(loop)
    
    @dp.message_handler(commands=['start'])
    async def start_cmd(message: types.Message):
        user_id = message.from_user.id
        if user_id not in user_settings:
            user_settings[user_id] = {'min_price': DEFAULT_MIN_PRICE, 'max_price': DEFAULT_MAX_PRICE}
        
        keyboard = InlineKeyboardMarkup(row_width=2)
        keyboard.add(
            InlineKeyboardButton("💰 Мин. цена", callback_data="set_min"),
            InlineKeyboardButton("💰 Макс. цена", callback_data="set_max")
        )
        keyboard.add(
            InlineKeyboardButton("🔍 Проверить сейчас", callback_data="check_now"),
            InlineKeyboardButton("📊 Текущий диапазон", callback_data="show_range")
        )
        
        await message.answer(
            f"🤖 *Бот для поиска сим-карт*\n\n"
            f"💰 Диапазон: {user_settings[user_id]['min_price']} - {user_settings[user_id]['max_price']} ₽\n"
            f"💎 Баланс: 400 ₽\n"
            f"⏱ Проверка: каждые {CHECK_INTERVAL // 60} мин\n\n"
            f"👇 Настрой кнопками:",
            parse_mode="Markdown",
            reply_markup=keyboard
        )
    
    @dp.callback_query_handler(lambda c: True)
    async def callback_handler(callback: types.CallbackQuery):
        user_id = callback.from_user.id
        data = callback.data
        
        if data == "set_min":
            await bot.send_message(user_id, "✏️ Введи *минимальную цену* (число):", parse_mode="Markdown")
            await callback.answer()
        elif data == "set_max":
            await bot.send_message(user_id, "✏️ Введи *максимальную цену* (число):", parse_mode="Markdown")
            await callback.answer()
        elif data == "check_now":
            await callback.answer("🔍 Ищу...")
            msg = await bot.send_message(user_id, "🔄 Поиск...")
            await check_all_for_user(user_id, user_settings[user_id]['min_price'], user_settings[user_id]['max_price'])
            await msg.edit_text("✅ Проверка завершена!")
        elif data == "show_range":
            settings = user_settings.get(user_id, {'min_price': DEFAULT_MIN_PRICE, 'max_price': DEFAULT_MAX_PRICE})
            await bot.send_message(
                user_id,
                f"📊 *Текущий диапазон*\n💰 {settings['min_price']} - {settings['max_price']} ₽",
                parse_mode="Markdown"
            )
            await callback.answer()
    
    @dp.message_handler(lambda msg: msg.text and msg.text.isdigit())
    async def price_input(message: types.Message):
        user_id = message.from_user.id
        value = int(message.text)
        
        if user_id not in user_settings:
            user_settings[user_id] = {'min_price': DEFAULT_MIN_PRICE, 'max_price': DEFAULT_MAX_PRICE}
        
        # Если минимальная цена не установлена
        if 'temp_price' not in user_settings[user_id]:
            user_settings[user_id]['min_price'] = value
            user_settings[user_id]['temp_price'] = True
            await message.answer(f"✅ Мин. цена: {value}₽\nТеперь введи *максимальную цену*:", parse_mode="Markdown")
        else:
            user_settings[user_id]['max_price'] = value
            del user_settings[user_id]['temp_price']
            await message.answer(
                f"✅ Диапазон установлен: {user_settings[user_id]['min_price']} - {user_settings[user_id]['max_price']} ₽\n"
                f"🔍 Напиши /start для главного меню"
            )
    
    # Фоновая проверка
    async def background_check():
        while True:
            try:
                for user_id, settings in user_settings.items():
                    if 'temp_price' not in settings:
                        await check_all_for_user(user_id, settings['min_price'], settings['max_price'])
                    await asyncio.sleep(10)
            except Exception as e:
                logger.error(f"Фоновая ошибка: {e}")
            await asyncio.sleep(CHECK_INTERVAL)
    
    # Запускаем фоновую проверку
    loop.create_task(background_check())
    
    # Запускаем бота
    logger.info("🚀 Бот запущен!")
    executor.start_polling(dp, skip_updates=True)

# ========== FLASK (ОСНОВНОЙ ПОТОК) ==========
@flask_app.route('/')
def index():
    return jsonify({
        "status": "ok",
        "bot": "running",
        "users": len(user_settings),
        "notifications": len(notified_products)
    }), 200

@flask_app.route('/health')
def health():
    return jsonify({"status": "alive", "timestamp": datetime.now().isoformat()}), 200

if __name__ == "__main__":
    logger.info(f"🔥 Запуск на порту {PORT}")
    
    # Запускаем бота в отдельном потоке
    bot_thread = threading.Thread(target=run_bot, daemon=True)
    bot_thread.start()
    
    # Ждём 2 секунды для запуска бота
    time.sleep(2)
    
    # Запускаем Flask в ОСНОВНОМ потоке (Render увидит порт!)
    flask_app.run(host='0.0.0.0', port=PORT, debug=False, use_reloader=False)
