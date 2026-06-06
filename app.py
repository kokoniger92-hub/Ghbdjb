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
from aiogram.utils import executor
from selenium import webdriver
from selenium.webdriver.chrome.service import Service
from selenium.webdriver.chrome.options import Options
from bs4 import BeautifulSoup

# ========== НАСТРОЙКИ ==========
BOT_TOKEN = os.getenv("BOT_TOKEN", "YOUR_BOT_TOKEN_HERE")
ADMIN_CHAT_ID = int(os.getenv("ADMIN_CHAT_ID", "123456789"))

MIN_PRICE = 1
MAX_PRICE = 60

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

# Flask для пингов (чтобы Render не усыплял бота)
flask_app = Flask(__name__)

@flask_app.route('/')
def health_check():
    return "✅ Бот для поиска сим-карт работает!", 200

def run_flask():
    flask_app.run(host='0.0.0.0', port=10000)

# Запускаем Flask в отдельном потоке
Thread(target=run_flask, daemon=True).start()

# Инициализация бота
bot = Bot(token=BOT_TOKEN)
dp = Dispatcher(bot)
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
    
    # Путь к Chrome на Render
    service = Service("/usr/bin/chromedriver")
    return webdriver.Chrome(service=service, options=chrome_options)

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

async def send_notification(item):
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
        await bot.send_message(ADMIN_CHAT_ID, message, parse_mode="Markdown")
        logger.info(f"✅ Уведомление: {item['name'][:50]} - {item['price']}₽")
    except Exception as e:
        logger.error(f"❌ Ошибка отправки: {e}")

def search_ozon(driver, query):
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
                
                if MIN_PRICE <= price <= MAX_PRICE and check_balance_in_text(title):
                    results.append({'platform': 'Ozon', 'name': title, 'price': price, 'url': url})
                    logger.info(f"🎯 Найдено на Ozon: {title[:50]} - {price}₽")
            except Exception:
                continue
    except Exception as e:
        logger.error(f"Ozon ошибка: {e}")
    return results

def search_wildberries(driver, query):
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
                
                if MIN_PRICE <= price <= MAX_PRICE and check_balance_in_text(title):
                    results.append({'platform': 'Wildberries', 'name': title, 'price': price, 'url': url})
                    logger.info(f"🎯 Найдено на WB: {title[:50]} - {price}₽")
            except Exception:
                continue
    except Exception as e:
        logger.error(f"WB ошибка: {e}")
    return results

async def check_all():
    """Главная проверка"""
    global notified_products
    driver = None
    try:
        driver = create_driver()
        logger.info(f"🔍 Проверка в {datetime.now().strftime('%H:%M:%S')}")
        
        all_items = []
        for query in SEARCH_QUERIES:
            ozon_items = search_ozon(driver, query)
            wb_items = search_wildberries(driver, query)
            all_items.extend(ozon_items)
            all_items.extend(wb_items)
            await asyncio.sleep(1)
        
        # Отправляем уведомления о новых
        new_count = 0
        for item in all_items:
            key = f"{item['platform']}_{item['url']}"
            if key not in notified_products:
                await send_notification(item)
                notified_products.add(key)
                new_count += 1
        
        logger.info(f"✅ Проверка завершена. Новых: {new_count}, Всего найдено: {len(notified_products)}")
        
        if len(notified_products) > 1000:
            notified_products.clear()
            
    except Exception as e:
        logger.error(f"❌ Ошибка проверки: {e}")
    finally:
        if driver:
            driver.quit()

async def periodic_check():
    """Фоновая проверка"""
    while True:
        try:
            await check_all()
        except Exception as e:
            logger.error(f"Ошибка в periodic_check: {e}")
        await asyncio.sleep(CHECK_INTERVAL)

@dp.message_handler(commands=['start'])
async def start_cmd(message: types.Message):
    await message.answer(
        f"🤖 *Бот для поиска сим-карт запущен!*\n\n"
        f"💰 *Цена:* {MIN_PRICE} - {MAX_PRICE} ₽\n"
        f"💎 *Баланс:* 400 ₽\n"
        f"📈 *Выгода:* до 399 ₽ с карты\n"
        f"⏱ *Проверка:* каждые {CHECK_INTERVAL // 60} минут\n"
        f"🛍 *Площадки:* Ozon, Wildberries\n\n"
        f"✅ Как найду подходящую карту — сразу пришлю уведомление!",
        parse_mode="Markdown"
    )

@dp.message_handler(commands=['check'])
async def check_cmd(message: types.Message):
    msg = await message.answer("🔄 Запускаю ручную проверку...")
    await check_all()
    await msg.edit_text(f"✅ Ручная проверка завершена!\nНайдено карт: {len(notified_products)}")

@dp.message_handler(commands=['stats'])
async def stats_cmd(message: types.Message):
    await message.answer(
        f"📊 *Статистика бота*\n\n"
        f"📨 Отправлено уведомлений: {len(notified_products)}\n"
        f"💰 Диапазон цен: {MIN_PRICE} - {MAX_PRICE} ₽\n"
        f"💎 Баланс: 400 ₽\n"
        f"⏱ Интервал проверки: {CHECK_INTERVAL // 60} минут\n"
        f"🛍 Площадки: Ozon, Wildberries\n\n"
        f"📈 Потенциальная выгода: до {len(notified_products) * 340} ₽",
        parse_mode="Markdown"
    )

if __name__ == "__main__":
    logger.info("🚀 Бот запущен!")
    logger.info(f"🔍 Отслеживаем сим-карты с балансом 400₽ за {MIN_PRICE}-{MAX_PRICE}₽")
    logger.info(f"⏱ Проверка каждые {CHECK_INTERVAL} секунд")
    
    # Запускаем фоновую проверку
    loop = asyncio.get_event_loop()
    loop.create_task(periodic_check())
    
    # Запускаем бота
    executor.start_polling(dp, skip_updates=True)
