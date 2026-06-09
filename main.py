import os
import logging
import asyncio
import json
from datetime import datetime
from telegram import Update, InlineKeyboardButton, InlineKeyboardMarkup, ReplyKeyboardMarkup, KeyboardButton
from telegram.ext import Application, CommandHandler, MessageHandler, CallbackQueryHandler, ContextTypes, filters
import aiosqlite
from dotenv import load_dotenv

# Загрузка переменных окружения
load_dotenv()

# Настройка логирования
logging.basicConfig(
    format='%(asctime)s - %(name)s - %(levelname)s - %(message)s',
    level=logging.INFO
)
logger = logging.getLogger(__name__)

# Конфигурация
BOT_TOKEN = os.getenv('BOT_TOKEN')
ADMIN_IDS = list(map(int, os.getenv('ADMIN_IDS', '').split(','))) if os.getenv('ADMIN_IDS') else []
DATABASE_PATH = 'proxy_bot.db'

# Словарь для хранения состояний пользователей
user_states = {}

class ProxyBot:
    def __init__(self):
        self.app = None
        
    async def init_db(self):
        """Инициализация базы данных"""
        async with aiosqlite.connect(DATABASE_PATH) as db:
            # Таблица пользователей
            await db.execute('''
                CREATE TABLE IF NOT EXISTS users (
                    user_id INTEGER PRIMARY KEY,
                    username TEXT,
                    first_name TEXT,
                    last_name TEXT,
                    is_admin BOOLEAN DEFAULT 0,
                    is_banned BOOLEAN DEFAULT 0,
                    balance REAL DEFAULT 0.0,
                    created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
                )
            ''')
            
            # Таблица заказов
            await db.execute('''
                CREATE TABLE IF NOT EXISTS orders (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    user_id INTEGER,
                    proxy_type TEXT,
                    quantity INTEGER,
                    country TEXT,
                    duration TEXT,
                    additional_info TEXT,
                    price REAL,
                    status TEXT DEFAULT 'pending',
                    admin_id INTEGER,
                    created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
                    FOREIGN KEY (user_id) REFERENCES users (user_id),
                    FOREIGN KEY (admin_id) REFERENCES users (user_id)
                )
            ''')
            
            # Таблица диалогов
            await db.execute('''
                CREATE TABLE IF NOT EXISTS dialogs (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    order_id INTEGER,
                    user_id INTEGER,
                    admin_id INTEGER,
                    message_text TEXT,
                    sender_type TEXT,
                    created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
                    FOREIGN KEY (order_id) REFERENCES orders (id)
                )
            ''')
            
            # Таблица настроек
            await db.execute('''
                CREATE TABLE IF NOT EXISTS settings (
                    key TEXT PRIMARY KEY,
                    value TEXT
                )
            ''')
            
            # Таблица стран
            await db.execute('''
                CREATE TABLE IF NOT EXISTS countries (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    name TEXT UNIQUE,
                    is_active BOOLEAN DEFAULT 1,
                    price_multiplier REAL DEFAULT 1.0
                )
            ''')
            
            # Таблица пополнений
            await db.execute('''
                CREATE TABLE IF NOT EXISTS topups (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    user_id INTEGER,
                    amount REAL,
                    status TEXT DEFAULT 'pending',
                    created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
                    FOREIGN KEY (user_id) REFERENCES users (user_id)
                )
            ''')
            
            # Добавляем базовые настройки
            await db.execute('''
                INSERT OR IGNORE INTO settings (key, value) VALUES 
                ('orders_enabled', 'true'),
                ('base_price_http', '2.0'),
                ('base_price_socks5', '2.5'),
                ('base_price_residential', '5.0'),
                ('base_price_datacenter', '1.5'),
                ('welcome_message', 'Добро пожаловать! Мы продаем качественные прокси.')
            ''')
            
            # Добавляем базовые страны
            countries = [
                'США', 'Россия', 'Германия', 'Франция', 'Великобритания',
                'Канада', 'Япония', 'Корея', 'Нидерланды', 'Польша'
            ]
            for country in countries:
                await db.execute(
                    'INSERT OR IGNORE INTO countries (name) VALUES (?)', 
                    (country,)
                )
            
            await db.commit()

    async def add_user(self, user_id, username, first_name, last_name):
        """Добавление пользователя"""
        async with aiosqlite.connect(DATABASE_PATH) as db:
            await db.execute('''
                INSERT OR REPLACE INTO users (user_id, username, first_name, last_name, is_admin)
                VALUES (?, ?, ?, ?, ?)
            ''', (user_id, username, first_name, last_name, user_id in ADMIN_IDS))
            await db.commit()

    async def is_user_banned(self, user_id):
        """Проверка на бан"""
        async with aiosqlite.connect(DATABASE_PATH) as db:
            async with db.execute('SELECT is_banned FROM users WHERE user_id = ?', (user_id,)) as cursor:
                result = await cursor.fetchone()
                return result[0] if result else False

    async def are_orders_enabled(self):
        """Проверка доступности заказов"""
        async with aiosqlite.connect(DATABASE_PATH) as db:
            async with db.execute('SELECT value FROM settings WHERE key = ?', ('orders_enabled',)) as cursor:
                result = await cursor.fetchone()
                return result[0] == 'true' if result else True

    async def get_available_countries(self):
        """Получение доступных стран"""
        async with aiosqlite.connect(DATABASE_PATH) as db:
            async with db.execute('SELECT name FROM countries WHERE is_active = 1') as cursor:
                countries = await cursor.fetchall()
                return [country[0] for country in countries]

    async def get_setting(self, key):
        """Получение настройки"""
        async with aiosqlite.connect(DATABASE_PATH) as db:
            async with db.execute('SELECT value FROM settings WHERE key = ?', (key,)) as cursor:
                result = await cursor.fetchone()
                return result[0] if result else None

    async def set_setting(self, key, value):
        """Установка настройки"""
        async with aiosqlite.connect(DATABASE_PATH) as db:
            await db.execute(
                'INSERT OR REPLACE INTO settings (key, value) VALUES (?, ?)', 
                (key, value)
            )
            await db.commit()

    async def get_user_balance(self, user_id):
        """Получение баланса пользователя"""
        async with aiosqlite.connect(DATABASE_PATH) as db:
            async with db.execute('SELECT balance FROM users WHERE user_id = ?', (user_id,)) as cursor:
                result = await cursor.fetchone()
                return result[0] if result else 0.0

    async def update_balance(self, user_id, amount):
        """Обновление баланса пользователя"""
        async with aiosqlite.connect(DATABASE_PATH) as db:
            await db.execute(
                'UPDATE users SET balance = balance + ? WHERE user_id = ?',
                (amount, user_id)
            )
            await db.commit()

    async def calculate_price(self, proxy_type, quantity, duration):
        """Расчет цены"""
        base_price = float(await self.get_setting(f'base_price_{proxy_type}') or '2.0')
        
        duration_multiplier = {
            '1 день': 1,
            '1 неделя': 6,
            '1 месяц': 20,
            '3 месяца': 50
        }
        
        multiplier = duration_multiplier.get(duration, 1)
        return base_price * quantity * multiplier

    async def create_order(self, user_id, proxy_type, quantity, country, duration, additional_info):
        """Создание заказа"""
        price = await self.calculate_price(proxy_type, quantity, duration)
        
        async with aiosqlite.connect(DATABASE_PATH) as db:
            cursor = await db.execute('''
                INSERT INTO orders (user_id, proxy_type, quantity, country, duration, additional_info, price)
                VALUES (?, ?, ?, ?, ?, ?, ?)
            ''', (user_id, proxy_type, quantity, country, duration, additional_info, price))
            order_id = cursor.lastrowid
            
            # Списываем с баланса
            await db.execute(
                'UPDATE users SET balance = balance - ? WHERE user_id = ?',
                (price, user_id)
            )
            
            await db.commit()
            return order_id, price

    async def cancel_order(self, order_id, user_id):
        """Отмена заказа и возврат средств"""
        async with aiosqlite.connect(DATABASE_PATH) as db:
            # Получаем информацию о заказе
            async with db.execute(
                'SELECT price, status FROM orders WHERE id = ? AND user_id = ?',
                (order_id, user_id)
            ) as cursor:
                order = await cursor.fetchone()
                if not order:
                    return False, "Заказ не найден"
                
                price, status = order
                if status != 'pending':
                    return False, f"Нельзя отменить заказ в статусе '{status}'"
                
                # Отменяем заказ
                await db.execute(
                    'UPDATE orders SET status = ? WHERE id = ?',
                    ('cancelled', order_id)
                )
                
                # Возвращаем деньги
                await db.execute(
                    'UPDATE users SET balance = balance + ? WHERE user_id = ?',
                    (price, user_id)
                )
                
                await db.commit()
                return True, price

    async def get_pending_orders(self):
        """Получение новых заказов"""
        async with aiosqlite.connect(DATABASE_PATH) as db:
            async with db.execute('''
                SELECT o.id, o.user_id, u.username, u.first_name, o.proxy_type, 
                       o.quantity, o.country, o.duration, o.price, o.created_at
                FROM orders o
                JOIN users u ON o.user_id = u.user_id
                WHERE o.status = 'pending'
                ORDER BY o.created_at DESC
            ''') as cursor:
                return await cursor.fetchall()

    async def get_order_by_id(self, order_id):
        """Получение заказа по ID"""
        async with aiosqlite.connect(DATABASE_PATH) as db:
            async with db.execute('''
                SELECT o.*, u.username, u.first_name, u.last_name
                FROM orders o
                JOIN users u ON o.user_id = u.user_id
                WHERE o.id = ?
            ''', (order_id,)) as cursor:
                return await cursor.fetchone()

    async def assign_admin_to_order(self, order_id, admin_id):
        """Назначение админа на заказ"""
        async with aiosqlite.connect(DATABASE_PATH) as db:
            await db.execute('''
                UPDATE orders SET admin_id = ?, status = 'in_progress'
                WHERE id = ?
            ''', (admin_id, order_id))
            await db.commit()

    async def get_active_dialog(self, user_id):
        """Получение активного диалога"""
        async with aiosqlite.connect(DATABASE_PATH) as db:
            async with db.execute('''
                SELECT order_id, admin_id FROM orders
                WHERE (user_id = ? OR admin_id = ?) AND status = 'in_progress'
                ORDER BY created_at DESC LIMIT 1
            ''', (user_id, user_id)) as cursor:
                return await cursor.fetchone()

    async def add_dialog_message(self, order_id, user_id, admin_id, message_text, sender_type):
        """Добавление сообщения в диалог"""
        async with aiosqlite.connect(DATABASE_PATH) as db:
            await db.execute('''
                INSERT INTO dialogs (order_id, user_id, admin_id, message_text, sender_type)
                VALUES (?, ?, ?, ?, ?)
            ''', (order_id, user_id, admin_id, message_text, sender_type))
            await db.commit()

    async def close_order(self, order_id, status='completed'):
        """Закрытие заказа"""
        async with aiosqlite.connect(DATABASE_PATH) as db:
            await db.execute('''
                UPDATE orders SET status = ? WHERE id = ?
            ''', (status, order_id))
            await db.commit()

    async def get_user_orders(self, user_id):
        """Получение заказов пользователя"""
        async with aiosqlite.connect(DATABASE_PATH) as db:
            async with db.execute('''
                SELECT id, proxy_type, quantity, country, duration, price, status, created_at
                FROM orders WHERE user_id = ? ORDER BY created_at DESC LIMIT 10
            ''', (user_id,)) as cursor:
                return await cursor.fetchall()

    async def ban_user(self, user_id):
        """Заблокировать пользователя"""
        async with aiosqlite.connect(DATABASE_PATH) as db:
            await db.execute('UPDATE users SET is_banned = 1 WHERE user_id = ?', (user_id,))
            await db.commit()

    async def unban_user(self, user_id):
        """Разблокировать пользователя"""
        async with aiosqlite.connect(DATABASE_PATH) as db:
            await db.execute('UPDATE users SET is_banned = 0 WHERE user_id = ?', (user_id,))
            await db.commit()

    async def add_country(self, country_name):
        """Добавить страну"""
        async with aiosqlite.connect(DATABASE_PATH) as db:
            await db.execute('INSERT OR IGNORE INTO countries (name) VALUES (?)', (country_name,))
            await db.commit()

    async def remove_country(self, country_name):
        """Удалить страну"""
        async with aiosqlite.connect(DATABASE_PATH) as db:
            await db.execute('UPDATE countries SET is_active = 0 WHERE name = ?', (country_name,))
            await db.commit()

    async def get_stats(self):
        """Получение статистики"""
        async with aiosqlite.connect(DATABASE_PATH) as db:
            # Общее количество пользователей
            async with db.execute('SELECT COUNT(*) FROM users') as cursor:
                total_users = (await cursor.fetchone())[0]
            
            # Количество заказов по статусам
            async with db.execute('''
                SELECT status, COUNT(*) FROM orders GROUP BY status
            ''') as cursor:
                orders_by_status = await cursor.fetchall()
            
            # Общий доход
            async with db.execute('''
                SELECT SUM(price) FROM orders WHERE status = 'completed'
            ''') as cursor:
                total_revenue = (await cursor.fetchone())[0] or 0
            
            return {
                'total_users': total_users,
                'orders_by_status': dict(orders_by_status),
                'total_revenue': total_revenue
            }

    async def create_topup_request(self, user_id, amount):
        """Создание запроса на пополнение"""
        async with aiosqlite.connect(DATABASE_PATH) as db:
            cursor = await db.execute(
                'INSERT INTO topups (user_id, amount) VALUES (?, ?)',
                (user_id, amount)
            )
            topup_id = cursor.lastrowid
            await db.commit()
            return topup_id

    async def get_pending_topups(self):
        """Получение ожидающих пополнений"""
        async with aiosqlite.connect(DATABASE_PATH) as db:
            async with db.execute('''
                SELECT t.id, t.user_id, u.username, u.first_name, t.amount, t.created_at
                FROM topups t
                JOIN users u ON t.user_id = u.user_id
                WHERE t.status = 'pending'
                ORDER BY t.created_at DESC
            ''') as cursor:
                return await cursor.fetchall()

    async def approve_topup(self, topup_id):
        """Подтверждение пополнения"""
        async with aiosqlite.connect(DATABASE_PATH) as db:
            async with db.execute(
                'SELECT user_id, amount FROM topups WHERE id = ? AND status = ?',
                (topup_id, 'pending')
            ) as cursor:
                topup = await cursor.fetchone()
                if not topup:
                    return None, None
                
                user_id, amount = topup
                await db.execute(
                    'UPDATE topups SET status = ? WHERE id = ?',
                    ('completed', topup_id)
                )
                await db.execute(
                    'UPDATE users SET balance = balance + ? WHERE user_id = ?',
                    (amount, user_id)
                )
                await db.commit()
                return user_id, amount

    async def reject_topup(self, topup_id):
        """Отклонение пополнения"""
        async with aiosqlite.connect(DATABASE_PATH) as db:
            async with db.execute(
                'SELECT user_id FROM topups WHERE id = ? AND status = ?',
                (topup_id, 'pending')
            ) as cursor:
                topup = await cursor.fetchone()
                if not topup:
                    return None
                
                await db.execute(
                    'UPDATE topups SET status = ? WHERE id = ?',
                    ('rejected', topup_id)
                )
                await db.commit()
                return topup[0]

# Создаем экземпляр бота
bot = ProxyBot()

# Обработчики команд
async def start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user = update.effective_user
    await bot.add_user(user.id, user.username, user.first_name, user.last_name)
    
    # Проверяем бан
    if await bot.is_user_banned(user.id):
        await update.message.reply_text("❌ Вы заблокированы и не можете использовать бота.")
        return
    
    welcome_msg = await bot.get_setting('welcome_message')
    
    if user.id in ADMIN_IDS:
        keyboard = [
            [KeyboardButton("📋 Новые заявки"), KeyboardButton("💬 Активные диалоги")],
            [KeyboardButton("📊 Статистика"), KeyboardButton("⚙️ Настройки")],
            [KeyboardButton("🌍 Управление странами"), KeyboardButton("👥 Пользователи")],
            [KeyboardButton("💰 Пополнения")]
        ]
        reply_markup = ReplyKeyboardMarkup(keyboard, resize_keyboard=True)
        await update.message.reply_text(
            f"🔧 *Админ-панель*\n\nДобро пожаловать, {user.first_name}!\n{welcome_msg}",
            reply_markup=reply_markup,
            parse_mode='Markdown'
        )
    else:
        keyboard = [
            [KeyboardButton("🛒 Заказать прокси"), KeyboardButton("📞 Мои заказы")],
            [KeyboardButton("💳 Баланс"), KeyboardButton("💰 Пополнить")],
            [KeyboardButton("ℹ️ Информация"), KeyboardButton("💰 Цены")]
        ]
        reply_markup = ReplyKeyboardMarkup(keyboard, resize_keyboard=True)
        await update.message.reply_text(
            f"👋 *{welcome_msg}*\n\nВыберите действие:",
            reply_markup=reply_markup,
            parse_mode='Markdown'
        )

async def handle_message(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user = update.effective_user
    text = update.message.text
    
    # Проверяем бан
    if await bot.is_user_banned(user.id):
        await update.message.reply_text("❌ Вы заблокированы.")
        return
    
    # Обработка состояний
    if user.id in user_states:
        state = user_states[user.id]
        await handle_state(update, context, state)
        return
    
    # Главное меню для админов
    if user.id in ADMIN_IDS:
        if text == "📋 Новые заявки":
            orders = await bot.get_pending_orders()
            if not orders:
                await update.message.reply_text("📭 Новых заявок нет.")
                return
            
            for order in orders:
                order_id, user_id, username, first_name, proxy_type, quantity, country, duration, price, created_at = order
                message = f"📋 *Заявка #{order_id}*\n"
                message += f"👤 {first_name} (@{username})\n"
                message += f"🆔 ID: `{user_id}`\n"
                message += f"📦 Тип: {proxy_type}\n"
                message += f"🔢 Количество: {quantity}\n"
                message += f"🌍 Страна: {country}\n"
                message += f"⏱ Срок: {duration}\n"
                message += f"💰 Цена: ${price:.2f}\n"
                message += f"📅 {created_at}"
                
                keyboard = [
                    [
                        InlineKeyboardButton("✅ Взять", callback_data=f"take_{order_id}"),
                        InlineKeyboardButton("❌ Отклонить", callback_data=f"reject_{order_id}")
                    ]
                ]
                reply_markup = InlineKeyboardMarkup(keyboard)
                await update.message.reply_text(message, reply_markup=reply_markup, parse_mode='Markdown')
        
        elif text == "💬 Активные диалоги":
            active = await bot.get_active_dialog(user.id)
            if active:
                await update.message.reply_text(
                    f"💬 У вас есть активный диалог по заказу #{active[0]}\n"
                    "Просто отправьте сообщение и оно будет переслано клиенту."
                )
            else:
                await update.message.reply_text("📭 Нет активных диалогов.")
        
        elif text == "📊 Статистика":
            stats = await bot.get_stats()
            message = "📊 *Статистика*\n\n"
            message += f"👥 Всего пользователей: {stats['total_users']}\n"
            message += f"📦 Заказов:\n"
            for status, count in stats['orders_by_status'].items():
                message += f"  • {status}: {count}\n"
            message += f"💰 Общий доход: ${stats['total_revenue']:.2f}"
            await update.message.reply_text(message, parse_mode='Markdown')
        
        elif text == "⚙️ Настройки":
            orders_enabled = await bot.get_setting('orders_enabled')
            status = "✅ Включены" if orders_enabled == 'true' else "❌ Отключены"
            
            keyboard = [
                [InlineKeyboardButton("🔄 Переключить заказы", callback_data="toggle_orders")],
                [InlineKeyboardButton("✏️ Изменить приветствие", callback_data="edit_welcome")],
                [InlineKeyboardButton("💵 Изменить цены", callback_data="edit_prices")]
            ]
            reply_markup = InlineKeyboardMarkup(keyboard)
            await update.message.reply_text(
                f"⚙️ *Настройки*\n\nЗаказы: {status}",
                reply_markup=reply_markup,
                parse_mode='Markdown'
            )
        
        elif text == "🌍 Управление странами":
            countries = await bot.get_available_countries()
            message = "🌍 *Доступные страны:*\n\n" + "\n".join(countries)
            keyboard = [
                [InlineKeyboardButton("➕ Добавить", callback_data="add_country")],
                [InlineKeyboardButton("➖ Удалить", callback_data="remove_country")]
            ]
            reply_markup = InlineKeyboardMarkup(keyboard)
            await update.message.reply_text(message, reply_markup=reply_markup, parse_mode='Markdown')
        
        elif text == "👥 Пользователи":
            await update.message.reply_text(
                "Введите ID пользователя для управления:",
                reply_markup=ReplyKeyboardMarkup([["🔙 Назад"]], resize_keyboard=True)
            )
            user_states[user.id] = {"action": "manage_user"}
        
        elif text == "💰 Пополнения":
            topups = await bot.get_pending_topups()
            if not topups:
                await update.message.reply_text("📭 Нет ожидающих пополнений.")
                return
            
            for topup in topups:
                topup_id, user_id, username, first_name, amount, created_at = topup
                message = f"💰 *Пополнение #{topup_id}*\n"
                message += f"👤 {first_name} (@{username})\n"
                message += f"💵 Сумма: ${amount:.2f}\n"
                message += f"📅 {created_at}"
                
                keyboard = [
                    [
                        InlineKeyboardButton("✅ Подтвердить", callback_data=f"approve_topup_{topup_id}"),
                        InlineKeyboardButton("❌ Отклонить", callback_data=f"reject_topup_{topup_id}")
                    ]
                ]
                reply_markup = InlineKeyboardMarkup(keyboard)
                await update.message.reply_text(message, reply_markup=reply_markup, parse_mode='Markdown')
        
        elif text == "🔙 Назад":
            keyboard = [
                [KeyboardButton("📋 Новые заявки"), KeyboardButton("💬 Активные диалоги")],
                [KeyboardButton("📊 Статистика"), KeyboardButton("⚙️ Настройки")],
                [KeyboardButton("🌍 Управление странами"), KeyboardButton("👥 Пользователи")],
                [KeyboardButton("💰 Пополнения")]
            ]
            reply_markup = ReplyKeyboardMarkup(keyboard, resize_keyboard=True)
            await update.message.reply_text("🔧 Админ-панель", reply_markup=reply_markup)
    
    else:
        # Клиентское меню
        if text == "🛒 Заказать прокси":
            if not await bot.are_orders_enabled():
                await update.message.reply_text("❌ Заказы временно недоступны.")
                return
            
            user_states[user.id] = {"action": "order", "step": "type"}
            
            keyboard = [
                [InlineKeyboardButton("HTTP", callback_data="type_http")],
                [InlineKeyboardButton("SOCKS5", callback_data="type_socks5")],
                [InlineKeyboardButton("Residential", callback_data="type_residential")],
                [InlineKeyboardButton("Datacenter", callback_data="type_datacenter")]
            ]
            reply_markup = InlineKeyboardMarkup(keyboard)
            await update.message.reply_text("📦 Выберите тип прокси:", reply_markup=reply_markup)
        
        elif text == "📞 Мои заказы":
            orders = await bot.get_user_orders(user.id)
            if not orders:
                await update.message.reply_text("📭 У вас нет заказов.")
                return
            
            for order in orders:
                order_id, proxy_type, quantity, country, duration, price, status, created_at = order
                status_emoji = {
                    'pending': '⏳',
                    'in_progress': '🔄',
                    'completed': '✅',
                    'cancelled': '❌',
                    'rejected': '🚫'
                }
                emoji = status_emoji.get(status, '❓')
                
                message = f"{emoji} *Заказ #{order_id}*\n"
                message += f"📦 {proxy_type} x{quantity}\n"
                message += f"🌍 {country}\n"
                message += f"⏱ {duration}\n"
                message += f"💰 ${price:.2f}\n"
                message += f"📅 {created_at}"
                
                keyboard = None
                if status == 'pending':
                    keyboard = [[InlineKeyboardButton("❌ Отменить", callback_data=f"cancel_order_{order_id}")]]
                
                reply_markup = InlineKeyboardMarkup(keyboard) if keyboard else None
                await update.message.reply_text(message, reply_markup=reply_markup, parse_mode='Markdown')
        
        elif text == "💳 Баланс":
            balance = await bot.get_user_balance(user.id)
            await update.message.reply_text(f"💳 *Ваш баланс: ${balance:.2f}*", parse_mode='Markdown')
        
        elif text == "💰 Пополнить":
            user_states[user.id] = {"action": "topup"}
            await update.message.reply_text(
                "💰 Введите сумму пополнения в USD (минимум $1):",
                reply_markup=ReplyKeyboardMarkup([["🔙 Отмена"]], resize_keyboard=True)
            )
        
        elif text == "ℹ️ Информация":
            message = "ℹ️ *О нас*\n\n"
            message += "Мы предоставляем качественные прокси для различных задач.\n\n"
            message += "*Типы прокси:*\n"
            message += "• HTTP\n"
            message += "• SOCKS5\n"
            message += "• Residential\n"
            message += "• Datacenter\n\n"
            message += "По всем вопросам обращайтесь к администратору."
            await update.message.reply_text(message, parse_mode='Markdown')
        
        elif text == "💰 Цены":
            http_price = await bot.get_setting('base_price_http') or '2.0'
            socks5_price = await bot.get_setting('base_price_socks5') or '2.5'
            residential_price = await bot.get_setting('base_price_residential') or '5.0'
            datacenter_price = await bot.get_setting('base_price_datacenter') or '1.5'
            
            message = "💰 *Цены (за 1 день):*\n\n"
            message += f"• HTTP: ${http_price}\n"
            message += f"• SOCKS5: ${socks5_price}\n"
            message += f"• Residential: ${residential_price}\n"
            message += f"• Datacenter: ${datacenter_price}\n\n"
            message += "*Сроки (множитель):*\n"
            message += "• 1 день: x1\n"
            message += "• 1 неделя: x6\n"
            message += "• 1 месяц: x20\n"
            message += "• 3 месяца: x50"
            await update.message.reply_text(message, parse_mode='Markdown')

async def handle_state(update: Update, context: ContextTypes.DEFAULT_TYPE, state):
    user = update.effective_user
    text = update.message.text
    
    if text == "🔙 Отмена" or text == "🔙 Назад":
        del user_states[user.id]
        await start(update, context)
        return
    
    if state["action"] == "manage_user":
        try:
            target_id = int(text)
            keyboard = [
                [InlineKeyboardButton("🔨 Забанить", callback_data=f"ban_{target_id}")],
                [InlineKeyboardButton("✅ Разбанить", callback_data=f"unban_{target_id}")]
            ]
            reply_markup = InlineKeyboardMarkup(keyboard)
            await update.message.reply_text(
                f"👤 Управление пользователем `{target_id}`",
                reply_markup=reply_markup,
                parse_mode='Markdown'
            )
        except ValueError:
            await update.message.reply_text("❌ Неверный ID. Введите число:")
    
    elif state["action"] == "topup":
        try:
            amount = float(text)
            if amount < 1:
                await update.message.reply_text("❌ Минимальная сумма $1. Введите сумму:")
                return
            
            topup_id = await bot.create_topup_request(user.id, amount)
            del user_states[user.id]
            
            await update.message.reply_text(
                f"✅ Заявка на пополнение #{topup_id} создана!\n"
                f"💵 Сумма: ${amount:.2f}\n"
                "⏳ Ожидайте подтверждения от администратора.",
                reply_markup=ReplyKeyboardMarkup([
                    ["🛒 Заказать прокси", "📞 Мои заказы"],
                    ["💳 Баланс", "💰 Пополнить"],
                    ["ℹ️ Информация", "💰 Цены"]
                ], resize_keyboard=True)
            )
        except ValueError:
            await update.message.reply_text("❌ Введите число:")

async def handle_callback(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    user = query.from_user
    data = query.data
    
    await query.answer()
    
    # Админские callback'и
    if data.startswith("take_"):
        order_id = int(data.split("_")[1])
        await bot.assign_admin_to_order(order_id, user.id)
        await query.edit_message_text(
            f"{query.message.text}\n\n✅ *Взято админом @{user.username}*",
            parse_mode='Markdown'
        )
        # Уведомляем клиента
        order = await bot.get_order_by_id(order_id)
        if order:
            await context.bot.send_message(
                order[1],
                f"✅ Ваш заказ #{order_id} взят в работу!\n"
                "Вы можете общаться с администратором через этот чат."
            )
    
    elif data.startswith("reject_"):
        order_id = int(data.split("_")[1])
        await bot.close_order(order_id, 'rejected')
        # Возвращаем деньги
        order = await bot.get_order_by_id(order_id)
        if order:
            await bot.update_balance(order[1], order[9])
            await context.bot.send_message(
                order[1],
                f"❌ Ваш заказ #{order_id} отклонен. Средства возвращены на баланс."
            )
        await query.edit_message_text(
            f"{query.message.text}\n\n❌ *Отклонено*",
            parse_mode='Markdown'
        )
    
    elif data.startswith("cancel_order_"):
        order_id = int(data.split("_")[2])
        success, result = await bot.cancel_order(order_id, user.id)
        if success:
            await query.edit_message_text(
                f"{query.message.text}\n\n❌ *Отменено. Возвращено ${result:.2f}*",
                parse_mode='Markdown'
            )
        else:
            await query.answer(f"❌ {result}", show_alert=True)
    
    elif data.startswith("ban_"):
        target_id = int(data.split("_")[1])
        await bot.ban_user(target_id)
        await query.edit_message_text(
            f"✅ Пользователь `{target_id}` забанен.",
            parse_mode='Markdown'
        )
    
    elif data.startswith("unban_"):
        target_id = int(data.split("_")[1])
        await bot.unban_user(target_id)
        await query.edit_message_text(
            f"✅ Пользователь `{target_id}` разбанен.",
            parse_mode='Markdown'
        )
    
    elif data.startswith("approve_topup_"):
        topup_id = int(data.split("_")[2])
        result = await bot.approve_topup(topup_id)
        if result[0]:
            user_id, amount = result
            await query.edit_message_text(
                f"{query.message.text}\n\n✅ *Подтверждено*",
                parse_mode='Markdown'
            )
            await context.bot.send_message(
                user_id,
                f"✅ Ваше пополнение на ${amount:.2f} подтверждено!\n"
                "Средства зачислены на баланс."
            )
        else:
            await query.answer("❌ Заявка не найдена", show_alert=True)
    
    elif data.startswith("reject_topup_"):
        topup_id = int(data.split("_")[2])
        user_id = await bot.reject_topup(topup_id)
        if user_id:
            await query.edit_message_text(
                f"{query.message.text}\n\n❌ *Отклонено*",
                parse_mode='Markdown'
            )
            await context.bot.send_message(
                user_id,
                "❌ Ваша заявка на пополнение отклонена."
            )
        else:
            await query.answer("❌ Заявка не найдена", show_alert=True)
    
    elif data == "toggle_orders":
        current = await bot.get_setting('orders_enabled')
        new_value = 'false' if current == 'true' else 'true'
        await bot.set_setting('orders_enabled', new_value)
        status = "✅ Включены" if new_value == 'true' else "❌ Отключены"
        await query.edit_message_text(f"⚙️ Заказы: {status}")
    
    elif data == "edit_welcome":
        user_states[user.id] = {"action": "edit_welcome"}
        await query.edit_message_text("Введите новое приветственное сообщение:")
    
    elif data == "edit_prices":
        user_states[user.id] = {"action": "edit_prices", "step": "type"}
        keyboard = [
            [InlineKeyboardButton("HTTP", callback_data="price_http")],
            [InlineKeyboardButton("SOCKS5", callback_data="price_socks5")],
            [InlineKeyboardButton("Residential", callback_data="price_residential")],
            [InlineKeyboardButton("Datacenter", callback_data="price_datacenter")]
        ]
        reply_markup = InlineKeyboardMarkup(keyboard)
        await query.edit_message_text("Выберите тип для изменения цены:", reply_markup=reply_markup)
    
    elif data.startswith("price_"):
        proxy_type = data.split("_")[1]
        user_states[user.id] = {"action": "edit_price_value", "type": proxy_type}
        await query.edit_message_text(f"Введите новую цену для {proxy_type}:")
    
    elif data == "add_country":
        user_states[user.id] = {"action": "add_country"}
        await query.edit_message_text("Введите название страны для добавления:")
    
    elif data == "remove_country":
        user_states[user.id] = {"action": "remove_country"}
        await query.edit_message_text("Введите название страны для удаления:")
    
    # Клиентские callback'и
    elif data.startswith("type_"):
        proxy_type = data.split("_")[1]
        user_states[user.id] = {"action": "order", "step": "quantity", "type": proxy_type}
        await query.edit_message_text(
            f"📦 Тип: *{proxy_type}*\n\nВведите количество (от 1 до 100):",
            parse_mode='Markdown'
        )
    
    elif data.startswith("country_"):
        country = data.split("_")[1]
        user_states[user.id] = {"action": "order", "step": "country", "type": country}
        await query.edit_message_text(
            f"🌍 Страна: *{country}*\n\nВведите название страны:",
            parse_mode='Markdown'
        )

async def handle_state_text(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user = update.effective_user
    text = update.message.text
    
    if user.id not in user_states:
        return
    
    state = user_states[user.id]
    
    if text == "🔙 Отмена" or text == "🔙 Назад":
        del user_states[user.id]
        await start(update, context)
        return
    
    if state["action"] == "edit_welcome":
        await bot.set_setting('welcome_message', text)
        del user_states[user.id]
        await update.message.reply_text("✅ Приветствие обновлено!")
    
    elif state["action"] == "edit_price_value":
        try:
            price = float(text)
            proxy_type = state["type"]
            await bot.set_setting(f'base_price_{proxy_type}', str(price))
            del user_states[user.id]
            await update.message.reply_text(f"✅ Цена для {proxy_type} обновлена: ${price}")
        except ValueError:
            await update.message.reply_text("❌ Введите число:")
    
    elif state["action"] == "add_country":
        await bot.add_country(text)
        del user_states[user.id]
        await update.message.reply_text(f"✅ Страна '{text}' добавлена!")
    
    elif state["action"] == "remove_country":
        await bot.remove_country(text)
        del user_states[user.id]
        await update.message.reply_text(f"✅ Страна '{text}' удалена!")
    
    elif state["action"] == "order":
        if state["step"] == "quantity":
            try:
                quantity = int(text)
                if quantity < 1 or quantity > 100:
                    await update.message.reply_text("❌ От 1 до 100:")
                    return
                state["quantity"] = quantity
                state["step"] = "country"
                
                countries = await bot.get_available_countries()
                keyboard = []
                for country in countries:
                    keyboard.append([InlineKeyboardButton(country, callback_data=f"country_{country}")])
                
                reply_markup = InlineKeyboardMarkup(keyboard)
                await update.message.reply_text("🌍 Выберите страну:", reply_markup=reply_markup)
            except ValueError:
                await update.message.reply_text("❌ Введите число:")
        
        elif state["step"] == "country":
            state["country"] = text
            state["step"] = "duration"
            
            keyboard = [
                [InlineKeyboardButton("1 день", callback_data="duration_1 день")],
                [InlineKeyboardButton("1 неделя", callback_data="duration_1 неделя")],
                [InlineKeyboardButton("1 месяц", callback_data="duration_1 месяц")],
                [InlineKeyboardButton("3 месяца", callback_data="duration_3 месяца")]
            ]
            reply_markup = InlineKeyboardMarkup(keyboard)
            await update.message.reply_text("⏱ Выберите срок:", reply_markup=reply_markup)

async def handle_callback_order(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    user = query.from_user
    data = query.data
    
    if user.id not in user_states:
        return
    
    state = user_states[user.id]
    
    await query.answer()
    
    if data.startswith("country_"):
        country = data.split("_")[1]
        state["country"] = country
        state["step"] = "duration"
        
        keyboard = [
            [InlineKeyboardButton("1 день", callback_data="duration_1 день")],
            [InlineKeyboardButton("1 неделя", callback_data="duration_1 неделя")],
            [InlineKeyboardButton("1 месяц", callback_data="duration_1 месяц")],
            [InlineKeyboardButton("3 месяца", callback_data="duration_3 месяца")]
        ]
        reply_markup = InlineKeyboardMarkup(keyboard)
        await query.edit_message_text("⏱ Выберите срок:", reply_markup=reply_markup)
    
    elif data.startswith("duration_"):
        duration = data.split("_")[1]
        state["duration"] = duration
        state["step"] = "additional"
        await query.edit_message_text(
            "📝 Введите дополнительную информацию (или '-' если нет):\n\n"
            "Например: нужны прокси с высоким trust score"
        )

async def handle_additional_info(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user = update.effective_user
    text = update.message.text
    
    if user.id not in user_states:
        return
    
    state = user_states[user.id]
    
    if state["action"] == "order" and state["step"] == "additional":
        additional = text if text != "-" else ""
        
        # Проверяем баланс
        balance = await bot.get_user_balance(user.id)
        price = await bot.calculate_price(state["type"], state["quantity"], state["duration"])
        
        if balance < price:
            del user_states[user.id]
            await update.message.reply_text(
                f"❌ Недостаточно средств!\n"
                f"💳 Баланс: ${balance:.2f}\n"
                f"💰 Стоимость: ${price:.2f}\n\n"
                f"Пополните баланс: /start → 💰 Пополнить",
                reply_markup=ReplyKeyboardMarkup([
                    ["🛒 Заказать прокси", "📞 Мои заказы"],
                    ["💳 Баланс", "💰 Пополнить"],
                    ["ℹ️ Информация", "💰 Цены"]
                ], resize_keyboard=True)
            )
            return
        
        order_id, price = await bot.create_order(
            user.id, state["type"], state["quantity"],
            state["country"], state["duration"], additional
        )
        
        del user_states[user.id]
        
        message = f"✅ *Заказ #{order_id} создан!*\n\n"
        message += f"📦 Тип: {state['type']}\n"
        message += f"🔢 Количество: {state['quantity']}\n"
        message += f"🌍 Страна: {state['country']}\n"
        message += f"⏱ Срок: {state['duration']}\n"
        message += f"💰 Списано: ${price:.2f}\n"
        message += f"💳 Остаток: ${balance - price:.2f}\n\n"
        message += "⏳ Ожидайте, администратор скоро свяжется с вами!"
        
        await update.message.reply_text(
            message,
            reply_markup=ReplyKeyboardMarkup([
                ["🛒 Заказать прокси", "📞 Мои заказы"],
                ["💳 Баланс", "💰 Пополнить"],
                ["ℹ️ Информация", "💰 Цены"]
            ], resize_keyboard=True),
            parse_mode='Markdown'
        )

async def main():
    """Запуск бота"""
    await bot.init_db()
    
    application = Application.builder().token(BOT_TOKEN).build()
    
    # Регистрация обработчиков
    application.add_handler(CommandHandler("start", start))
    application.add_handler(CallbackQueryHandler(handle_callback, pattern="^(take_|reject_|cancel_order_|ban_|unban_|approve_topup_|reject_topup_|toggle_orders|edit_welcome|edit_prices|price_|add_country|remove_country)"))
    application.add_handler(CallbackQueryHandler(handle_callback_order, pattern="^(country_|duration_)"))
    application.add_handler(MessageHandler(filters.TEXT & ~filters.COMMAND, handle_additional_info), group=1)
    application.add_handler(MessageHandler(filters.TEXT & ~filters.COMMAND, handle_state_text), group=2)
    application.add_handler(MessageHandler(filters.TEXT & ~filters.COMMAND, handle_message), group=3)
    
    # Запуск
    logger.info("Бот запущен")
    await application.run_polling()

if __name__ == '__main__':
    asyncio.run(main())
