import os
import logging
import asyncio
from datetime import datetime
from flask import Flask, jsonify
from aiogram import Bot, Dispatcher, types
from aiogram.utils import executor

# ========== КОНФИГ ==========
BOT_TOKEN = os.getenv("BOT_TOKEN", "8897748741:AAG2f8sHicGX_wxGEBPm2gqgbULhkGp4weE")
ADMIN_CHAT_ID = int(os.getenv("ADMIN_CHAT_ID", "5979001063"))
PORT = int(os.getenv("PORT", 10000))
# ===========================

logging.basicConfig(level=logging.INFO)
logger = logging.getLogger(__name__)

# Flask приложение
flask_app = Flask(__name__)

# Бот
bot = Bot(token=BOT_TOKEN)
dp = Dispatcher(bot)

# ========== КОМАНДЫ БОТА ==========
@dp.message_handler(commands=['start'])
async def start_cmd(message: types.Message):
    await message.answer(
        f"🤖 *Бот для поиска сим-карт запущен!*\n\n"
        f"💎 *Что ищем:* Сим-карты с балансом 400₽\n"
        f"💰 *Диапазон цен:* 1 - 60 ₽\n"
        f"⏱ *Проверка:* вручную по команде /check\n\n"
        f"✅ Бот работает стабильно!\n"
        f"🔍 Нажми /check для поиска",
        parse_mode="Markdown"
    )
    logger.info(f"Пользователь {message.from_user.id} запустил бота")

@dp.message_handler(commands=['check'])
async def check_cmd(message: types.Message):
    status_msg = await message.answer("🔄 *Поиск сим-карт...*\n\nЭто может занять несколько секунд", parse_mode="Markdown")
    
    # Имитация поиска (в реальности здесь будет парсинг)
    await asyncio.sleep(2)
    
    # Отправляем результат
    await message.answer(
        f"🎉 *Найдена выгодная сим-карта!*\n\n"
        f"📱 *Оператор:* МТС / Tele2\n"
        f"💰 *Цена:* 49 ₽\n"
        f"💎 *Баланс:* 400 ₽\n"
        f"📈 *Ваша выгода:* 351 ₽\n\n"
        f"🔗 [Купить на Ozon](https://www.ozon.ru/product/sim-karta-mts-400-rubley-123456789/)\n"
        f"🔗 [Купить на Wildberries](https://www.wildberries.ru/catalog/987654321/detail.aspx)\n\n"
        f"⚠️ *Демо-режим:* Бот показывает пример. Реальный парсинг добавим позже.",
        parse_mode="Markdown",
        disable_web_page_preview=True
    )
    
    await status_msg.edit_text("✅ *Поиск завершён!*\n\nНайдено предложений: 1", parse_mode="Markdown")

@dp.message_handler(commands=['help'])
async def help_cmd(message: types.Message):
    await message.answer(
        f"📖 *Команды бота:*\n\n"
        f"/start - Запустить бота\n"
        f"/check - Проверить наличие сим-карт\n"
        f"/help - Показать это сообщение\n\n"
        f"⚙️ *Как пользоваться:*\n"
        f"1. Нажми /check\n"
        f"2. Бот найдёт лучшие предложения\n"
        f"3. Переходи по ссылке и покупай!\n\n"
        f"💡 Бот ищет сим-карты с балансом 400₽ в диапазоне 1-60₽",
        parse_mode="Markdown"
    )

@dp.message_handler()
async def unknown_cmd(message: types.Message):
    await message.answer(
        f"❓ *Неизвестная команда*\n\n"
        f"Используй: /start, /check или /help",
        parse_mode="Markdown"
    )

# ========== FLASK ДЛЯ HEALTH CHECK ==========
@flask_app.route('/')
def index():
    return jsonify({
        "status": "ok",
        "bot": "running",
        "timestamp": datetime.now().isoformat()
    })

@flask_app.route('/health')
def health():
    return jsonify({"status": "alive"})

# ========== ЗАПУСК ==========
if __name__ == "__main__":
    from threading import Thread
    
    def run_flask():
        flask_app.run(host='0.0.0.0', port=PORT, debug=False, use_reloader=False)
    
    # Запускаем Flask в отдельном потоке
    flask_thread = Thread(target=run_flask, daemon=True)
    flask_thread.start()
    
    logger.info(f"🚀 Бот запущен на порту {PORT}")
    logger.info(f"🤖 Токен: {BOT_TOKEN[:20]}...")
    logger.info(f"👤 Admin ID: {ADMIN_CHAT_ID}")
    
    # Запускаем бота
    executor.start_polling(dp, skip_updates=True)
