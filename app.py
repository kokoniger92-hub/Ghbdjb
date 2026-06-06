import os
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

# Пробуем импорт selenium
try:
    from selenium import webdriver
    from selenium.webdriver.chrome.service import Service
    from selenium.webdriver.chrome.options import Options
    SELENIUM_OK = True
except:
    SELENIUM_OK = False
    print("⚠️ Selenium не установлен")

# ========== КОНФИГ ==========
BOT_TOKEN = os.getenv("BOT_TOKEN", "8897748741:AAG2f8sHicGX_wxGEBPm2gqgbULhkGp4weE")
ADMIN_CHAT_ID = int(os.getenv("ADMIN_CHAT_ID", "5979001063"))

MIN_PRICE = 1
MAX_PRICE = 60
CHECK_INTERVAL = 300
PORT = int(os.getenv("PORT", 10000))
# ===========================

logging.basicConfig(level=logging.INFO)
logger = logging.getLogger(__name__)

# Flask
flask_app = Flask(__name__)

# Бот
bot = Bot(token=BOT_TOKEN)
dp = Dispatcher(bot)

# Хранилище
user_settings = {}
notified = set()

def create_driver():
    """Создаёт драйвер Chrome"""
    if not SELENIUM_OK:
        return None
    
    opts = Options()
    opts.add_argument("--headless=new")
    opts.add_argument("--no-sandbox")
    opts.add_argument("--disable-dev-shm-usage")
    opts.add_argument("--disable-gpu")
    
    # Render пути
    paths = ["/usr/bin/chromedriver", "/usr/bin/chromium-browser", "/usr/bin/chromium"]
    for path in paths:
        try:
            if os.path.exists(path):
                driver = webdriver.Chrome(service=Service(path), options=opts)
                logger.info(f"✅ Драйвер: {path}")
                return driver
        except:
            continue
    
    try:
        from webdriver_manager.chrome import ChromeDriverManager
        driver = webdriver.Chrome(service=Service(ChromeDriverManager().install()), options=opts)
        logger.info("✅ Драйвер через webdriver-manager")
        return driver
    except Exception as e:
        logger.error(f"❌ Драйвер ошибка: {e}")
        return None

def has_balance_400(text):
    """Проверка баланса 400"""
    if not text:
        return False
    return bool(re.search(r'баланс\s*400|стартовый\s*баланс\s*400|400\s*руб|400₽', text.lower()))

def search_ozon():
    """Парсинг Ozon"""
    results = []
    driver = None
    try:
        driver = create_driver()
        if not driver:
            return []
        
        queries = ["сим карта баланс 400", "сим карта стартовый баланс 400"]
        for q in queries:
            driver.get(f"https://www.ozon.ru/search/?text={q.replace(' ', '+')}")
            time.sleep(3)
            
            soup = BeautifulSoup(driver.page_source, 'html.parser')
            items = soup.find_all('div', class_='tile-root')[:5]
            
            for item in items:
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
                    url = "https://www.ozon.ru" + link_elem['href'] if link_elem else ""
                    
                    if MIN_PRICE <= price <= MAX_PRICE and has_balance_400(title):
                        results.append({'platform': 'Ozon', 'name': title[:100], 'price': price, 'url': url})
                        logger.info(f"🎯 Ozon: {price}₽")
                except:
                    continue
    except Exception as e:
        logger.error(f"Ozon ошибка: {e}")
    finally:
        if driver:
            driver.quit()
    return results

def search_wb():
    """Парсинг Wildberries"""
    results = []
    driver = None
    try:
        driver = create_driver()
        if not driver:
            return []
        
        queries = ["сим карта баланс 400", "сим карта стартовый баланс 400"]
        for q in queries:
            driver.get(f"https://www.wildberries.ru/catalog/0/search.aspx?search={q.replace(' ', '%20')}")
            time.sleep(3)
            
            soup = BeautifulSoup(driver.page_source, 'html.parser')
            items = soup.find_all('div', class_='product-card')[:5]
            
            for item in items:
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
                    
                    if MIN_PRICE <= price <= MAX_PRICE and has_balance_400(title):
                        results.append({'platform': 'WB', 'name': title[:100], 'price': price, 'url': url})
                        logger.info(f"🎯 WB: {price}₽")
                except:
                    continue
    except Exception as e:
        logger.error(f"WB ошибка: {e}")
    finally:
        if driver:
            driver.quit()
    return results

async def check_and_notify(user_id, min_p, max_p):
    """Проверка и уведомление"""
    global MIN_PRICE, MAX_PRICE
    MIN_PRICE, MAX_PRICE = min_p, max_p
    
    logger.info(f"🔍 Поиск для {user_id}: {min_p}-{max_p}₽")
    
    # Если selenium не работает, отправляем тестовое сообщение
    if not SELENIUM_OK:
        await bot.send_message(user_id, "⚠️ Бот работает в тестовом режиме. Selenium не установлен.")
        return
    
    items = search_ozon() + search_wb()
    
    for item in items:
        key = f"{user_id}_{item['platform']}_{item['url']}"
        if key not in notified:
            profit = 400 - item['price']
            msg = (
                f"🎉 *СИМ-КАРТА НАЙДЕНА!*\n\n"
                f"🛍 *Платформа:* {item['platform']}\n"
                f"📱 *Название:* {item['name']}\n"
                f"💰 *Цена:* {item['price']} ₽\n"
                f"💎 *Баланс:* 400 ₽\n"
                f"📈 *Выгода:* {profit} ₽\n\n"
                f"🔗 [Купить]({item['url']})"
            )
            try:
                await bot.send_message(user_id, msg, parse_mode="Markdown")
                notified.add(key)
                logger.info(f"✅ Уведомление для {user_id}")
            except Exception as e:
                logger.error(f"Ошибка отправки: {e}")
    
    logger.info(f"✅ Найдено: {len(items)}")

# ========== КОМАНДЫ БОТА ==========
@dp.message_handler(commands=['start'])
async def start_cmd(msg: types.Message):
    uid = msg.from_user.id
    if uid not in user_settings:
        user_settings[uid] = {'min': 1, 'max': 60}
    
    kb = InlineKeyboardMarkup(row_width=2)
    kb.add(
        InlineKeyboardButton("💰 Мин. цена", callback_data="set_min"),
        InlineKeyboardButton("💰 Макс. цена", callback_data="set_max")
    )
    kb.add(
        InlineKeyboardButton("🔍 Проверить сейчас", callback_data="check"),
        InlineKeyboardButton("📊 Настройки", callback_data="show")
    )
    
    await msg.answer(
        f"🤖 *Бот для поиска сим-карт*\n\n"
        f"💰 *Твой диапазон:* {user_settings[uid]['min']} - {user_settings[uid]['max']} ₽\n"
        f"💎 *Баланс:* 400 ₽\n"
        f"⏱ *Проверка:* раз в 5 минут\n\n"
        f"👇 Настрой кнопками:",
        parse_mode="Markdown",
        reply_markup=kb
    )

@dp.callback_query_handler(lambda c: True)
async def cb_handler(cb: types.CallbackQuery):
    uid = cb.from_user.id
    data = cb.data
    
    if data == "set_min":
        await bot.send_message(uid, "✏️ Введи *минимальную цену* (например: 1):", parse_mode="Markdown")
        await cb.answer()
    elif data == "set_max":
        await bot.send_message(uid, "✏️ Введи *максимальную цену* (например: 60):", parse_mode="Markdown")
        await cb.answer()
    elif data == "check":
        await cb.answer("🔍 Ищу...")
        m = await bot.send_message(uid, "🔄 Поиск...")
        await check_and_notify(uid, user_settings[uid]['min'], user_settings[uid]['max'])
        await m.edit_text("✅ Поиск завершён!")
    elif data == "show":
        s = user_settings.get(uid, {'min': 1, 'max': 60})
        await bot.send_message(uid, f"📊 *Твой диапазон:* {s['min']} - {s['max']} ₽", parse_mode="Markdown")
        await cb.answer()

@dp.message_handler(lambda m: m.text and m.text.isdigit())
async def price_input(m: types.Message):
    uid = m.from_user.id
    val = int(m.text)
    
    if uid not in user_settings:
        user_settings[uid] = {'min': 1, 'max': 60}
    
    if 'waiting' not in user_settings[uid]:
        user_settings[uid]['min'] = val
        user_settings[uid]['waiting'] = 'max'
        await m.answer(f"✅ Мин. цена: {val}₽\nТеперь введи *максимальную* цену:", parse_mode="Markdown")
    else:
        user_settings[uid]['max'] = val
        del user_settings[uid]['waiting']
        await m.answer(
            f"✅ *Диапазон сохранён!*\n💰 {user_settings[uid]['min']} - {user_settings[uid]['max']} ₽\n\n"
            f"Напиши /start для главного меню",
            parse_mode="Markdown"
        )

# ========== ФОНОВАЯ ПРОВЕРКА ==========
async def background_worker():
    while True:
        try:
            for uid, settings in user_settings.items():
                if 'waiting' not in settings:
                    await check_and_notify(uid, settings['min'], settings['max'])
                await asyncio.sleep(10)
        except Exception as e:
            logger.error(f"Фон ошибка: {e}")
        await asyncio.sleep(CHECK_INTERVAL)

def start_bot():
    loop = asyncio.new_event_loop()
    asyncio.set_event_loop(loop)
    loop.create_task(background_worker())
    executor.start_polling(dp, skip_updates=True)

# ========== FLASK ==========
@flask_app.route('/')
def index():
    return jsonify({
        "status": "ok",
        "bot": "running",
        "users": len(user_settings),
        "notified": len(notified)
    })

@flask_app.route('/health')
def health():
    return jsonify({"status": "alive", "time": datetime.now().isoformat()})

# ========== ЗАПУСК ==========
if __name__ == "__main__":
    logger.info(f"🔥 Запуск на порту {PORT}")
    
    # Запускаем бота в потоке
    t = threading.Thread(target=start_bot, daemon=True)
    t.start()
    
    time.sleep(2)
    logger.info("✅ Бот запущен")
    
    # Flask в главном потоке
    flask_app.run(host='0.0.0.0', port=PORT, debug=False, use_reloader=False)
