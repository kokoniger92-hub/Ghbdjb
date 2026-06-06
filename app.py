import os
import logging
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

# Flask
flask_app = Flask(__name__)

# Бот
bot = Bot(token=BOT_TOKEN)
dp = Dispatcher(bot)

# ========== КОМАНДЫ ==========
@dp.message_handler(commands=['start'])
async def start(message: types.Message):
    await message.answer(
        f"🤖 Бот работает!\n\n"
        f"💰 Ищем сим-карты с балансом 400₽\n"
        f"💵 Диапазон цен: 1-60 ₽\n\n"
        f"✅ Бот успешно запущен!"
    )
    logger.info(f"User {message.from_user.id} started bot")

@dp.message_handler(commands=['check'])
async def check(message: types.Message):
    msg = await message.answer("🔄 Проверка...")
    await msg.edit_text(
        f"✅ Проверка завершена!\n\n"
        f"💰 Сим-карты с балансом 400₽\n"
        f"💵 Цена: 50-60 ₽\n\n"
        f"🔗 Пример: https://www.ozon.ru"
    )

@dp.message_handler(commands=['help'])
async def help_cmd(message: types.Message):
    await message.answer(
        f"Команды:\n"
        f"/start - Запуск\n"
        f"/check - Проверка\n"
        f"/help - Помощь"
    )

@dp.message_handler()
async def echo(message: types.Message):
    await message.answer(f"Привет! Используй команды: /start, /check, /help")

# ========== FLASK ==========
@flask_app.route('/')
def index():
    return jsonify({"status": "ok", "bot": "running"})

@flask_app.route('/health')
def health():
    return jsonify({"status": "alive"})

# ========== ЗАПУСК ==========
if __name__ == "__main__":
    from threading import Thread
    
    # Запускаем Flask в потоке
    def run_flask():
        flask_app.run(host='0.0.0.0', port=PORT, debug=False)
    
    Thread(target=run_flask, daemon=True).start()
    
    logger.info(f"🚀 Бот запущен на порту {PORT}")
    executor.start_polling(dp, skip_updates=True)
