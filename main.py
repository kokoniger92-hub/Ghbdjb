import os
import sys
import logging
import asyncio
import time
from datetime import datetime
from telegram import Update, InlineKeyboardButton, InlineKeyboardMarkup, ReplyKeyboardMarkup, KeyboardButton
from telegram.ext import Application, CommandHandler, MessageHandler, CallbackQueryHandler, ContextTypes, filters
import aiosqlite
from dotenv import load_dotenv

# Загрузка переменных окружения
load_dotenv()

# Настройка логирования (меньше логов = быстрее)
logging.basicConfig(
    format='%(asctime)s - %(levelname)s - %(message)s',
    level=logging.WARNING  # Только предупреждения и ошибки
)
logger = logging.getLogger(__name__)

# Конфигурация
BOT_TOKEN = os.getenv('BOT_TOKEN')
if not BOT_TOKEN:
    logger.error("BOT_TOKEN не найден!")
    sys.exit(1)

ADMIN_IDS = list(map(int, os.getenv('ADMIN_IDS', '').split(','))) if os.getenv('ADMIN_IDS') else []
DATABASE_PATH = '/tmp/proxy_bot.db'  # Используем /tmp для скорости на Render

# Кэш для ускорения
user_states = {}
cache = {
    'settings': {},
    'countries': [],
    'last_update': 0
}
CACHE_TTL = 30  # Кэш на 30 секунд

class ProxyBot:
    def __init__(self):
        self.db_path = DATABASE_PATH
        
    async def init_db(self):
        """Инициализация базы данных"""
        async with aiosqlite.connect(self.db_path) as db:
            # Включаем WAL режим для скорости
            await db.execute('PRAGMA journal_mode=WAL')
            await db.execute('PRAGMA synchronous=OFF')
            await db.execute('PRAGMA cache_size=4000')
            
            await db.executescript('''
                CREATE TABLE IF NOT EXISTS users (
                    user_id INTEGER PRIMARY KEY,
                    username TEXT,
                    first_name TEXT,
                    last_name TEXT,
                    is_admin BOOLEAN DEFAULT 0,
                    is_banned BOOLEAN DEFAULT 0,
                    balance REAL DEFAULT 0.0,
                    created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
                );
                
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
                    created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
                );
                
                CREATE TABLE IF NOT EXISTS settings (
                    key TEXT PRIMARY KEY,
                    value TEXT
                );
                
                CREATE TABLE IF NOT EXISTS countries (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    name TEXT UNIQUE,
                    is_active BOOLEAN DEFAULT 1
                );
                
                CREATE TABLE IF NOT EXISTS topups (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    user_id INTEGER,
                    amount REAL,
                    status TEXT DEFAULT 'pending',
                    created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
                );
                
                INSERT OR IGNORE INTO settings (key, value) VALUES 
                ('orders_enabled', 'true'),
                ('base_price_http', '2.0'),
                ('base_price_socks5', '2.5'),
                ('base_price_residential', '5.0'),
                ('base_price_datacenter', '1.5'),
                ('welcome_message', 'Добро пожаловать! Мы продаем качественные прокси.');
            ''')
            
            # Добавляем страны
            countries = ['США', 'Россия', 'Германия', 'Франция', 'Великобритания',
                        'Канада', 'Япония', 'Корея', 'Нидерланды', 'Польша']
            for country in countries:
                await db.execute('INSERT OR IGNORE INTO countries (name) VALUES (?)', (country,))
            
            await db.commit()

    def _get_cached(self, key):
        """Получение из кэша"""
        if time.time() - cache['last_update'] > CACHE_TTL:
            cache['settings'] = {}
            cache['countries'] = []
            cache['last_update'] = time.time()
        return cache.get(key)

    async def add_user(self, user_id, username, first_name, last_name):
        async with aiosqlite.connect(self.db_path) as db:
            await db.execute(
                'INSERT OR REPLACE INTO users (user_id, username, first_name, last_name, is_admin) VALUES (?, ?, ?, ?, ?)',
                (user_id, username or '', first_name or '', last_name or '', user_id in ADMIN_IDS)
            )
            await db.commit()

    async def is_user_banned(self, user_id):
        async with aiosqlite.connect(self.db_path) as db:
            cursor = await db.execute('SELECT is_banned FROM users WHERE user_id = ?', (user_id,))
            result = await cursor.fetchone()
            return result[0] if result else False

    async def get_setting(self, key):
        cached = self._get_cached('settings')
        if key in cached:
            return cached[key]
            
        async with aiosqlite.connect(self.db_path) as db:
            cursor = await db.execute('SELECT value FROM settings WHERE key = ?', (key,))
            result = await cursor.fetchone()
            value = result[0] if result else None
            if value:
                cache['settings'][key] = value
            return value

    async def set_setting(self, key, value):
        async with aiosqlite.connect(self.db_path) as db:
            await db.execute('INSERT OR REPLACE INTO settings VALUES (?, ?)', (key, value))
            await db.commit()
            cache['settings'][key] = value

    async def get_available_countries(self):
        countries = self._get_cached('countries')
        if countries:
            return countries
            
        async with aiosqlite.connect(self.db_path) as db:
            cursor = await db.execute('SELECT name FROM countries WHERE is_active = 1')
            countries = [row[0] for row in await cursor.fetchall()]
            cache['countries'] = countries
            return countries

    async def get_user_balance(self, user_id):
        async with aiosqlite.connect(self.db_path) as db:
            cursor = await db.execute('SELECT balance FROM users WHERE user_id = ?', (user_id,))
            result = await cursor.fetchone()
            return result[0] if result else 0.0

    async def calculate_price(self, proxy_type, quantity, duration):
        base_price = float(await self.get_setting(f'base_price_{proxy_type}') or '2.0')
        multipliers = {'1 день': 1, '1 неделя': 6, '1 месяц': 20, '3 месяца': 50}
        return base_price * quantity * multipliers.get(duration, 1)

    async def create_order(self, user_id, proxy_type, quantity, country, duration, additional_info):
        price = await self.calculate_price(proxy_type, quantity, duration)
        async with aiosqlite.connect(self.db_path) as db:
            cursor = await db.execute(
                'INSERT INTO orders (user_id, proxy_type, quantity, country, duration, additional_info, price) VALUES (?, ?, ?, ?, ?, ?, ?)',
                (user_id, proxy_type, quantity, country, duration, additional_info or '', price)
            )
            order_id = cursor.lastrowid
            await db.execute('UPDATE users SET balance = balance - ? WHERE user_id = ?', (price, user_id))
            await db.commit()
            return order_id, price

    async def cancel_order(self, order_id, user_id):
        async with aiosqlite.connect(self.db_path) as db:
            cursor = await db.execute('SELECT price, status FROM orders WHERE id = ? AND user_id = ?', (order_id, user_id))
            order = await cursor.fetchone()
            if not order:
                return False, "Заказ не найден"
            
            price, status = order
            if status != 'pending':
                return False, f"Нельзя отменить заказ в статусе '{status}'"
            
            await db.execute("UPDATE orders SET status = 'cancelled' WHERE id = ?", (order_id,))
            await db.execute('UPDATE users SET balance = balance + ? WHERE user_id = ?', (price, user_id))
            await db.commit()
            return True, price

    async def get_pending_orders(self):
        async with aiosqlite.connect(self.db_path) as db:
            cursor = await db.execute('''
                SELECT o.id, o.user_id, u.username, u.first_name, o.proxy_type, 
                       o.quantity, o.country, o.duration, o.price, o.created_at
                FROM orders o JOIN users u ON o.user_id = u.user_id
                WHERE o.status = 'pending' ORDER BY o.created_at DESC LIMIT 10
            ''')
            return await cursor.fetchall()

    async def get_order_by_id(self, order_id):
        async with aiosqlite.connect(self.db_path) as db:
            cursor = await db.execute('SELECT o.*, u.username FROM orders o JOIN users u ON o.user_id = u.user_id WHERE o.id = ?', (order_id,))
            return await cursor.fetchone()

    async def assign_admin_to_order(self, order_id, admin_id):
        async with aiosqlite.connect(self.db_path) as db:
            await db.execute("UPDATE orders SET admin_id = ?, status = 'in_progress' WHERE id = ?", (admin_id, order_id))
            await db.commit()

    async def close_order(self, order_id, status='completed'):
        async with aiosqlite.connect(self.db_path) as db:
            await db.execute('UPDATE orders SET status = ? WHERE id = ?', (status, order_id))
            await db.commit()

    async def get_user_orders(self, user_id):
        async with aiosqlite.connect(self.db_path) as db:
            cursor = await db.execute(
                'SELECT id, proxy_type, quantity, country, duration, price, status, created_at FROM orders WHERE user_id = ? ORDER BY created_at DESC LIMIT 10',
                (user_id,)
            )
            return await cursor.fetchall()

    async def ban_user(self, user_id):
        async with aiosqlite.connect(self.db_path) as db:
            await db.execute('UPDATE users SET is_banned = 1 WHERE user_id = ?', (user_id,))
            await db.commit()

    async def unban_user(self, user_id):
        async with aiosqlite.connect(self.db_path) as db:
            await db.execute('UPDATE users SET is_banned = 0 WHERE user_id = ?', (user_id,))
            await db.commit()

    async def add_country(self, name):
        async with aiosqlite.connect(self.db_path) as db:
            await db.execute('INSERT OR IGNORE INTO countries (name) VALUES (?)', (name,))
            await db.commit()
            cache['countries'] = []

    async def remove_country(self, name):
        async with aiosqlite.connect(self.db_path) as db:
            await db.execute('UPDATE countries SET is_active = 0 WHERE name = ?', (name,))
            await db.commit()
            cache['countries'] = []

    async def get_stats(self):
        async with aiosqlite.connect(self.db_path) as db:
            cursor = await db.execute('SELECT COUNT(*) FROM users')
            total_users = (await cursor.fetchone())[0]
            
            cursor = await db.execute("SELECT COUNT(*) FROM orders WHERE status = 'completed'")
            completed = (await cursor.fetchone())[0]
            
            cursor = await db.execute("SELECT COUNT(*) FROM orders WHERE status = 'pending'")
            pending = (await cursor.fetchone())[0]
            
            cursor = await db.execute("SELECT SUM(price) FROM orders WHERE status = 'completed'")
            revenue = (await cursor.fetchone())[0] or 0
            
            return {
                'total_users': total_users,
                'completed': completed,
                'pending': pending,
                'total_revenue': revenue
            }

    async def create_topup_request(self, user_id, amount):
        async with aiosqlite.connect(self.db_path) as db:
            cursor = await db.execute('INSERT INTO topups (user_id, amount) VALUES (?, ?)', (user_id, amount))
            topup_id = cursor.lastrowid
            await db.commit()
            return topup_id

    async def get_pending_topups(self):
        async with aiosqlite.connect(self.db_path) as db:
            cursor = await db.execute('''
                SELECT t.id, t.user_id, u.username, u.first_name, t.amount, t.created_at
                FROM topups t JOIN users u ON t.user_id = u.user_id
                WHERE t.status = 'pending' ORDER BY t.created_at DESC LIMIT 10
            ''')
            return await cursor.fetchall()

    async def approve_topup(self, topup_id):
        async with aiosqlite.connect(self.db_path) as db:
            cursor = await db.execute('SELECT user_id, amount FROM topups WHERE id = ? AND status = ?', (topup_id, 'pending'))
            topup = await cursor.fetchone()
            if not topup:
                return None, None
            
            user_id, amount = topup
            await db.execute("UPDATE topups SET status = 'completed' WHERE id = ?", (topup_id,))
            await db.execute('UPDATE users SET balance = balance + ? WHERE user_id = ?', (amount, user_id))
            await db.commit()
            return user_id, amount

    async def reject_topup(self, topup_id):
        async with aiosqlite.connect(self.db_path) as db:
            cursor = await db.execute('SELECT user_id FROM topups WHERE id = ? AND status = ?', (topup_id, 'pending'))
            topup = await cursor.fetchone()
            if not topup:
                return None
            await db.execute("UPDATE topups SET status = 'rejected' WHERE id = ?", (topup_id,))
            await db.commit()
            return topup[0]

bot = ProxyBot()

# Быстрые клавиатуры (создаем один раз)
ADMIN_KEYBOARD = ReplyKeyboardMarkup([
    [KeyboardButton("📋 Новые заявки"), KeyboardButton("📊 Статистика")],
    [KeyboardButton("👥 Пользователи"), KeyboardButton("💰 Пополнения")],
    [KeyboardButton("⚙️ Настройки")]
], resize_keyboard=True)

USER_KEYBOARD = ReplyKeyboardMarkup([
    [KeyboardButton("🛒 Заказать прокси"), KeyboardButton("📞 Мои заказы")],
    [KeyboardButton("💳 Баланс"), KeyboardButton("💰 Пополнить")],
    [KeyboardButton("ℹ️ Информация"), KeyboardButton("💰 Цены")]
], resize_keyboard=True)

BACK_KEYBOARD = ReplyKeyboardMarkup([[KeyboardButton("🔙 Отмена")]], resize_keyboard=True)

async def start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user = update.effective_user
    await bot.add_user(user.id, user.username, user.first_name, user.last_name)
    
    if await bot.is_user_banned(user.id):
        await update.message.reply_text("❌ Вы заблокированы.")
        return
    
    welcome_msg = await bot.get_setting('welcome_message') or 'Добро пожаловать!'
    
    if user.id in ADMIN_IDS:
        await update.message.reply_text(
            f"🔧 *Админ-панель*\n\n{welcome_msg}",
            reply_markup=ADMIN_KEYBOARD,
            parse_mode='Markdown'
        )
    else:
        await update.message.reply_text(
            f"👋 *{welcome_msg}*\n\nВыберите действие:",
            reply_markup=USER_KEYBOARD,
            parse_mode='Markdown'
        )

async def handle_message(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user = update.effective_user
    text = update.message.text
    
    if await bot.is_user_banned(user.id):
        await update.message.reply_text("❌ Вы заблокированы.")
        return
    
    # Быстрая проверка состояний
    if user.id in user_states:
        await handle_state(update, context)
        return
    
    if user.id in ADMIN_IDS:
        await admin_handler(update, context, text)
    else:
        await user_handler(update, context, text)

async def admin_handler(update: Update, context, text):
    user = update.effective_user
    
    if text == "📋 Новые заявки":
        orders = await bot.get_pending_orders()
        if not orders:
            await update.message.reply_text("📭 Новых заявок нет.")
            return
        
        for order in orders:
            order_id, uid, username, first_name, ptype, qty, country, duration, price, created_at = order
            msg = f"📋 *Заявка #{order_id}*\n👤 {first_name}\n📦 {ptype} x{qty}\n🌍 {country}\n⏱ {duration}\n💰 ${price:.2f}"
            
            keyboard = [[
                InlineKeyboardButton("✅ Взять", callback_data=f"take_{order_id}"),
                InlineKeyboardButton("❌ Отклонить", callback_data=f"reject_{order_id}")
            ]]
            await update.message.reply_text(msg, reply_markup=InlineKeyboardMarkup(keyboard), parse_mode='Markdown')
    
    elif text == "📊 Статистика":
        stats = await bot.get_stats()
        msg = f"📊 *Статистика*\n\n👥 Пользователей: {stats['total_users']}\n📦 Выполнено: {stats['completed']}\n⏳ В ожидании: {stats['pending']}\n💰 Доход: ${stats['total_revenue']:.2f}"
        await update.message.reply_text(msg, parse_mode='Markdown')
    
    elif text == "👥 Пользователи":
        user_states[user.id] = {"action": "manage_user"}
        await update.message.reply_text("Введите ID пользователя:", reply_markup=BACK_KEYBOARD)
    
    elif text == "💰 Пополнения":
        topups = await bot.get_pending_topups()
        if not topups:
            await update.message.reply_text("📭 Нет пополнений.")
            return
        
        for t in topups:
            tid, uid, username, first_name, amount, created_at = t
            msg = f"💰 *Пополнение #{tid}*\n👤 {first_name}\n💵 ${amount:.2f}"
            keyboard = [[
                InlineKeyboardButton("✅", callback_data=f"approve_topup_{tid}"),
                InlineKeyboardButton("❌", callback_data=f"reject_topup_{tid}")
            ]]
            await update.message.reply_text(msg, reply_markup=InlineKeyboardMarkup(keyboard), parse_mode='Markdown')
    
    elif text == "⚙️ Настройки":
        enabled = await bot.get_setting('orders_enabled')
        status = "✅ Включены" if enabled == 'true' else "❌ Отключены"
        keyboard = [[InlineKeyboardButton("🔄 Переключить заказы", callback_data="toggle_orders")]]
        await update.message.reply_text(f"⚙️ Заказы: {status}", reply_markup=InlineKeyboardMarkup(keyboard))

async def user_handler(update: Update, context, text):
    user = update.effective_user
    
    if text == "🛒 Заказать прокси":
        if await bot.get_setting('orders_enabled') != 'true':
            await update.message.reply_text("❌ Заказы недоступны.")
            return
        
        keyboard = [
            [InlineKeyboardButton("HTTP", callback_data="type_http"),
             InlineKeyboardButton("SOCKS5", callback_data="type_socks5")],
            [InlineKeyboardButton("Residential", callback_data="type_residential"),
             InlineKeyboardButton("Datacenter", callback_data="type_datacenter")]
        ]
        await update.message.reply_text("📦 Выберите тип:", reply_markup=InlineKeyboardMarkup(keyboard))
    
    elif text == "📞 Мои заказы":
        orders = await bot.get_user_orders(user.id)
        if not orders:
            await update.message.reply_text("📭 Нет заказов.")
            return
        
        for order in orders:
            oid, ptype, qty, country, duration, price, status, created_at = order
            emoji = {'pending': '⏳', 'in_progress': '🔄', 'completed': '✅', 'cancelled': '❌', 'rejected': '🚫'}.get(status, '❓')
            msg = f"{emoji} *Заказ #{oid}*\n📦 {ptype} x{qty}\n🌍 {country}\n⏱ {duration}\n💰 ${price:.2f}"
            
            keyboard = InlineKeyboardMarkup([[InlineKeyboardButton("❌ Отменить", callback_data=f"cancel_order_{oid}")]]) if status == 'pending' else None
            await update.message.reply_text(msg, reply_markup=keyboard, parse_mode='Markdown')
    
    elif text == "💳 Баланс":
        balance = await bot.get_user_balance(user.id)
        await update.message.reply_text(f"💳 *Баланс: ${balance:.2f}*", parse_mode='Markdown')
    
    elif text == "💰 Пополнить":
        user_states[user.id] = {"action": "topup"}
        await update.message.reply_text("💰 Введите сумму USD (мин $1):", reply_markup=BACK_KEYBOARD)
    
    elif text == "ℹ️ Информация":
        await update.message.reply_text("ℹ️ Мы продаем качественные прокси.\n\nТипы: HTTP, SOCKS5, Residential, Datacenter\nСроки: 1 день, 1 неделя, 1 месяц, 3 месяца")
    
    elif text == "💰 Цены":
        http_p = await bot.get_setting('base_price_http') or '2.0'
        socks_p = await bot.get_setting('base_price_socks5') or '2.5'
        res_p = await bot.get_setting('base_price_residential') or '5.0'
        dc_p = await bot.get_setting('base_price_datacenter') or '1.5'
        msg = f"💰 *Цены за 1 день:*\n• HTTP: ${http_p}\n• SOCKS5: ${socks_p}\n• Residential: ${res_p}\n• Datacenter: ${dc_p}\n\n*Множители:*\n1 нед: x6 | 1 мес: x20 | 3 мес: x50"
        await update.message.reply_text(msg, parse_mode='Markdown')

async def handle_state(update: Update, context):
    user = update.effective_user
    text = update.message.text
    state = user_states[user.id]
    
    if text == "🔙 Отмена":
        del user_states[user.id]
        await start(update, context)
        return
    
    if state["action"] == "manage_user":
        try:
            target_id = int(text)
            keyboard = [[
                InlineKeyboardButton("🔨 Забанить", callback_data=f"ban_{target_id}"),
                InlineKeyboardButton("✅ Разбанить", callback_data=f"unban_{target_id}")
            ]]
            await update.message.reply_text(f"👤 ID: `{target_id}`", reply_markup=InlineKeyboardMarkup(keyboard), parse_mode='Markdown')
            del user_states[user.id]
        except ValueError:
            await update.message.reply_text("❌ Введите число:")
    
    elif state["action"] == "topup":
        try:
            amount = float(text)
            if amount < 1:
                await update.message.reply_text("❌ Минимум $1:")
                return
            
            topup_id = await bot.create_topup_request(user.id, amount)
            del user_states[user.id]
            await update.message.reply_text(f"✅ Заявка #{topup_id} на ${amount:.2f} создана!\n⏳ Ожидайте подтверждения.", reply_markup=USER_KEYBOARD)
        except ValueError:
            await update.message.reply_text("❌ Введите число:")
    
    elif state["action"] == "order":
        if state["step"] == "quantity":
            try:
                qty = int(text)
                if qty < 1 or qty > 100:
                    await update.message.reply_text("❌ 1-100:")
                    return
                state["quantity"] = qty
                state["step"] = "country"
                
                countries = await bot.get_available_countries()
                keyboard = []
                row = []
                for i, c in enumerate(countries):
                    row.append(InlineKeyboardButton(c, callback_data=f"country_{c}"))
                    if len(row) == 2:
                        keyboard.append(row)
                        row = []
                if row:
                    keyboard.append(row)
                await update.message.reply_text("🌍 Страна:", reply_markup=InlineKeyboardMarkup(keyboard))
            except ValueError:
                await update.message.reply_text("❌ Введите число:")

async def handle_callback(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    user = query.from_user
    data = query.data
    await query.answer()
    
    if data.startswith("take_"):
        order_id = int(data.split("_")[1])
        await bot.assign_admin_to_order(order_id, user.id)
        await query.edit_message_text(f"{query.message.text}\n\n✅ Взято @{user.username}")
        order = await bot.get_order_by_id(order_id)
        if order:
            await context.bot.send_message(order[1], f"✅ Заказ #{order_id} в работе!")
    
    elif data.startswith("reject_"):
        order_id = int(data.split("_")[1])
        await bot.close_order(order_id, 'rejected')
        order = await bot.get_order_by_id(order_id)
        if order:
            await bot.update_balance(order[1], order[9])
            await context.bot.send_message(order[1], f"❌ Заказ #{order_id} отклонен. Деньги возвращены.")
        await query.edit_message_text(f"{query.message.text}\n\n❌ Отклонено")
    
    elif data.startswith("cancel_order_"):
        order_id = int(data.split("_")[2])
        success, result = await bot.cancel_order(order_id, user.id)
        if success:
            await query.edit_message_text(f"{query.message.text}\n\n❌ Отменено. +${result:.2f}")
        else:
            await query.answer(f"❌ {result}", show_alert=True)
    
    elif data.startswith("ban_"):
        target_id = int(data.split("_")[1])
        await bot.ban_user(target_id)
        await query.edit_message_text(f"✅ {target_id} забанен")
    
    elif data.startswith("unban_"):
        target_id = int(data.split("_")[1])
        await bot.unban_user(target_id)
        await query.edit_message_text(f"✅ {target_id} разбанен")
    
    elif data.startswith("approve_topup_"):
        topup_id = int(data.split("_")[2])
        result = await bot.approve_topup(topup_id)
        if result[0]:
            user_id, amount = result
            await query.edit_message_text(f"{query.message.text}\n\n✅ Подтверждено")
            await context.bot.send_message(user_id, f"✅ Пополнение ${amount:.2f} зачислено!")
    
    elif data.startswith("reject_topup_"):
        topup_id = int(data.split("_")[2])
        user_id = await bot.reject_topup(topup_id)
        if user_id:
            await query.edit_message_text(f"{query.message.text}\n\n❌ Отклонено")
            await context.bot.send_message(user_id, "❌ Пополнение отклонено")
    
    elif data == "toggle_orders":
        current = await bot.get_setting('orders_enabled')
        new = 'false' if current == 'true' else 'true'
        await bot.set_setting('orders_enabled', new)
        status = "✅ Включены" if new == 'true' else "❌ Отключены"
        await query.edit_message_text(f"⚙️ Заказы: {status}")
    
    elif data.startswith("type_"):
        proxy_type = data.split("_")[1]
        user_states[user.id] = {"action": "order", "step": "quantity", "type": proxy_type}
        await query.edit_message_text(f"📦 {proxy_type}\n\nВведите количество (1-100):")
    
    elif data.startswith("country_"):
        country = data.split("_")[1]
        user_states[user.id]["country"] = country
        user_states[user.id]["step"] = "duration"
        
        keyboard = [
            [InlineKeyboardButton("1 день", callback_data="duration_1 день"),
             InlineKeyboardButton("1 неделя", callback_data="duration_1 неделя")],
            [InlineKeyboardButton("1 месяц", callback_data="duration_1 месяц"),
             InlineKeyboardButton("3 месяца", callback_data="duration_3 месяца")]
        ]
        await query.edit_message_text("⏱ Срок:", reply_markup=InlineKeyboardMarkup(keyboard))
    
    elif data.startswith("duration_"):
        duration = data.split("_")[1]
        state = user_states[user.id]
        state["duration"] = duration
        
        # Создаем заказ сразу
        balance = await bot.get_user_balance(user.id)
        price = await bot.calculate_price(state["type"], state["quantity"], state["country"])
        
        if balance < price:
            del user_states[user.id]
            await query.edit_message_text(f"❌ Недостаточно средств!\nБаланс: ${balance:.2f}\nНужно: ${price:.2f}")
            return
        
        order_id, price = await bot.create_order(
            user.id, state["type"], state["quantity"],
            state["country"], state["duration"], ""
        )
        
        del user_states[user.id]
        msg = f"✅ *Заказ #{order_id} создан!*\n📦 {state['type']} x{state['quantity']}\n🌍 {state['country']}\n⏱ {state['duration']}\n💰 ${price:.2f}"
        await query.edit_message_text(msg, parse_mode='Markdown')

def main():
    if not BOT_TOKEN:
        logger.error("BOT_TOKEN не установлен!")
        sys.exit(1)
    
    loop = asyncio.new_event_loop()
    asyncio.set_event_loop(loop)
    
    try:
        loop.run_until_complete(bot.init_db())
        
        application = (
            Application.builder()
            .token(BOT_TOKEN)
            .connect_timeout(20)
            .read_timeout(20)
            .write_timeout(20)
            .pool_timeout(10)
            .build()
        )
        
        application.add_handler(CommandHandler("start", start))
        application.add_handler(CallbackQueryHandler(handle_callback))
        application.add_handler(MessageHandler(filters.TEXT & ~filters.COMMAND, handle_message))
        
        logger.info("Бот запущен!")
        application.run_polling(drop_pending_updates=True, poll_interval=0.5)
        
    except Exception as e:
        logger.error(f"Ошибка: {e}")
        sys.exit(1)
    finally:
        loop.close()

if __name__ == '__main__':
    main()
