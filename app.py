import os
import logging
import asyncio
from datetime import datetime
from flask import Flask, jsonify
from aiogram import Bot, Dispatcher, types
from aiogram.types import InlineKeyboardMarkup, InlineKeyboardButton
from aiogram.utils import executor

# ========== КОНФИГ ==========
BOT_TOKEN = os.getenv("BOT_TOKEN", "8897748741:AAG2f8sHicGX_wxGEBPm2gqgbULhkGp4weE")
ADMIN_CHAT_ID = int(os.getenv("ADMIN_CHAT_ID", "5979001063"))

PORT = int(os.getenv("PORT", 10000))
# ===========================

# Настройка логов
logging.basicConfig(level=logging.INFO)
logger = logging.getLogger(__name__)

# Flask
flask_app = Flask(__name__)

# Бот
bot = Bot(token=BOT_TOKEN)
dp = Dispatcher(bot)

# Хранилище пользователей
user_settings = {}

# ========== КОМАНДЫ БОТА ==========
@dp.message_handler(commands=['start'])
async def start_cmd(message: types.Message):
    user_id = message.from_user.id
    
    if user_id not in user_settings:
        user_settings[user_id] = {'min': 1, 'max': 60}
    
    keyboard = InlineKeyboardMarkup(row_width=2)
    keyboard.add(
        InlineKeyboardButton("💰 Установить мин. цену", callback_data="set_min"),
        InlineKeyboardButton("💰 Установить макс. цену", callback_data="set_max")
    )
    keyboard.add(
        InlineKeyboardButton("📊 Мои настройки", callback_data="show"),
        InlineKeyboardButton("ℹ️ Помощь", callback_data="help")
    )
    
    await message.answer(
        f"🤖 *Бот для поиска сим-карт запущен!*\n\n"
        f"💎 *Что ищем:* Сим-карты с балансом 400₽\n"
        f"💰 *Твой диапазон:* {user_settings[user_id]['min']} - {user_settings[user_id]['max']} ₽\n\n"
        f"🔍 Бот будет искать сим-карты автоматически и присылать уведомления.\n\n"
        f"👇 Используй кнопки для настройки:",
        parse_mode="Markdown",
        reply_markup=keyboard
    )
    
    # Логируем запуск
    logger.info(f"Пользователь {user_id} запустил бота")
    await bot.send_message(ADMIN_CHAT_ID, f"✅ Бот запущен пользователем {user_id}")

@dp.callback_query_handler(lambda c: True)
async def process_callback(callback_query: types.CallbackQuery):
    user_id = callback_query.from_user.id
    data = callback_query.data
    
    if data == "set_min":
        await bot.send_message(
            user_id,
            "✏️ Введи *минимальную цену* в рублях (например: 1):",
            parse_mode="Markdown"
        )
        await callback_query.answer()
        
    elif data == "set_max":
        await bot.send_message(
            user_id,
            "✏️ Введи *максимальную цену* в рублях (например: 60):",
            parse_mode="Markdown"
        )
        await callback_query.answer()
        
    elif data == "show":
        if user_id in user_settings:
            settings = user_settings[user_id]
            await bot.send_message(
                user_id,
                f"📊 *Твои настройки*\n\n"
                f"💰 Минимальная цена: {settings['min']} ₽\n"
                f"💰 Максимальная цена: {settings['max']} ₽\n"
                f"💎 Баланс: 400 ₽",
                parse_mode="Markdown"
            )
        else:
            await bot.send_message(user_id, "❌ Настройки не найдены. Напиши /start")
        await callback_query.answer()
        
    elif data == "help":
        await bot.send_message(
            user_id,
            f"ℹ️ *Помощь*\n\n"
            f"🤖 Бот ищет сим-карты с балансом 400₽ на маркетплейсах.\n\n"
            f"📝 *Команды:*\n"
            f"/start - Главное меню\n"
            f"/settings - Настройки\n"
            f"/check - Ручная проверка\n"
            f"/help - Помощь\n\n"
            f"⚙️ *Как пользоваться:*\n"
            f"1. Установи мин. и макс. цену\n"
            f"2. Бот сам найдёт выгодные предложения\n"
            f"3. Получай уведомления о находках!",
            parse_mode="Markdown"
        )
        await callback_query.answer()

@dp.message_handler(commands=['settings'])
async def settings_cmd(message: types.Message):
    user_id = message.from_user.id
    if user_id not in user_settings:
        user_settings[user_id] = {'min': 1, 'max': 60}
    
    keyboard = InlineKeyboardMarkup(row_width=2)
    keyboard.add(
        InlineKeyboardButton("💰 Мин. цена", callback_data="set_min"),
        InlineKeyboardButton("💰 Макс. цена", callback_data="set_max")
    )
    keyboard.add(
        InlineKeyboardButton("📊 Текущие", callback_data="show"),
        InlineKeyboardButton("🔄 Сброс", callback_data="reset")
    )
    
    await message.answer(
        f"⚙️ *Настройки*\n\n"
        f"💰 Мин. цена: {user_settings[user_id]['min']} ₽\n"
        f"💰 Макс. цена: {user_settings[user_id]['max']} ₽",
        parse_mode="Markdown",
        reply_markup=keyboard
    )

@dp.message_handler(commands=['check'])
async def check_cmd(message: types.Message):
    user_id = message.from_user.id
    
    if user_id not in user_settings:
        user_settings[user_id] = {'min': 1, 'max': 60}
    
    msg = await message.answer("🔄 Ищу сим-карты...")
    
    # Демо-поиск (позже добавим реальный парсинг)
    await asyncio.sleep(2)
    
    # Отправляем тестовое уведомление
    test_message = (
        f"🎉 *ТЕСТОВОЕ УВЕДОМЛЕНИЕ*\n\n"
        f"🛍 Платформа: Ozon (демо-режим)\n"
        f"📱 Название: Сим-карта с балансом 400₽\n"
        f"💰 Цена: {user_settings[user_id]['min'] + 10} ₽\n"
        f"💎 Баланс: 400 ₽\n"
        f"📈 Выгода: {400 - (user_settings[user_id]['min'] + 10)} ₽\n\n"
        f"🔗 [Ссылка на товар](https://www.ozon.ru)\n\n"
        f"⚠️ Это тестовое сообщение. Реальный поиск будет добавлен позже."
    )
    
    await message.answer(test_message, parse_mode="Markdown")
    await msg.edit_text("✅ Проверка завершена!")

@dp.message_handler(commands=['help'])
async def help_cmd(message: types.Message):
    await message.answer(
        f"ℹ️ *Помощь*\n\n"
        f"/start - Запустить бота\n"
        f"/settings - Настройки цен\n"
        f"/check - Ручная проверка\n"
        f"/help - Эта справка\n\n"
        f"💡 *Совет:* Установи диапазон цен 1-60 ₽ для поиска самых выгодных предложений!",
        parse_mode="Markdown"
    )

@dp.message_handler(lambda msg: msg.text and msg.text.isdigit())
async def price_input(message: types.Message):
    user_id = message.from_user.id
    value = int(message.text)
    
    if user_id not in user_settings:
        user_settings[user_id] = {'min': 1, 'max': 60}
    
    # Простая логика: сначала устанавливаем мин., потом макс.
    if 'temp_min' not in user_settings[user_id]:
        user_settings[user_id]['temp_min'] = value
        await message.answer(f"✅ Минимальная цена: {value} ₽\nТеперь введи *максимальную* цену:", parse_mode="Markdown")
    else:
        user_settings[user_id]['min'] = user_settings[user_id]['temp_min']
        user_settings[user_id]['max'] = value
        del user_settings[user_id]['temp_min']
        
        await message.answer(
            f"✅ *Диапазон сохранён!*\n\n"
            f"💰 Мин. цена: {user_settings[user_id]['min']} ₽\n"
            f"💰 Макс. цена: {user_settings[user_id]['max']} ₽\n\n"
            f"🔍 Напиши /check для проверки или /start для меню",
            parse_mode="Markdown"
        )

@dp.callback_query_handler(lambda c: c.data == "reset")
async def reset_callback(callback_query: types.CallbackQuery):
    user_id = callback_query.from_user.id
    user_settings[user_id] = {'min': 1, 'max': 60}
    await bot.send_message(user_id, "🔄 Настройки сброшены до значений по умолчанию (1-60 ₽)")
    await callback_query.answer()

# ========== FLASK ДЛЯ HEALTH CHECK ==========
@flask_app.route('/')
def index():
    return jsonify({
        "status": "ok",
        "bot": "running",
        "users": len(user_settings),
        "timestamp": datetime.now().isoformat()
    })

@flask_app.route('/health')
def health():
    return jsonify({"status": "alive"})

# ========== ЗАПУСК ==========
if __name__ == "__main__":
    from threading import Thread
    
    logger.info(f"🚀 Запуск бота на порту {PORT}")
    
    # Запускаем Flask в отдельном потоке
    def run_flask():
        flask_app.run(host='0.0.0.0', port=PORT, debug=False)
    
    flask_thread = Thread(target=run_flask)
    flask_thread.start()
    
    logger.info("✅ Flask запущен")
    logger.info("🤖 Бот начинает работу...")
    
    # Запускаем бота
    executor.start_polling(dp, skip_updates=True)
