import os
import sys
import json
import logging
from datetime import datetime
from threading import Thread

from fastapi import FastAPI, Request, Form, HTTPException
from fastapi.responses import JSONResponse, HTMLResponse
from telegram import Update, InlineKeyboardButton, InlineKeyboardMarkup, ReplyKeyboardMarkup, KeyboardButton
from telegram.ext import Application, CommandHandler, MessageHandler, CallbackQueryHandler, ContextTypes, filters
from telegram.constants import ParseMode
import aiosqlite
from dotenv import load_dotenv

load_dotenv()

logging.basicConfig(format='%(asctime)s - %(levelname)s - %(message)s', level=logging.INFO)
logger = logging.getLogger(__name__)

BOT_TOKEN = os.getenv('BOT_TOKEN')
if not BOT_TOKEN:
    logger.error("BOT_TOKEN не найден!")
    sys.exit(1)

ADMIN_IDS = list(map(int, os.getenv('ADMIN_IDS', '').split(','))) if os.getenv('ADMIN_IDS') else []
ADMIN_PASSWORD = os.getenv('ADMIN_PASSWORD', 'admin123')
DATABASE_PATH = '/tmp/proxy_bot.db'
PORT = int(os.getenv('PORT', 8000))

user_states = {}

class Database:
    def __init__(self):
        self.path = DATABASE_PATH
    
    async def init(self):
        async with aiosqlite.connect(self.path) as db:
            await db.execute('PRAGMA journal_mode=WAL')
            await db.execute('PRAGMA synchronous=NORMAL')
            await db.execute('PRAGMA cache_size=-8000')
            
            await db.executescript('''
                CREATE TABLE IF NOT EXISTS users (
                    user_id INTEGER PRIMARY KEY,
                    username TEXT DEFAULT '',
                    first_name TEXT DEFAULT '',
                    last_name TEXT DEFAULT '',
                    is_admin INTEGER DEFAULT 0,
                    is_banned INTEGER DEFAULT 0,
                    balance REAL DEFAULT 0.0,
                    total_spent REAL DEFAULT 0.0,
                    orders_count INTEGER DEFAULT 0,
                    created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
                );
                
                CREATE TABLE IF NOT EXISTS orders (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    user_id INTEGER NOT NULL,
                    proxy_type TEXT NOT NULL,
                    quantity INTEGER NOT NULL,
                    country TEXT NOT NULL,
                    duration TEXT NOT NULL,
                    additional_info TEXT DEFAULT '',
                    price REAL NOT NULL,
                    status TEXT DEFAULT 'pending',
                    admin_id INTEGER,
                    created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
                    updated_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
                );
                
                CREATE TABLE IF NOT EXISTS settings (
                    key TEXT PRIMARY KEY,
                    value TEXT
                );
                
                CREATE TABLE IF NOT EXISTS countries (
                    name TEXT PRIMARY KEY,
                    is_active INTEGER DEFAULT 1
                );
                
                CREATE TABLE IF NOT EXISTS topups (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    user_id INTEGER NOT NULL,
                    amount REAL NOT NULL,
                    status TEXT DEFAULT 'pending',
                    admin_id INTEGER,
                    created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
                );
                
                INSERT OR IGNORE INTO settings (key, value) VALUES 
                ('orders_enabled', 'true'),
                ('base_price_http', '2.0'),
                ('base_price_socks5', '2.5'),
                ('base_price_residential', '5.0'),
                ('base_price_datacenter', '1.5'),
                ('welcome_message', 'Добро пожаловать в магазин прокси!'),
                ('support_username', '@admin');
            ''')
            
            default_countries = [
                'США', 'Россия', 'Германия', 'Франция', 'Великобритания',
                'Канада', 'Япония', 'Корея', 'Нидерланды', 'Польша',
                'Испания', 'Италия', 'Швеция', 'Норвегия', 'Финляндия'
            ]
            for country in default_countries:
                await db.execute('INSERT OR IGNORE INTO countries (name) VALUES (?)', (country,))
            
            await db.commit()
            logger.info("База данных инициализирована")
    
    async def execute(self, sql, params=None, fetch=True):
        async with aiosqlite.connect(self.path) as db:
            db.row_factory = aiosqlite.Row
            if params:
                cursor = await db.execute(sql, params)
            else:
                cursor = await db.execute(sql)
            if fetch:
                result = await cursor.fetchall()
                return [dict(row) for row in result]
            await db.commit()
            return cursor.lastrowid
    
    async def get_setting(self, key, default=None):
        result = await self.execute('SELECT value FROM settings WHERE key = ?', [key])
        return result[0]['value'] if result else default
    
    async def set_setting(self, key, value):
        await self.execute('INSERT OR REPLACE INTO settings VALUES (?, ?)', [key, value], fetch=False)
    
    async def get_user(self, user_id):
        result = await self.execute('SELECT * FROM users WHERE user_id = ?', [user_id])
        return result[0] if result else None
    
    async def create_user(self, user_id, username, first_name, last_name):
        await self.execute(
            'INSERT OR REPLACE INTO users (user_id, username, first_name, last_name, is_admin) VALUES (?, ?, ?, ?, ?)',
            [user_id, username or '', first_name or '', last_name or '', 1 if user_id in ADMIN_IDS else 0],
            fetch=False
        )
    
    async def get_user_orders(self, user_id, limit=20):
        return await self.execute(
            'SELECT * FROM orders WHERE user_id = ? ORDER BY created_at DESC LIMIT ?',
            [user_id, limit]
        )
    
    async def create_order(self, user_id, proxy_type, quantity, country, duration, price):
        order_id = await self.execute(
            'INSERT INTO orders (user_id, proxy_type, quantity, country, duration, price) VALUES (?, ?, ?, ?, ?, ?)',
            [user_id, proxy_type, quantity, country, duration, price],
            fetch=False
        )
        await self.execute(
            'UPDATE users SET balance = balance - ?, orders_count = orders_count + 1, total_spent = total_spent + ? WHERE user_id = ?',
            [price, price, user_id],
            fetch=False
        )
        return order_id
    
    async def cancel_order(self, order_id, user_id):
        order = await self.execute(
            'SELECT * FROM orders WHERE id = ? AND user_id = ? AND status = ?',
            [order_id, user_id, 'pending']
        )
        if not order:
            return False, "Заказ не найден или уже обработан"
        
        order = order[0]
        await self.execute(
            "UPDATE orders SET status = 'cancelled', updated_at = CURRENT_TIMESTAMP WHERE id = ?",
            [order_id], fetch=False
        )
        await self.execute(
            'UPDATE users SET balance = balance + ? WHERE user_id = ?',
            [order['price'], user_id], fetch=False
        )
        return True, order['price']
    
    async def get_pending_orders(self):
        return await self.execute('''
            SELECT o.*, u.username, u.first_name 
            FROM orders o 
            JOIN users u ON o.user_id = u.user_id 
            WHERE o.status = 'pending' 
            ORDER BY o.created_at DESC 
            LIMIT 50
        ''')
    
    async def get_order(self, order_id):
        result = await self.execute(
            'SELECT o.*, u.username, u.first_name FROM orders o JOIN users u ON o.user_id = u.user_id WHERE o.id = ?',
            [order_id]
        )
        return result[0] if result else None
    
    async def update_order_status(self, order_id, status, admin_id=None):
        await self.execute(
            'UPDATE orders SET status = ?, admin_id = ?, updated_at = CURRENT_TIMESTAMP WHERE id = ?',
            [status, admin_id, order_id], fetch=False
        )
    
    async def get_pending_topups(self):
        return await self.execute('''
            SELECT t.*, u.username, u.first_name 
            FROM topups t 
            JOIN users u ON t.user_id = u.user_id 
            WHERE t.status = 'pending' 
            ORDER BY t.created_at DESC 
            LIMIT 50
        ''')
    
    async def create_topup(self, user_id, amount):
        return await self.execute(
            'INSERT INTO topups (user_id, amount) VALUES (?, ?)',
            [user_id, amount], fetch=False
        )
    
    async def approve_topup(self, topup_id, admin_id):
        topup = await self.execute(
            'SELECT * FROM topups WHERE id = ? AND status = ?',
            [topup_id, 'pending']
        )
        if not topup:
            return None
        topup = topup[0]
        await self.execute(
            "UPDATE topups SET status = 'completed', admin_id = ? WHERE id = ?",
            [admin_id, topup_id], fetch=False
        )
        await self.execute(
            'UPDATE users SET balance = balance + ? WHERE user_id = ?',
            [topup['amount'], topup['user_id']], fetch=False
        )
        return topup
    
    async def reject_topup(self, topup_id, admin_id):
        topup = await self.execute(
            'SELECT * FROM topups WHERE id = ? AND status = ?',
            [topup_id, 'pending']
        )
        if not topup:
            return None
        await self.execute(
            "UPDATE topups SET status = 'rejected', admin_id = ? WHERE id = ?",
            [admin_id, topup_id], fetch=False
        )
        return topup[0]
    
    async def get_stats(self):
        total_users = await self.execute('SELECT COUNT(*) as count FROM users')
        total_orders = await self.execute('SELECT COUNT(*) as count FROM orders')
        completed = await self.execute("SELECT COUNT(*) as count FROM orders WHERE status = 'completed'")
        pending = await self.execute("SELECT COUNT(*) as count FROM orders WHERE status = 'pending'")
        revenue = await self.execute("SELECT COALESCE(SUM(price), 0) as total FROM orders WHERE status = 'completed'")
        
        return {
            'total_users': total_users[0]['count'],
            'total_orders': total_orders[0]['count'],
            'completed_orders': completed[0]['count'],
            'pending_orders': pending[0]['count'],
            'total_revenue': round(revenue[0]['total'], 2)
        }
    
    async def get_all_users(self):
        return await self.execute('SELECT * FROM users ORDER BY created_at DESC LIMIT 100')
    
    async def get_countries(self):
        result = await self.execute('SELECT name FROM countries WHERE is_active = 1 ORDER BY name')
        return [r['name'] for r in result]
    
    async def add_country(self, name):
        await self.execute('INSERT OR IGNORE INTO countries (name) VALUES (?)', [name], fetch=False)
    
    async def remove_country(self, name):
        await self.execute('UPDATE countries SET is_active = 0 WHERE name = ?', [name], fetch=False)

db = Database()

MAIN_KEYBOARD = ReplyKeyboardMarkup([
    [KeyboardButton("🛒 Заказать прокси"), KeyboardButton("📋 Мои заказы")],
    [KeyboardButton("💳 Баланс"), KeyboardButton("💰 Пополнить")],
    [KeyboardButton("ℹ️ Информация"), KeyboardButton("💵 Цены")]
], resize_keyboard=True)

ADMIN_KEYBOARD = ReplyKeyboardMarkup([
    [KeyboardButton("📋 Заявки"), KeyboardButton("💰 Пополнения")],
    [KeyboardButton("📊 Статистика"), KeyboardButton("👥 Пользователи")],
    [KeyboardButton("⚙️ Настройки"), KeyboardButton("🌍 Страны")]
], resize_keyboard=True)

CANCEL_KEYBOARD = ReplyKeyboardMarkup([
    [KeyboardButton("🔙 Отмена")]
], resize_keyboard=True)

async def start_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user = update.effective_user
    await db.create_user(user.id, user.username, user.first_name, user.last_name)
    
    user_data = await db.get_user(user.id)
    if user_data and user_data['is_banned']:
        await update.message.reply_text("❌ Ваш аккаунт заблокирован.")
        return
    
    welcome = await db.get_setting('welcome_message', 'Добро пожаловать!')
    
    if user.id in ADMIN_IDS:
        await update.message.reply_text(
            f"🔧 *Админ-панель*\n\n{welcome}",
            reply_markup=ADMIN_KEYBOARD,
            parse_mode=ParseMode.MARKDOWN
        )
    else:
        await update.message.reply_text(
            f"👋 {welcome}\n\nВыберите действие:",
            reply_markup=MAIN_KEYBOARD
        )

async def handle_all_messages(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not update.message or not update.message.text:
        return
    
    user = update.effective_user
    text = update.message.text.strip()
    
    await db.create_user(user.id, user.username, user.first_name, user.last_name)
    user_data = await db.get_user(user.id)
    
    if user_data and user_data['is_banned']:
        await update.message.reply_text("❌ Вы заблокированы.")
        return
    
    if user.id in user_states:
        await handle_state_message(update, context)
        return
    
    if user.id in ADMIN_IDS:
        await handle_admin_menu(update, context, text)
    else:
        await handle_user_menu(update, context, text)

async def handle_state_message(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user = update.effective_user
    text = update.message.text.strip()
    state = user_states.get(user.id)
    
    if not state:
        return
    
    if text == "🔙 Отмена":
        del user_states[user.id]
        keyboard = ADMIN_KEYBOARD if user.id in ADMIN_IDS else MAIN_KEYBOARD
        await update.message.reply_text("❌ Отменено", reply_markup=keyboard)
        return
    
    if state.get('action') == 'awaiting_quantity':
        try:
            qty = int(text)
            if qty < 1 or qty > 100:
                await update.message.reply_text("❌ Введите число от 1 до 100:")
                return
            
            state['quantity'] = qty
            state['action'] = 'awaiting_country'
            
            proxy_type = state['proxy_type']
            base_price = await db.get_setting(f'base_price_{proxy_type}', '2.0')
            
            countries = await db.get_countries()
            keyboard = []
            row = []
            for country in countries:
                row.append(InlineKeyboardButton(country, callback_data=f"country_{country}"))
                if len(row) == 2:
                    keyboard.append(row)
                    row = []
            if row:
                keyboard.append(row)
            keyboard.append([InlineKeyboardButton("🔙 Отмена", callback_data="cancel_action")])
            
            await update.message.reply_text(
                f"📦 {proxy_type.upper()} × {qty}\n\n"
                f"💵 Базовая цена: ${float(base_price):.2f}/день\n\n"
                f"🌍 Выберите страну:",
                reply_markup=InlineKeyboardMarkup(keyboard)
            )
        except ValueError:
            await update.message.reply_text("❌ Введите целое число:")
    
    elif state.get('action') == 'awaiting_topup':
        try:
            amount = float(text)
            if amount < 1:
                await update.message.reply_text("❌ Минимум $1. Введите сумму:")
                return
            if amount > 1000:
                await update.message.reply_text("❌ Максимум $1000. Введите сумму:")
                return
            
            topup_id = await db.create_topup(user.id, amount)
            del user_states[user.id]
            
            await update.message.reply_text(
                f"✅ Заявка #{topup_id} создана!\n\n"
                f"💵 Сумма: ${amount:.2f}\n"
                f"⏳ Ожидайте подтверждения.",
                reply_markup=MAIN_KEYBOARD
            )
            
            for admin_id in ADMIN_IDS:
                try:
                    await context.bot.send_message(
                        admin_id,
                        f"💰 Пополнение #{topup_id}\n"
                        f"👤 {user.first_name} (@{user.username})\n"
                        f"💵 ${amount:.2f}"
                    )
                except:
                    pass
        except ValueError:
            await update.message.reply_text("❌ Введите число (например: 10.50):")
    
    elif state.get('action') == 'awaiting_welcome':
        await db.set_setting('welcome_message', text)
        del user_states[user.id]
        await update.message.reply_text("✅ Приветствие обновлено!", reply_markup=ADMIN_KEYBOARD)
    
    elif state.get('action') == 'awaiting_price':
        try:
            price = float(text)
            proxy_type = state.get('proxy_type')
            await db.set_setting(f'base_price_{proxy_type}', str(price))
            del user_states[user.id]
            await update.message.reply_text(f"✅ Цена для {proxy_type} обновлена: ${price:.2f}", reply_markup=ADMIN_KEYBOARD)
        except ValueError:
            await update.message.reply_text("❌ Введите число:")
    
    elif state.get('action') == 'awaiting_new_country':
        await db.add_country(text)
        del user_states[user.id]
        await update.message.reply_text(f"✅ Страна '{text}' добавлена!", reply_markup=ADMIN_KEYBOARD)
    
    elif state.get('action') == 'awaiting_remove_country':
        await db.remove_country(text)
        del user_states[user.id]
        await update.message.reply_text(f"✅ Страна '{text}' удалена!", reply_markup=ADMIN_KEYBOARD)

async def handle_admin_menu(update: Update, context, text):
    user = update.effective_user
    
    if text == "📋 Заявки":
        orders = await db.get_pending_orders()
        if not orders:
            await update.message.reply_text("✅ Нет новых заявок")
            return
        
        for order in orders:
            msg = (
                f"📋 *Заявка #{order['id']}*\n\n"
                f"👤 {order['first_name']} (@{order['username']})\n"
                f"📦 {order['proxy_type'].upper()} × {order['quantity']}\n"
                f"🌍 {order['country']}\n"
                f"⏱ {order['duration']}\n"
                f"💰 ${order['price']:.2f}\n"
                f"📅 {order['created_at'][:19]}"
            )
            keyboard = [[
                InlineKeyboardButton("✅ Взять в работу", callback_data=f"take_{order['id']}"),
                InlineKeyboardButton("❌ Отклонить", callback_data=f"reject_{order['id']}")
            ]]
            await update.message.reply_text(
                msg,
                reply_markup=InlineKeyboardMarkup(keyboard),
                parse_mode=ParseMode.MARKDOWN
            )
    
    elif text == "💰 Пополнения":
        topups = await db.get_pending_topups()
        if not topups:
            await update.message.reply_text("✅ Нет ожидающих пополнений")
            return
        
        for t in topups:
            msg = (
                f"💰 *Пополнение #{t['id']}*\n\n"
                f"👤 {t['first_name']} (@{t['username']})\n"
                f"💵 ${t['amount']:.2f}\n"
                f"📅 {t['created_at'][:19]}"
            )
            keyboard = [[
                InlineKeyboardButton("✅ Подтвердить", callback_data=f"approve_topup_{t['id']}"),
                InlineKeyboardButton("❌ Отклонить", callback_data=f"reject_topup_{t['id']}")
            ]]
            await update.message.reply_text(
                msg,
                reply_markup=InlineKeyboardMarkup(keyboard),
                parse_mode=ParseMode.MARKDOWN
            )
    
    elif text == "📊 Статистика":
        stats = await db.get_stats()
        msg = (
            f"📊 *Статистика*\n\n"
            f"👥 Пользователей: {stats['total_users']}\n"
            f"📦 Всего заказов: {stats['total_orders']}\n"
            f"✅ Выполнено: {stats['completed_orders']}\n"
            f"⏳ В ожидании: {stats['pending_orders']}\n"
            f"💰 Общий доход: ${stats['total_revenue']:.2f}"
        )
        await update.message.reply_text(msg, parse_mode=ParseMode.MARKDOWN)
    
    elif text == "👥 Пользователи":
        users = await db.get_all_users()
        if not users:
            await update.message.reply_text("Нет пользователей")
            return
        
        msg = "👥 *Последние 20 пользователей:*\n\n"
        for u in users[:20]:
            ban = "🚫" if u['is_banned'] else "✅"
            username_str = f" (@{u['username']})" if u['username'] else ""
            msg += (
                f"{ban} {u['first_name'] or 'Без имени'}{username_str}\n"
                f"   ID: `{u['user_id']}` | 💳 ${u['balance']:.2f}\n"
                f"   Заказов: {u['orders_count']}\n\n"
            )
        await update.message.reply_text(msg, parse_mode=ParseMode.MARKDOWN)
    
    elif text == "⚙️ Настройки":
        enabled = await db.get_setting('orders_enabled')
        status = "✅ Включены" if enabled == 'true' else "❌ Отключены"
        
        prices = {
            'http': await db.get_setting('base_price_http', '2.0'),
            'socks5': await db.get_setting('base_price_socks5', '2.5'),
            'residential': await db.get_setting('base_price_residential', '5.0'),
            'datacenter': await db.get_setting('base_price_datacenter', '1.5')
        }
        
        msg = (
            f"⚙️ *Настройки*\n\n"
            f"Прием заказов: {status}\n\n"
            f"*Цены за день:*\n"
            f"• HTTP: ${prices['http']}\n"
            f"• SOCKS5: ${prices['socks5']}\n"
            f"• Residential: ${prices['residential']}\n"
            f"• Datacenter: ${prices['datacenter']}"
        )
        
        keyboard = [
            [InlineKeyboardButton("🔄 Переключить заказы", callback_data="toggle_orders")],
            [InlineKeyboardButton("✏️ Изменить приветствие", callback_data="edit_welcome")],
            [InlineKeyboardButton("💵 HTTP", callback_data="setprice_http"),
             InlineKeyboardButton("💵 SOCKS5", callback_data="setprice_socks5")],
            [InlineKeyboardButton("💵 Residential", callback_data="setprice_residential"),
             InlineKeyboardButton("💵 Datacenter", callback_data="setprice_datacenter")]
        ]
        
        await update.message.reply_text(
            msg,
            reply_markup=InlineKeyboardMarkup(keyboard),
            parse_mode=ParseMode.MARKDOWN
        )
    
    elif text == "🌍 Страны":
        countries = await db.get_countries()
        msg = "🌍 *Доступные страны:*\n\n" + "\n".join(f"• {c}" for c in countries)
        
        keyboard = [
            [InlineKeyboardButton("➕ Добавить страну", callback_data="add_country"),
             InlineKeyboardButton("➖ Удалить страну", callback_data="remove_country")]
        ]
        
        await update.message.reply_text(
            msg,
            reply_markup=InlineKeyboardMarkup(keyboard),
            parse_mode=ParseMode.MARKDOWN
        )

async def handle_user_menu(update: Update, context, text):
    user = update.effective_user
    
    if text == "🛒 Заказать прокси":
        enabled = await db.get_setting('orders_enabled', 'true')
        if enabled != 'true':
            await update.message.reply_text("❌ Прием заказов временно приостановлен")
            return
        
        keyboard = [
            [InlineKeyboardButton("HTTP", callback_data="type_http"),
             InlineKeyboardButton("SOCKS5", callback_data="type_socks5")],
            [InlineKeyboardButton("Residential", callback_data="type_residential"),
             InlineKeyboardButton("Datacenter", callback_data="type_datacenter")]
        ]
        await update.message.reply_text(
            "📦 *Выберите тип прокси:*",
            reply_markup=InlineKeyboardMarkup(keyboard),
            parse_mode=ParseMode.MARKDOWN
        )
    
    elif text == "📋 Мои заказы":
        orders = await db.get_user_orders(user.id)
        if not orders:
            await update.message.reply_text("📭 У вас пока нет заказов")
            return
        
        for order in orders:
            emoji = {
                'pending': '⏳',
                'in_progress': '🔄',
                'completed': '✅',
                'cancelled': '❌',
                'rejected': '🚫'
            }.get(order['status'], '❓')
            
            msg = (
                f"{emoji} *Заказ #{order['id']}*\n\n"
                f"📦 {order['proxy_type'].upper()} × {order['quantity']}\n"
                f"🌍 {order['country']} | ⏱ {order['duration']}\n"
                f"💰 ${order['price']:.2f}\n"
                f"📅 {order['created_at'][:10]}"
            )
            
            keyboard = None
            if order['status'] == 'pending':
                keyboard = InlineKeyboardMarkup([[
                    InlineKeyboardButton("❌ Отменить заказ", callback_data=f"cancel_{order['id']}")
                ]])
            
            await update.message.reply_text(
                msg,
                reply_markup=keyboard,
                parse_mode=ParseMode.MARKDOWN
            )
    
    elif text == "💳 Баланс":
        user_data = await db.get_user(user.id)
        balance = user_data['balance'] if user_data else 0
        spent = user_data['total_spent'] if user_data else 0
        orders_count = user_data['orders_count'] if user_data else 0
        
        await update.message.reply_text(
            f"💳 *Ваш баланс*\n\n"
            f"💰 Доступно: ${balance:.2f}\n"
            f"📊 Потрачено: ${spent:.2f}\n"
            f"📦 Заказов: {orders_count}",
            parse_mode=ParseMode.MARKDOWN
        )
    
    elif text == "💰 Пополнить":
        user_states[user.id] = {'action': 'awaiting_topup'}
        await update.message.reply_text(
            "💰 Введите сумму пополнения в USD (мин. $1):",
            reply_markup=CANCEL_KEYBOARD
        )
    
    elif text == "ℹ️ Информация":
        support = await db.get_setting('support_username', '@admin')
        await update.message.reply_text(
            f"ℹ️ *О сервисе*\n\n"
            f"Мы предоставляем качественные прокси для любых задач.\n\n"
            f"*Доступные типы:*\n"
            f"• HTTP - для веб-серфинга\n"
            f"• SOCKS5 - универсальные\n"
            f"• Residential - жилые IP\n"
            f"• Datacenter - серверные\n\n"
            f"*Сроки аренды:*\n"
            f"• 1 день | 1 неделя | 1 месяц | 3 месяца\n\n"
            f"*Поддержка:* {support}",
            parse_mode=ParseMode.MARKDOWN
        )
    
    elif text == "💵 Цены":
        prices = {
            'http': await db.get_setting('base_price_http', '2.0'),
            'socks5': await db.get_setting('base_price_socks5', '2.5'),
            'residential': await db.get_setting('base_price_residential', '5.0'),
            'datacenter': await db.get_setting('base_price_datacenter', '1.5')
        }
        
        await update.message.reply_text(
            f"💵 *Цены за 1 день:*\n\n"
            f"• HTTP: ${prices['http']}\n"
            f"• SOCKS5: ${prices['socks5']}\n"
            f"• Residential: ${prices['residential']}\n"
            f"• Datacenter: ${prices['datacenter']}\n\n"
            f"*Множители срока:*\n"
            f"• 1 неделя: ×6\n"
            f"• 1 месяц: ×20\n"
            f"• 3 месяца: ×50",
            parse_mode=ParseMode.MARKDOWN
        )

async def handle_callbacks(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    user = query.from_user
    data = query.data
    
    await query.answer()
    
    if data == "cancel_action":
        if user.id in user_states:
            del user_states[user.id]
        await query.edit_message_text("❌ Действие отменено")
        return
    
    if data.startswith("type_"):
        proxy_type = data.split("_")[1]
        user_states[user.id] = {
            'action': 'awaiting_quantity',
            'proxy_type': proxy_type
        }
        base_price = await db.get_setting(f'base_price_{proxy_type}', '2.0')
        await query.edit_message_text(
            f"📦 *{proxy_type.upper()}*\n\n"
            f"💵 Базовая цена: ${float(base_price):.2f}/день\n\n"
            f"Введите количество (1-100):",
            parse_mode=ParseMode.MARKDOWN
        )
    
    elif data.startswith("country_"):
        country = data.split("_", 1)[1]
        user_states[user.id]['country'] = country
        user_states[user.id]['action'] = 'awaiting_duration'
        
        keyboard = [
            [InlineKeyboardButton("1 день", callback_data="duration_1 день"),
             InlineKeyboardButton("1 неделя", callback_data="duration_1 неделя")],
            [InlineKeyboardButton("1 месяц", callback_data="duration_1 месяц"),
             InlineKeyboardButton("3 месяца", callback_data="duration_3 месяца")],
            [InlineKeyboardButton("🔙 Отмена", callback_data="cancel_action")]
        ]
        
        await query.edit_message_text(
            f"🌍 *{country}*\n\nВыберите срок аренды:",
            reply_markup=InlineKeyboardMarkup(keyboard),
            parse_mode=ParseMode.MARKDOWN
        )
    
    elif data.startswith("duration_"):
        duration = data.split("_", 1)[1]
        state = user_states.get(user.id)
        
        if not state or 'proxy_type' not in state:
            await query.edit_message_text("❌ Ошибка. Начните заказ заново.")
            return
        
        proxy_type = state['proxy_type']
        quantity = state['quantity']
        country = state['country']
        
        base_prices = {
            'http': float(await db.get_setting('base_price_http', '2.0')),
            'socks5': float(await db.get_setting('base_price_socks5', '2.5')),
            'residential': float(await db.get_setting('base_price_residential', '5.0')),
            'datacenter': float(await db.get_setting('base_price_datacenter', '1.5'))
        }
        multipliers = {'1 день': 1, '1 неделя': 6, '1 месяц': 20, '3 месяца': 50}
        
        base = base_prices.get(proxy_type, 2.0)
        mult = multipliers.get(duration, 1)
        total_price = base * quantity * mult
        
        user_data = await db.get_user(user.id)
        balance = user_data['balance'] if user_data else 0
        
        if balance < total_price:
            del user_states[user.id]
            await query.edit_message_text(
                f"❌ *Недостаточно средств!*\n\n"
                f"💰 Баланс: ${balance:.2f}\n"
                f"💵 Стоимость заказа: ${total_price:.2f}\n"
                f"❌ Не хватает: ${total_price - balance:.2f}\n\n"
                f"Пополните баланс и попробуйте снова.",
                parse_mode=ParseMode.MARKDOWN
            )
            return
        
        order_id = await db.create_order(user.id, proxy_type, quantity, country, duration, total_price)
        new_balance = balance - total_price
        del user_states[user.id]
        
        msg = (
            f"✅ *Заказ #{order_id} создан!*\n\n"
            f"📦 {proxy_type.upper()} × {quantity}\n"
            f"🌍 {country} | ⏱ {duration}\n"
            f"💰 Стоимость: ${total_price:.2f}\n"
            f"💳 Остаток на балансе: ${new_balance:.2f}\n\n"
            f"⏳ Ожидайте обработки администратором."
        )
        
        await query.edit_message_text(msg, parse_mode=ParseMode.MARKDOWN)
        
        for admin_id in ADMIN_IDS:
            try:
                await context.bot.send_message(
                    admin_id,
                    f"📋 Новая заявка #{order_id}\n"
                    f"👤 {user.first_name} (@{user.username})\n"
                    f"📦 {proxy_type.upper()} × {quantity}\n"
                    f"🌍 {country} | ⏱ {duration}\n"
                    f"💰 ${total_price:.2f}"
                )
            except:
                pass
    
    elif data.startswith("take_"):
        order_id = int(data.split("_")[1])
        await db.update_order_status(order_id, 'in_progress', user.id)
        order = await db.get_order(order_id)
        
        await query.edit_message_text(
            f"{query.message.text}\n\n✅ *Взято в работу*",
            parse_mode=ParseMode.MARKDOWN
        )
        
        if order:
            await context.bot.send_message(
                order['user_id'],
                f"✅ Ваш заказ #{order_id} взят в работу!\n\nОжидайте выполнения."
            )
    
    elif data.startswith("reject_"):
        order_id = int(data.split("_")[1])
        order = await db.get_order(order_id)
        
        if order:
            await db.update_order_status(order_id, 'rejected', user.id)
            await db.execute(
                'UPDATE users SET balance = balance + ? WHERE user_id = ?',
                [order['price'], order['user_id']], fetch=False
            )
            
            await query.edit_message_text(
                f"{query.message.text}\n\n❌ *Отклонено*",
                parse_mode=ParseMode.MARKDOWN
            )
            
            await context.bot.send_message(
                order['user_id'],
                f"❌ Ваш заказ #{order_id} отклонен.\n"
                f"💰 ${order['price']:.2f} возвращены на баланс."
            )
    
    elif data.startswith("cancel_"):
        order_id = int(data.split("_")[1])
        success, result = await db.cancel_order(order_id, user.id)
        
        if success:
            await query.edit_message_text(
                f"{query.message.text}\n\n❌ *Отменено*\n💰 +${result:.2f} на баланс",
                parse_mode=ParseMode.MARKDOWN
            )
        else:
            await query.answer(result, show_alert=True)
    
    elif data.startswith("approve_topup_"):
        topup_id = int(data.split("_")[2])
        topup = await db.approve_topup(topup_id, user.id)
        
        if topup:
            await query.edit_message_text(
                f"{query.message.text}\n\n✅ *Подтверждено*",
                parse_mode=ParseMode.MARKDOWN
            )
            await context.bot.send_message(
                topup['user_id'],
                f"✅ Пополнение #{topup_id} на ${topup['amount']:.2f} зачислено!"
            )
        else:
            await query.answer("Заявка не найдена", show_alert=True)
    
    elif data.startswith("reject_topup_"):
        topup_id = int(data.split("_")[2])
        topup = await db.reject_topup(topup_id, user.id)
        
        if topup:
            await query.edit_message_text(
                f"{query.message.text}\n\n❌ *Отклонено*",
                parse_mode=ParseMode.MARKDOWN
            )
            await context.bot.send_message(
                topup['user_id'],
                f"❌ Пополнение #{topup_id} отклонено."
            )
        else:
            await query.answer("Заявка не найдена", show_alert=True)
    
    elif data == "toggle_orders":
        current = await db.get_setting('orders_enabled')
        new_value = 'false' if current == 'true' else 'true'
        await db.set_setting('orders_enabled', new_value)
        status = "✅ Включены" if new_value == 'true' else "❌ Отключены"
        await query.edit_message_text(f"⚙️ Прием заказов: {status}")
    
    elif data == "edit_welcome":
        user_states[user.id] = {'action': 'awaiting_welcome'}
        await query.edit_message_text("✏️ Введите новое приветственное сообщение:")
    
    elif data.startswith("setprice_"):
        proxy_type = data.split("_")[1]
        user_states[user.id] = {'action': 'awaiting_price', 'proxy_type': proxy_type}
        await query.edit_message_text(f"💵 Введите новую цену для {proxy_type.upper()} (за 1 день):")
    
    elif data == "add_country":
        user_states[user.id] = {'action': 'awaiting_new_country'}
        await query.edit_message_text("🌍 Введите название новой страны:")
    
    elif data == "remove_country":
        user_states[user.id] = {'action': 'awaiting_remove_country'}
        await query.edit_message_text("🌍 Введите название страны для удаления:")

# FastAPI приложение
app = FastAPI(title="Proxy Bot")

@app.on_event("startup")
async def startup():
    await db.init()
    # Удаляем вебхук при запуске
    import aiohttp
    async with aiohttp.ClientSession() as session:
        async with session.get(f"https://api.telegram.org/bot{BOT_TOKEN}/deleteWebhook") as resp:
            await resp.json()

@app.get("/")
async def root():
    return {"status": "ok", "message": "Proxy Bot is running"}

@app.get("/health")
async def health():
    return {"status": "healthy", "timestamp": datetime.now().isoformat()}

@app.get("/admin", response_class=HTMLResponse)
async def admin_panel(request: Request):
    html = """
<!DOCTYPE html>
<html lang="ru">
<head>
    <meta charset="UTF-8">
    <meta name="viewport" content="width=device-width, initial-scale=1.0">
    <title>Proxy Bot - Админка</title>
    <style>
        *{margin:0;padding:0;box-sizing:border-box}
        body{font-family:-apple-system,system-ui,sans-serif;background:#0f172a;color:#e2e8f0;padding:20px}
        .container{max-width:1400px;margin:0 auto}
        h1{color:#60a5fa;margin-bottom:30px;font-size:2.5em}
        .login{background:#1e293b;padding:40px;border-radius:20px;max-width:400px;margin:100px auto;text-align:center}
        .login input{width:100%;padding:15px;margin:15px 0;background:#334155;border:2px solid #475569;border-radius:12px;color:#e2e8f0;font-size:16px}
        .login button{width:100%;padding:15px;background:#3b82f6;border:none;border-radius:12px;color:white;font-size:16px;font-weight:600;cursor:pointer}
        .dashboard{display:none}
        .stats{display:grid;grid-template-columns:repeat(auto-fit,minmax(250px,1fr));gap:20px;margin-bottom:40px}
        .stat-card{background:#1e293b;padding:25px;border-radius:16px;border:1px solid #334155}
        .stat-card h3{color:#94a3b8;font-size:14px;text-transform:uppercase;margin-bottom:10px}
        .stat-card .value{font-size:2.5em;font-weight:bold;color:#60a5fa}
        .section{background:#1e293b;padding:30px;border-radius:16px;margin-bottom:30px;border:1px solid #334155}
        .section h2{color:#60a5fa;margin-bottom:20px;font-size:1.5em}
        table{width:100%;border-collapse:collapse}
        th,td{padding:14px 12px;text-align:left;border-bottom:1px solid #334155}
        th{color:#94a3b8;font-weight:600;font-size:13px;text-transform:uppercase}
        .btn{padding:8px 16px;border:none;border-radius:8px;cursor:pointer;font-size:14px;font-weight:600;margin:3px}
        .btn-success{background:#22c55e;color:white}
        .btn-danger{background:#ef4444;color:white}
        .header{display:flex;justify-content:space-between;align-items:center;margin-bottom:30px}
        .refresh-btn{background:#3b82f6;color:white;padding:12px 24px;border:none;border-radius:12px;font-size:15px;font-weight:600;cursor:pointer}
        .empty{text-align:center;color:#94a3b8;padding:40px;font-size:18px}
    </style>
</head>
<body>
    <div class="container">
        <div class="login" id="loginBox">
            <h1>🔧 Proxy Bot</h1>
            <p style="color:#94a3b8;margin-bottom:20px">Админ-панель управления</p>
            <input type="password" id="password" placeholder="Пароль" onkeypress="if(event.key==='Enter')login()">
            <button onclick="login()">Войти</button>
        </div>
        <div class="dashboard" id="dashboard">
            <div class="header">
                <h1>📊 Панель управления</h1>
                <button class="refresh-btn" onclick="loadAll()">🔄 Обновить</button>
            </div>
            <div class="stats" id="stats"></div>
            <div class="section"><h2>📋 Новые заявки</h2><div id="orders"></div></div>
            <div class="section"><h2>💰 Пополнения</h2><div id="topups"></div></div>
        </div>
    </div>
    <script>
        let password='';
        function login(){
            password=document.getElementById('password').value;
            if(!password)return alert('Введите пароль');
            loadAll();
            document.getElementById('loginBox').style.display='none';
            document.getElementById('dashboard').style.display='block';
            setInterval(loadAll,30000);
        }
        async function api(url){
            const res=await fetch(url+'?password='+password);
            if(res.status===403){alert('Неверный пароль');location.reload();return null}
            return res.json();
        }
        async function loadAll(){
            const[stats,orders,topups]=await Promise.all([api('/api/stats'),api('/api/orders'),api('/api/topups')]);
            if(stats)document.getElementById('stats').innerHTML=`
                <div class="stat-card"><h3>Пользователи</h3><div class="value">${stats.total_users}</div></div>
                <div class="stat-card"><h3>Заказы</h3><div class="value">${stats.total_orders}</div></div>
                <div class="stat-card"><h3>Выполнено</h3><div class="value">${stats.completed_orders}</div></div>
                <div class="stat-card"><h3>В ожидании</h3><div class="value">${stats.pending_orders}</div></div>
                <div class="stat-card"><h3>Доход</h3><div class="value">$${stats.total_revenue}</div></div>`;
            if(orders)renderOrders(orders);
            if(topups)renderTopups(topups);
        }
        function renderOrders(orders){
            if(!orders.length){document.getElementById('orders').innerHTML='<div class="empty">✅ Нет заявок</div>';return}
            let html='<table><tr><th>ID</th><th>Пользователь</th><th>Тип</th><th>Кол-во</th><th>Страна</th><th>Срок</th><th>Цена</th><th>Действия</th></tr>';
            orders.forEach(o=>html+=`<tr><td>#${o.id}</td><td>${o.first_name}<br><small>@${o.username}</small></td><td>${o.proxy_type}</td><td>${o.quantity}</td><td>${o.country}</td><td>${o.duration}</td><td>$${o.price}</td><td><button class="btn btn-success" onclick="act('/api/order/${o.id}/status','in_progress')">✅</button><button class="btn btn-danger" onclick="act('/api/order/${o.id}/status','rejected')">❌</button></td></tr>`);
            html+='</table>';
            document.getElementById('orders').innerHTML=html;
        }
        function renderTopups(topups){
            if(!topups.length){document.getElementById('topups').innerHTML='<div class="empty">✅ Нет пополнений</div>';return}
            let html='<table><tr><th>ID</th><th>Пользователь</th><th>Сумма</th><th>Действия</th></tr>';
            topups.forEach(t=>html+=`<tr><td>#${t.id}</td><td>${t.first_name}<br><small>@${t.username}</small></td><td>$${t.amount}</td><td><button class="btn btn-success" onclick="act('/api/topup/${t.id}/approve')">✅</button><button class="btn btn-danger" onclick="act('/api/topup/${t.id}/reject')">❌</button></td></tr>`);
            html+='</table>';
            document.getElementById('topups').innerHTML=html;
        }
        async function act(url,status){
            const form=new FormData();
            if(status)form.append('status',status);
            form.append('password',password);
            await fetch(url,{method:'POST',body:form});
            loadAll();
        }
    </script>
</body>
</html>
    """
    return HTMLResponse(content=html)

@app.get("/api/stats")
async def api_stats(password: str = ""):
    if password != ADMIN_PASSWORD:
        raise HTTPException(403, "Неверный пароль")
    return await db.get_stats()

@app.get("/api/orders")
async def api_orders(password: str = ""):
    if password != ADMIN_PASSWORD:
        raise HTTPException(403)
    return await db.get_pending_orders()

@app.get("/api/topups")
async def api_topups(password: str = ""):
    if password != ADMIN_PASSWORD:
        raise HTTPException(403)
    return await db.get_pending_topups()

@app.post("/api/order/{order_id}/status")
async def api_update_order(order_id: int, status: str = Form(...), password: str = Form(...)):
    if password != ADMIN_PASSWORD:
        raise HTTPException(403)
    order = await db.get_order(order_id)
    if not order:
        raise HTTPException(404, "Заказ не найден")
    await db.update_order_status(order_id, status)
    if status == 'rejected':
        await db.execute('UPDATE users SET balance = balance + ? WHERE user_id = ?', [order['price'], order['user_id']], fetch=False)
    return {"success": True}

@app.post("/api/topup/{topup_id}/approve")
async def api_approve_topup(topup_id: int, password: str = Form(...)):
    if password != ADMIN_PASSWORD:
        raise HTTPException(403)
    result = await db.approve_topup(topup_id, 0)
    if not result:
        raise HTTPException(404, "Заявка не найдена")
    return {"success": True}

@app.post("/api/topup/{topup_id}/reject")
async def api_reject_topup(topup_id: int, password: str = Form(...)):
    if password != ADMIN_PASSWORD:
        raise HTTPException(403)
    result = await db.reject_topup(topup_id, 0)
    if not result:
        raise HTTPException(404, "Заявка не найдена")
    return {"success": True}

if __name__ == "__main__":
    import uvicorn
    
    # Запускаем FastAPI для админки в отдельном потоке
    def run_api():
        uvicorn.run(app, host="0.0.0.0", port=PORT, log_level="info")
    
    Thread(target=run_api, daemon=True).start()
    
    # Создаем и запускаем бота
    import asyncio
    
    async def run_bot():
        await db.init()
        
        # Удаляем вебхук
        import aiohttp
        async with aiohttp.ClientSession() as session:
            async with session.get(f"https://api.telegram.org/bot{BOT_TOKEN}/deleteWebhook") as resp:
                result = await resp.json()
                logger.info(f"Webhook deleted: {result}")
        
        # Создаем приложение
        app_bot = Application.builder().token(BOT_TOKEN).build()
        
        app_bot.add_handler(CommandHandler("start", start_command))
        app_bot.add_handler(CallbackQueryHandler(handle_callbacks))
        app_bot.add_handler(MessageHandler(filters.TEXT & ~filters.COMMAND, handle_all_messages))
        
        await app_bot.initialize()
        await app_bot.start()
        
        logger.info("Бот запущен в режиме polling")
        
        await app_bot.updater.start_polling(drop_pending_updates=True)
        
        # Бесконечный цикл
        while True:
            await asyncio.sleep(3600)
    
    asyncio.run(run_bot())
