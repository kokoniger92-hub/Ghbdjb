import os
import sys
import asyncio
import logging
from datetime import datetime
from threading import Thread
from contextlib import asynccontextmanager

import requests
from fastapi import FastAPI, Request, Form, HTTPException
from fastapi.responses import JSONResponse, HTMLResponse
from telegram import Update, InlineKeyboardButton, InlineKeyboardMarkup, ReplyKeyboardMarkup, KeyboardButton, WebAppInfo
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
APP_URL = os.getenv('APP_URL', 'https://ghbdjb-tusy.onrender.com')

user_states = {}
active_chats = {}  # user_id -> order_id (для чата с админом)

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
                    proxy_data TEXT DEFAULT '',
                    created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
                    updated_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
                );
                
                CREATE TABLE IF NOT EXISTS chat_messages (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    order_id INTEGER NOT NULL,
                    user_id INTEGER NOT NULL,
                    admin_id INTEGER NOT NULL,
                    message TEXT NOT NULL,
                    from_admin INTEGER DEFAULT 0,
                    created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
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
                
                CREATE TABLE IF NOT EXISTS broadcasts (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    admin_id INTEGER NOT NULL,
                    message TEXT NOT NULL,
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
                'Канада', 'Япония', 'Корея', 'Нидерланды', 'Польша'
            ]
            for country in default_countries:
                await db.execute('INSERT OR IGNORE INTO countries (name) VALUES (?)', (country,))
            
            await db.commit()
            logger.info("База данных инициализирована")
    
    async def execute(self, sql, params=None, fetch=True):
        async with aiosqlite.connect(self.path) as db:
            db.row_factory = aiosqlite.Row
            cursor = await db.execute(sql, params or [])
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
    
    async def get_user_orders(self, user_id, limit=50):
        return await self.execute(
            'SELECT * FROM orders WHERE user_id = ? ORDER BY created_at DESC LIMIT ?',
            [user_id, limit]
        )
    
    async def create_order(self, user_id, proxy_type, quantity, country, duration, price):
        order_id = await self.execute(
            'INSERT INTO orders (user_id, proxy_type, quantity, country, duration, price) VALUES (?, ?, ?, ?, ?, ?)',
            [user_id, proxy_type, quantity, country, duration, price], fetch=False
        )
        await self.execute(
            'UPDATE users SET balance = balance - ?, orders_count = orders_count + 1, total_spent = total_spent + ? WHERE user_id = ?',
            [price, price, user_id], fetch=False
        )
        return order_id
    
    async def cancel_order(self, order_id, user_id):
        order = await self.execute(
            'SELECT * FROM orders WHERE id = ? AND user_id = ? AND status = ?',
            [order_id, user_id, 'pending']
        )
        if not order:
            return False, "Заказ не найден"
        order = order[0]
        await self.execute("UPDATE orders SET status = 'cancelled' WHERE id = ?", [order_id], fetch=False)
        await self.execute('UPDATE users SET balance = balance + ? WHERE user_id = ?', [order['price'], user_id], fetch=False)
        return True, order['price']
    
    async def get_pending_orders(self):
        return await self.execute('''
            SELECT o.*, u.username, u.first_name 
            FROM orders o JOIN users u ON o.user_id = u.user_id 
            WHERE o.status = 'pending' ORDER BY o.created_at DESC LIMIT 50
        ''')
    
    async def get_in_progress_orders(self):
        return await self.execute('''
            SELECT o.*, u.username, u.first_name 
            FROM orders o JOIN users u ON o.user_id = u.user_id 
            WHERE o.status = 'in_progress' ORDER BY o.updated_at DESC LIMIT 50
        ''')
    
    async def get_order(self, order_id):
        result = await self.execute(
            'SELECT o.*, u.username, u.first_name FROM orders o JOIN users u ON o.user_id = u.user_id WHERE o.id = ?', [order_id]
        )
        return result[0] if result else None
    
    async def update_order_status(self, order_id, status, admin_id=None, proxy_data=''):
        if proxy_data:
            await self.execute('UPDATE orders SET status = ?, admin_id = ?, proxy_data = ?, updated_at = CURRENT_TIMESTAMP WHERE id = ?',
                             [status, admin_id, proxy_data, order_id], fetch=False)
        else:
            await self.execute('UPDATE orders SET status = ?, admin_id = ?, updated_at = CURRENT_TIMESTAMP WHERE id = ?',
                             [status, admin_id, order_id], fetch=False)
    
    async def get_pending_topups(self):
        return await self.execute('''
            SELECT t.*, u.username, u.first_name 
            FROM topups t JOIN users u ON t.user_id = u.user_id 
            WHERE t.status = 'pending' ORDER BY t.created_at DESC LIMIT 50
        ''')
    
    async def create_topup(self, user_id, amount):
        return await self.execute('INSERT INTO topups (user_id, amount) VALUES (?, ?)', [user_id, amount], fetch=False)
    
    async def approve_topup(self, topup_id, admin_id):
        topup = await self.execute('SELECT * FROM topups WHERE id = ? AND status = ?', [topup_id, 'pending'])
        if not topup:
            return None
        topup = topup[0]
        await self.execute("UPDATE topups SET status = 'completed', admin_id = ? WHERE id = ?", [admin_id, topup_id], fetch=False)
        await self.execute('UPDATE users SET balance = balance + ? WHERE user_id = ?', [topup['amount'], topup['user_id']], fetch=False)
        return topup
    
    async def reject_topup(self, topup_id, admin_id):
        topup = await self.execute('SELECT * FROM topups WHERE id = ? AND status = ?', [topup_id, 'pending'])
        if not topup:
            return None
        await self.execute("UPDATE topups SET status = 'rejected', admin_id = ? WHERE id = ?", [admin_id, topup_id], fetch=False)
        return topup[0]
    
    async def get_stats(self):
        users = await self.execute('SELECT COUNT(*) as c FROM users')
        orders = await self.execute('SELECT COUNT(*) as c FROM orders')
        completed = await self.execute("SELECT COUNT(*) as c FROM orders WHERE status='completed'")
        pending = await self.execute("SELECT COUNT(*) as c FROM orders WHERE status='pending'")
        in_progress = await self.execute("SELECT COUNT(*) as c FROM orders WHERE status='in_progress'")
        revenue = await self.execute("SELECT COALESCE(SUM(price),0) as t FROM orders WHERE status='completed'")
        return {
            'total_users': users[0]['c'], 'total_orders': orders[0]['c'],
            'completed_orders': completed[0]['c'], 'pending_orders': pending[0]['c'],
            'in_progress_orders': in_progress[0]['c'], 'total_revenue': round(revenue[0]['t'], 2)
        }
    
    async def get_all_users(self):
        return await self.execute('SELECT * FROM users ORDER BY created_at DESC LIMIT 200')
    
    async def get_countries(self):
        result = await self.execute('SELECT name FROM countries WHERE is_active = 1 ORDER BY name')
        return [r['name'] for r in result]
    
    async def add_country(self, name):
        await self.execute('INSERT OR IGNORE INTO countries (name) VALUES (?)', [name], fetch=False)
    
    async def remove_country(self, name):
        await self.execute('UPDATE countries SET is_active = 0 WHERE name = ?', [name], fetch=False)
    
    async def add_chat_message(self, order_id, user_id, admin_id, message, from_admin=0):
        await self.execute(
            'INSERT INTO chat_messages (order_id, user_id, admin_id, message, from_admin) VALUES (?, ?, ?, ?, ?)',
            [order_id, user_id, admin_id, message, from_admin], fetch=False
        )
    
    async def get_chat_messages(self, order_id):
        return await self.execute(
            'SELECT * FROM chat_messages WHERE order_id = ? ORDER BY created_at ASC', [order_id]
        )
    
    async def get_all_users_for_broadcast(self):
        result = await self.execute('SELECT user_id FROM users WHERE is_admin = 0 AND is_banned = 0')
        return [r['user_id'] for r in result]

db = Database()

# Клавиатура админа (только админ видит бота полноценно)
ADMIN_KEYBOARD = ReplyKeyboardMarkup([
    [KeyboardButton("📋 Новые заявки"), KeyboardButton("🔄 Заявки в работе")],
    [KeyboardButton("💰 Пополнения"), KeyboardButton("💬 Чат с клиентом")],
    [KeyboardButton("📊 Статистика"), KeyboardButton("👥 Пользователи")],
    [KeyboardButton("📢 Рассылка"), KeyboardButton("⚙️ Настройки")],
    [KeyboardButton("🌍 Страны")]
], resize_keyboard=True)

CANCEL_KEYBOARD = ReplyKeyboardMarkup([[KeyboardButton("🔙 Отмена")]], resize_keyboard=True)

# Команда /start
async def start_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user = update.effective_user
    await db.create_user(user.id, user.username, user.first_name, user.last_name)
    
    if user.id in ADMIN_IDS:
        welcome = await db.get_setting('welcome_message', 'Добро пожаловать!')
        await update.message.reply_text(
            f"🔧 *Админ-панель*\n\n{welcome}\n\n"
            f"👥 Пользователи заходят через Mini App\n"
            f"📢 Ты можешь делать рассылку\n"
            f"💬 Чат с клиентами внутри бота",
            reply_markup=ADMIN_KEYBOARD, parse_mode=ParseMode.MARKDOWN
        )
    else:
        # Обычные пользователи видят только приглашение в Mini App
        await update.message.reply_text(
            f"👋 Добро пожаловать в магазин прокси!\n\n"
            f"🛒 Для заказа нажмите кнопку ниже:",
            reply_markup=ReplyKeyboardMarkup([
                [KeyboardButton("🛒 Открыть магазин", web_app=WebAppInfo(url=f"{APP_URL}/app"))],
                [KeyboardButton("📋 Мои заказы"), KeyboardButton("💳 Баланс")],
                [KeyboardButton("💰 Пополнить"), KeyboardButton("ℹ️ Информация")]
            ], resize_keyboard=True)
        )

# Обработка сообщений
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
    
    # Если админ в режиме чата
    if user.id in ADMIN_IDS and user.id in active_chats:
        await handle_chat_message(update, context)
        return
    
    # Обработка состояний
    if user.id in user_states:
        await handle_state_message(update, context)
        return
    
    if user.id in ADMIN_IDS:
        await handle_admin_menu(update, context, text)
    else:
        await handle_user_menu(update, context, text)

async def handle_chat_message(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user = update.effective_user
    text = update.message.text.strip()
    
    if text == "🔙 Выйти из чата":
        del active_chats[user.id]
        await update.message.reply_text("Вы вышли из чата.", reply_markup=ADMIN_KEYBOARD)
        return
    
    order_id = active_chats[user.id]
    order = await db.get_order(order_id)
    
    if not order:
        del active_chats[user.id]
        await update.message.reply_text("Заказ не найден.", reply_markup=ADMIN_KEYBOARD)
        return
    
    # Сохраняем сообщение
    await db.add_chat_message(order_id, order['user_id'], user.id, text, from_admin=1)
    
    # Отправляем клиенту
    try:
        await context.bot.send_message(
            order['user_id'],
            f"💬 *Сообщение от поддержки*\n\n{text}",
            parse_mode=ParseMode.MARKDOWN
        )
        await update.message.reply_text(f"✅ Отправлено клиенту #{order['user_id']}")
    except Exception as e:
        await update.message.reply_text(f"❌ Ошибка отправки: {e}")

async def handle_state_message(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user = update.effective_user
    text = update.message.text.strip()
    state = user_states.get(user.id)
    
    if not state:
        return
    
    if text == "🔙 Отмена":
        del user_states[user.id]
        keyboard = ADMIN_KEYBOARD if user.id in ADMIN_IDS else ReplyKeyboardMarkup([
            [KeyboardButton("🛒 Открыть магазин", web_app=WebAppInfo(url=f"{APP_URL}/app"))],
            [KeyboardButton("📋 Мои заказы"), KeyboardButton("💳 Баланс")],
            [KeyboardButton("💰 Пополнить")]
        ], resize_keyboard=True)
        await update.message.reply_text("❌ Отменено", reply_markup=keyboard)
        return
    
    if state.get('action') == 'awaiting_topup':
        try:
            amount = float(text)
            if amount < 1 or amount > 1000:
                await update.message.reply_text("❌ От $1 до $1000:")
                return
            topup_id = await db.create_topup(user.id, amount)
            del user_states[user.id]
            await update.message.reply_text(
                f"✅ Заявка #{topup_id} на ${amount:.2f} создана!\n⏳ Ожидайте подтверждения.",
                reply_markup=ReplyKeyboardMarkup([
                    [KeyboardButton("🛒 Открыть магазин", web_app=WebAppInfo(url=f"{APP_URL}/app"))],
                    [KeyboardButton("📋 Мои заказы"), KeyboardButton("💳 Баланс")]
                ], resize_keyboard=True)
            )
            for aid in ADMIN_IDS:
                try:
                    await context.bot.send_message(aid, f"💰 Пополнение #{topup_id}\n👤 {user.first_name}\n💵 ${amount:.2f}")
                except: pass
        except ValueError:
            await update.message.reply_text("❌ Введите число:")
    
    elif state.get('action') == 'awaiting_broadcast':
        users = await db.get_all_users_for_broadcast()
        success = 0
        for uid in users:
            try:
                await context.bot.send_message(uid, text)
                success += 1
                await asyncio.sleep(0.05)
            except: pass
        del user_states[user.id]
        await update.message.reply_text(f"📢 Рассылка отправлена!\n✅ Доставлено: {success}/{len(users)}", reply_markup=ADMIN_KEYBOARD)
    
    elif state.get('action') == 'awaiting_welcome':
        await db.set_setting('welcome_message', text)
        del user_states[user.id]
        await update.message.reply_text("✅ Приветствие обновлено!", reply_markup=ADMIN_KEYBOARD)
    
    elif state.get('action') == 'awaiting_price':
        try:
            price = float(text)
            await db.set_setting(f'base_price_{state["type"]}', str(price))
            del user_states[user.id]
            await update.message.reply_text(f"✅ Цена обновлена: ${price:.2f}", reply_markup=ADMIN_KEYBOARD)
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
    
    elif state.get('action') == 'awaiting_proxy_data':
        order_id = state.get('order_id')
        await db.update_order_status(order_id, 'completed', user.id, text)
        order = await db.get_order(order_id)
        del user_states[user.id]
        
        if order:
            try:
                await context.bot.send_message(
                    order['user_id'],
                    f"✅ *Заказ #{order_id} выполнен!*\n\n"
                    f"📦 Прокси данные:\n`{text}`\n\n"
                    f"Спасибо за заказ!",
                    parse_mode=ParseMode.MARKDOWN
                )
            except: pass
        
        await update.message.reply_text(f"✅ Заказ #{order_id} завершен!\nДанные отправлены клиенту.", reply_markup=ADMIN_KEYBOARD)

async def handle_admin_menu(update: Update, context, text):
    user = update.effective_user
    
    if text == "📋 Новые заявки":
        orders = await db.get_pending_orders()
        if not orders:
            await update.message.reply_text("✅ Нет новых заявок"); return
        
        for order in orders:
            msg = (
                f"📋 *Заявка #{order['id']}*\n"
                f"👤 {order['first_name']} (@{order['username']})\n"
                f"🆔 `{order['user_id']}`\n"
                f"📦 {order['proxy_type'].upper()} × {order['quantity']}\n"
                f"🌍 {order['country']} | ⏱ {order['duration']}\n"
                f"💰 ${order['price']:.2f}\n📅 {order['created_at'][:19]}"
            )
            keyboard = [[
                InlineKeyboardButton("✅ Взять в работу", callback_data=f"take_{order['id']}"),
                InlineKeyboardButton("❌ Отклонить", callback_data=f"reject_{order['id']}")
            ]]
            await update.message.reply_text(msg, reply_markup=InlineKeyboardMarkup(keyboard), parse_mode=ParseMode.MARKDOWN)
    
    elif text == "🔄 Заявки в работе":
        orders = await db.get_in_progress_orders()
        if not orders:
            await update.message.reply_text("✅ Нет заявок в работе"); return
        
        for order in orders:
            msg = (
                f"🔄 *Заявка #{order['id']}*\n"
                f"👤 {order['first_name']} (@{order['username']})\n"
                f"📦 {order['proxy_type'].upper()} × {order['quantity']}\n"
                f"🌍 {order['country']} | ⏱ {order['duration']}\n"
                f"💰 ${order['price']:.2f}"
            )
            keyboard = [[
                InlineKeyboardButton("✅ Выполнено", callback_data=f"complete_{order['id']}"),
                InlineKeyboardButton("💬 Чат", callback_data=f"chat_{order['id']}")
            ]]
            await update.message.reply_text(msg, reply_markup=InlineKeyboardMarkup(keyboard), parse_mode=ParseMode.MARKDOWN)
    
    elif text == "💰 Пополнения":
        topups = await db.get_pending_topups()
        if not topups:
            await update.message.reply_text("✅ Нет пополнений"); return
        
        for t in topups:
            user_info = await db.get_user(t['user_id'])
            bal = user_info['balance'] if user_info else 0
            msg = f"💰 *Пополнение #{t['id']}*\n👤 {t['first_name']} (@{t['username']})\n💵 ${t['amount']:.2f}\n💳 Баланс: ${bal:.2f}"
            keyboard = [[
                InlineKeyboardButton("✅ Подтвердить", callback_data=f"approve_topup_{t['id']}"),
                InlineKeyboardButton("❌ Отклонить", callback_data=f"reject_topup_{t['id']}")
            ]]
            await update.message.reply_text(msg, reply_markup=InlineKeyboardMarkup(keyboard), parse_mode=ParseMode.MARKDOWN)
    
    elif text == "💬 Чат с клиентом":
        await update.message.reply_text(
            "Введите ID заказа для чата:",
            reply_markup=ReplyKeyboardMarkup([[KeyboardButton("🔙 Отмена")]], resize_keyboard=True)
        )
        user_states[user.id] = {'action': 'awaiting_chat_order'}
    
    elif text == "📊 Статистика":
        s = await db.get_stats()
        msg = f"📊 *Статистика*\n\n👥 Пользователей: {s['total_users']}\n📦 Заказов: {s['total_orders']}\n✅ Выполнено: {s['completed_orders']}\n🔄 В работе: {s['in_progress_orders']}\n⏳ Ожидают: {s['pending_orders']}\n💰 Доход: ${s['total_revenue']:.2f}"
        await update.message.reply_text(msg, parse_mode=ParseMode.MARKDOWN)
    
    elif text == "👥 Пользователи":
        users = await db.get_all_users()
        msg = "👥 *Пользователи:*\n\n"
        for u in users[:30]:
            ban = "🚫" if u['is_banned'] else "✅"
            uname = f" (@{u['username']})" if u['username'] else ""
            msg += f"{ban} {u['first_name'] or '—'}{uname} | 💳 ${u['balance']:.2f} | ID: `{u['user_id']}`\n"
        await update.message.reply_text(msg, parse_mode=ParseMode.MARKDOWN)
    
    elif text == "📢 Рассылка":
        user_states[user.id] = {'action': 'awaiting_broadcast'}
        await update.message.reply_text("📢 Введите текст рассылки:", reply_markup=CANCEL_KEYBOARD)
    
    elif text == "⚙️ Настройки":
        enabled = await db.get_setting('orders_enabled')
        status = "✅" if enabled == 'true' else "❌"
        prices = {
            'http': await db.get_setting('base_price_http', '2.0'),
            'socks5': await db.get_setting('base_price_socks5', '2.5'),
            'residential': await db.get_setting('base_price_residential', '5.0'),
            'datacenter': await db.get_setting('base_price_datacenter', '1.5')
        }
        msg = f"⚙️ *Настройки*\nЗаказы: {status}\n\n*Цены:*\nHTTP: ${prices['http']}\nSOCKS5: ${prices['socks5']}\nResidential: ${prices['residential']}\nDatacenter: ${prices['datacenter']}"
        keyboard = [
            [InlineKeyboardButton("🔄 Заказы", callback_data="toggle_orders")],
            [InlineKeyboardButton("✏️ Приветствие", callback_data="edit_welcome")],
            [InlineKeyboardButton("💵 HTTP", callback_data="setprice_http"), InlineKeyboardButton("💵 SOCKS5", callback_data="setprice_socks5")],
            [InlineKeyboardButton("💵 Residential", callback_data="setprice_residential"), InlineKeyboardButton("💵 Datacenter", callback_data="setprice_datacenter")]
        ]
        await update.message.reply_text(msg, reply_markup=InlineKeyboardMarkup(keyboard), parse_mode=ParseMode.MARKDOWN)
    
    elif text == "🌍 Страны":
        countries = await db.get_countries()
        msg = "🌍 *Страны:*\n" + "\n".join(f"• {c}" for c in countries)
        keyboard = [[InlineKeyboardButton("➕ Добавить", callback_data="add_country"), InlineKeyboardButton("➖ Удалить", callback_data="remove_country")]]
        await update.message.reply_text(msg, reply_markup=InlineKeyboardMarkup(keyboard), parse_mode=ParseMode.MARKDOWN)
    
    elif text == "🔙 Отмена":
        await update.message.reply_text("🔧 Админ-панель", reply_markup=ADMIN_KEYBOARD)

async def handle_user_menu(update: Update, context, text):
    user = update.effective_user
    
    if text == "📋 Мои заказы":
        orders = await db.get_user_orders(user.id)
        if not orders:
            await update.message.reply_text("📭 Нет заказов"); return
        for order in orders:
            emoji = {'pending':'⏳','in_progress':'🔄','completed':'✅','cancelled':'❌','rejected':'🚫'}.get(order['status'],'❓')
            msg = f"{emoji} *Заказ #{order['id']}*\n📦 {order['proxy_type'].upper()} × {order['quantity']}\n🌍 {order['country']} | ⏱ {order['duration']}\n💰 ${order['price']:.2f}"
            if order['status'] == 'completed' and order['proxy_data']:
                msg += f"\n📋 Данные: `{order['proxy_data']}`"
            if order['status'] == 'pending':
                keyboard = InlineKeyboardMarkup([[InlineKeyboardButton("❌ Отменить", callback_data=f"cancel_{order['id']}")]])
                await update.message.reply_text(msg, reply_markup=keyboard, parse_mode=ParseMode.MARKDOWN)
            else:
                await update.message.reply_text(msg, parse_mode=ParseMode.MARKDOWN)
    
    elif text == "💳 Баланс":
        ud = await db.get_user(user.id)
        bal = ud['balance'] if ud else 0
        await update.message.reply_text(f"💳 *Баланс: ${bal:.2f}*", parse_mode=ParseMode.MARKDOWN)
    
    elif text == "💰 Пополнить":
        user_states[user.id] = {'action': 'awaiting_topup'}
        await update.message.reply_text("💰 Введите сумму USD:", reply_markup=CANCEL_KEYBOARD)
    
    elif text == "ℹ️ Информация":
        await update.message.reply_text("ℹ️ Магазин прокси.\nТипы: HTTP, SOCKS5, Residential, Datacenter\nСроки: 1 день - 3 месяца")

async def handle_callbacks(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    user = query.from_user
    data = query.data
    await query.answer()
    
    if data == "cancel_action":
        if user.id in user_states: del user_states[user.id]
        await query.edit_message_text("❌ Отменено"); return
    
    if data.startswith("take_"):
        oid = int(data.split("_")[1])
        await db.update_order_status(oid, 'in_progress', user.id)
        order = await db.get_order(oid)
        await query.edit_message_text(f"{query.message.text}\n\n✅ *Взято в работу*", parse_mode=ParseMode.MARKDOWN)
        if order:
            await context.bot.send_message(order['user_id'], f"✅ Заказ #{oid} в работе!\n💬 С вами свяжется поддержка.")
    
    elif data.startswith("reject_"):
        oid = int(data.split("_")[1])
        order = await db.get_order(oid)
        if order:
            await db.update_order_status(oid, 'rejected', user.id)
            await db.execute('UPDATE users SET balance = balance + ? WHERE user_id = ?', [order['price'], order['user_id']], fetch=False)
            await query.edit_message_text(f"{query.message.text}\n\n❌ *Отклонено*", parse_mode=ParseMode.MARKDOWN)
            await context.bot.send_message(order['user_id'], f"❌ Заказ #{oid} отклонен. 💰 ${order['price']:.2f} возвращены.")
    
    elif data.startswith("complete_"):
        oid = int(data.split("_")[1])
        user_states[user.id] = {'action': 'awaiting_proxy_data', 'order_id': oid}
        await query.edit_message_text(f"{query.message.text}\n\n📝 *Введите данные прокси для отправки клиенту:*", parse_mode=ParseMode.MARKDOWN)
    
    elif data.startswith("chat_"):
        oid = int(data.split("_")[1])
        active_chats[user.id] = oid
        order = await db.get_order(oid)
        messages = await db.get_chat_messages(oid)
        history = "💬 *Чат по заказу #{0}*\n\n".format(oid)
        for m in messages[-20:]:
            sender = "Вы" if m['from_admin'] else "Клиент"
            history += f"*{sender}*: {m['message']}\n"
        history += "\n_Введите сообщение для клиента_"
        await query.edit_message_text(
            history,
            parse_mode=ParseMode.MARKDOWN,
            reply_markup=InlineKeyboardMarkup([[InlineKeyboardButton("🔙 Выйти из чата", callback_data="close_chat")]])
        )
    
    elif data == "close_chat":
        if user.id in active_chats: del active_chats[user.id]
        await query.edit_message_text("💬 Чат закрыт")
    
    elif data.startswith("cancel_"):
        oid = int(data.split("_")[1])
        success, result = await db.cancel_order(oid, user.id)
        if success:
            await query.edit_message_text(f"❌ *Отменено*\n💰 +${result:.2f}", parse_mode=ParseMode.MARKDOWN)
        else:
            await query.answer(result, show_alert=True)
    
    elif data.startswith("approve_topup_"):
        tid = int(data.split("_")[2])
        topup = await db.approve_topup(tid, user.id)
        if topup:
            await query.edit_message_text(f"✅ *Подтверждено*", parse_mode=ParseMode.MARKDOWN)
            await context.bot.send_message(topup['user_id'], f"✅ Пополнение #{tid} на ${topup['amount']:.2f} зачислено!")
    
    elif data.startswith("reject_topup_"):
        tid = int(data.split("_")[2])
        topup = await db.reject_topup(tid, user.id)
        if topup:
            await query.edit_message_text(f"❌ *Отклонено*", parse_mode=ParseMode.MARKDOWN)
            await context.bot.send_message(topup['user_id'], f"❌ Пополнение #{tid} отклонено.")
    
    elif data == "toggle_orders":
        cur = await db.get_setting('orders_enabled')
        new = 'false' if cur == 'true' else 'true'
        await db.set_setting('orders_enabled', new)
        await query.edit_message_text(f"⚙️ Заказы: {'✅' if new == 'true' else '❌'}")
    
    elif data == "edit_welcome":
        user_states[user.id] = {'action': 'awaiting_welcome'}
        await query.edit_message_text("✏️ Введите приветствие:")
    
    elif data.startswith("setprice_"):
        pt = data.split("_")[1]
        user_states[user.id] = {'action': 'awaiting_price', 'type': pt}
        await query.edit_message_text(f"💵 Цена для {pt.upper()}:")
    
    elif data == "add_country":
        user_states[user.id] = {'action': 'awaiting_new_country'}
        await query.edit_message_text("🌍 Название страны:")
    
    elif data == "remove_country":
        user_states[user.id] = {'action': 'awaiting_remove_country'}
        await query.edit_message_text("🌍 Страна для удаления:")

# FastAPI
@asynccontextmanager
async def lifespan(app: FastAPI):
    await db.init()
    yield

app = FastAPI(lifespan=lifespan, title="Proxy Bot")

@app.get("/")
async def root():
    return {"status": "ok"}

@app.get("/health")
async def health():
    return {"status": "healthy"}

@app.get("/app", response_class=HTMLResponse)
async def user_app():
    html = """
<!DOCTYPE html><html lang="ru"><head><meta charset="UTF-8"><meta name="viewport" content="width=device-width, initial-scale=1.0"><title>Магазин прокси</title>
<script src="https://telegram.org/js/telegram-web-app.js"></script>
<style>*{margin:0;padding:0;box-sizing:border-box}body{font-family:-apple-system,sans-serif;background:var(--tg-theme-bg-color,#0f172a);color:var(--tg-theme-text-color,#e2e8f0);padding:16px}.container{max-width:500px;margin:0 auto}h1{text-align:center;margin-bottom:24px;color:var(--tg-theme-button-color,#60a5fa)}.card{background:var(--tg-theme-secondary-bg-color,#1e293b);padding:20px;border-radius:16px;margin-bottom:16px}.card h2{font-size:18px;margin-bottom:12px}.btn{display:block;width:100%;padding:14px;margin:8px 0;background:var(--tg-theme-button-color,#3b82f6);color:var(--tg-theme-button-text-color,white);border:none;border-radius:12px;font-size:16px;font-weight:600;cursor:pointer}.balance{font-size:2em;font-weight:bold;text-align:center;color:#22c55e;margin:16px 0}.grid{display:grid;grid-template-columns:1fr 1fr;gap:10px}.order-card{background:var(--tg-theme-secondary-bg-color,#1e293b);padding:16px;border-radius:12px;margin-bottom:10px}.status{padding:4px 10px;border-radius:12px;font-size:12px;font-weight:600}.status-pending{background:#f59e0b;color:#000}.status-completed{background:#22c55e;color:#fff}.status-in_progress{background:#3b82f6;color:#fff}input,select{width:100%;padding:12px;margin:8px 0;background:var(--tg-theme-bg-color,#334155);border:2px solid var(--tg-theme-hint-color,#475569);border-radius:10px;color:var(--tg-theme-text-color,#e2e8f0);font-size:15px}.hidden{display:none}.tabs{display:flex;gap:10px;margin-bottom:20px}.tab{flex:1;padding:12px;text-align:center;background:var(--tg-theme-secondary-bg-color,#1e293b);border-radius:10px;cursor:pointer;font-weight:600}.tab.active{background:var(--tg-theme-button-color,#3b82f6);color:white}</style></head>
<body><div class="container"><h1>🛒 Магазин прокси</h1>
<div class="tabs"><div class="tab active" onclick="showTab('shop')">Заказать</div><div class="tab" onclick="showTab('orders')">Мои заказы</div></div>
<div id="shopTab"><div class="card"><h2>Новый заказ</h2>
<select id="proxyType"><option value="http">HTTP</option><option value="socks5">SOCKS5</option><option value="residential">Residential</option><option value="datacenter">Datacenter</option></select>
<input type="number" id="quantity" placeholder="Количество (1-100)" min="1" max="100" value="1">
<select id="country"></select>
<select id="duration"><option value="1 день">1 день</option><option value="1 неделя">1 неделя</option><option value="1 месяц">1 месяц</option><option value="3 месяца">3 месяца</option></select>
<div style="text-align:center;margin:16px 0;font-size:1.2em">💰 <b id="price">$0.00</b></div>
<div class="balance" id="balance">💳 $0.00</div>
<button class="btn" onclick="createOrder()">🛒 Заказать</button></div></div>
<div id="ordersTab" class="hidden"><div id="ordersList"></div></div></div>
<script>
const tg=window.Telegram.WebApp;tg.ready();tg.expand();
let userId=null,balance=0,countries=[],prices={};
if(tg.initDataUnsafe&&tg.initDataUnsafe.user)userId=tg.initDataUnsafe.user.id;
async function api(url,method='GET',body=null){const o={method,headers:{}};if(body){o.headers['Content-Type']='application/json';o.body=JSON.stringify(body)}return(await fetch(url,o)).json()}
async function load(){const[c,p]=await Promise.all([api('/api/countries'),api('/api/prices')]);countries=c.countries||[];prices=p.prices||{};document.getElementById('country').innerHTML=countries.map(c=>`<option>${c}</option>`).join('');updatePrice();if(userId){const u=await api('/api/user/'+userId);if(u.balance!==undefined){balance=u.balance;document.getElementById('balance').textContent=`💳 $${balance.toFixed(2)}`}}}
function updatePrice(){const t=document.getElementById('proxyType').value,q=parseInt(document.getElementById('quantity').value)||1,d=document.getElementById('duration').value,m={'1 день':1,'1 неделя':6,'1 месяц':20,'3 месяца':50};document.getElementById('price').textContent=`$${((prices[t]||2)*q*(m[d]||1)).toFixed(2)}`}
document.getElementById('proxyType').onchange=updatePrice;document.getElementById('quantity').oninput=updatePrice;document.getElementById('duration').onchange=updatePrice;
async function createOrder(){if(!userId){tg.showPopup({title:'Ошибка',message:'Откройте через Telegram бота'});return}
const t=document.getElementById('proxyType').value,q=parseInt(document.getElementById('quantity').value),c=document.getElementById('country').value,d=document.getElementById('duration').value,m={'1 день':1,'1 неделя':6,'1 месяц':20,'3 месяца':50},total=(prices[t]||2)*q*(m[d]||1);
if(balance<total){tg.showPopup({title:'Недостаточно средств',message:`Баланс: $${balance.toFixed(2)}\nНужно: $${total.toFixed(2)}`});return}
const r=await api('/api/create_order','POST',{user_id:userId,proxy_type:t,quantity:q,country:c,duration:d,price:total});
if(r.success){balance=r.new_balance;document.getElementById('balance').textContent=`💳 $${balance.toFixed(2)}`;tg.showPopup({title:'✅ Успешно!',message:`Заказ #${r.order_id} создан!\nОжидайте обработки.`})}else tg.showPopup({title:'Ошибка',message:r.error||'Ошибка'})}
async function showTab(tab){document.querySelectorAll('.tab').forEach(t=>t.classList.remove('active'));event.target.classList.add('active');document.getElementById('shopTab').classList.add('hidden');document.getElementById('ordersTab').classList.add('hidden');
if(tab==='shop')document.getElementById('shopTab').classList.remove('hidden');else{document.getElementById('ordersTab').classList.remove('hidden');await loadOrders()}}
async function loadOrders(){if(!userId)return;const d=await api('/api/user/'+userId+'/orders');const o=d.orders||[];const e={'pending':'⏳','in_progress':'🔄','completed':'✅','cancelled':'❌','rejected':'🚫'};
document.getElementById('ordersList').innerHTML=o.length?o.map(o=>`<div class="order-card"><div style="display:flex;justify-content:space-between"><b>#${o.id}</b><span class="status status-${o.status}">${e[o.status]||'❓'} ${o.status}</span></div><div>📦 ${o.proxy_type.toUpperCase()} × ${o.quantity}</div><div>🌍 ${o.country} | ⏱ ${o.duration}</div><div>💰 $${o.price.toFixed(2)}</div>${o.status==='completed'&&o.proxy_data?`<div style="margin-top:8px;padding:8px;background:#334155;border-radius:8px">📋 <code>${o.proxy_data}</code></div>`:''}</div>`).join(''):'<div class="card" style="text-align:center">📭 Нет заказов</div>'}
load();</script></body></html>"""
    return HTMLResponse(content=html)

@app.get("/api/countries")
async def api_countries():
    return {"countries": await db.get_countries()}

@app.get("/api/prices")
async def api_prices():
    return {"prices": {
        'http': float(await db.get_setting('base_price_http','2.0')),
        'socks5': float(await db.get_setting('base_price_socks5','2.5')),
        'residential': float(await db.get_setting('base_price_residential','5.0')),
        'datacenter': float(await db.get_setting('base_price_datacenter','1.5'))
    }}

@app.get("/api/user/{user_id}")
async def api_user(user_id: int):
    u = await db.get_user(user_id)
    return {"balance": u['balance'] if u else 0}

@app.get("/api/user/{user_id}/orders")
async def api_user_orders(user_id: int):
    return {"orders": await db.get_user_orders(user_id)}

@app.post("/api/create_order")
async def api_create_order(request: Request):
    data = await request.json()
    user_id, proxy_type, quantity, country, duration, price = data['user_id'], data['proxy_type'], data['quantity'], data['country'], data['duration'], data['price']
    user = await db.get_user(user_id)
    if not user: return {"success": False, "error": "Пользователь не найден"}
    if user['balance'] < price: return {"success": False, "error": "Недостаточно средств"}
    oid = await db.create_order(user_id, proxy_type, quantity, country, duration, price)
    for aid in ADMIN_IDS:
        try: await telegram_app.bot.send_message(aid, f"📋 Новый заказ #{oid}\n👤 {user['first_name']}\n📦 {proxy_type.upper()} × {quantity}\n💰 ${price:.2f}")
        except: pass
    return {"success": True, "order_id": oid, "new_balance": user['balance'] - price}

if __name__ == "__main__":
    import uvicorn
    Thread(target=lambda: uvicorn.run(app, host="0.0.0.0", port=PORT, log_level="info"), daemon=True).start()
    try: requests.get(f"https://api.telegram.org/bot{BOT_TOKEN}/deleteWebhook")
    except: pass
    
    async def run_bot():
        global telegram_app
        await db.init()
        telegram_app = Application.builder().token(BOT_TOKEN).build()
        telegram_app.add_handler(CommandHandler("start", start_command))
        telegram_app.add_handler(CallbackQueryHandler(handle_callbacks))
        telegram_app.add_handler(MessageHandler(filters.TEXT & ~filters.COMMAND, handle_all_messages))
        await telegram_app.initialize()
        await telegram_app.start()
        logger.info("Бот запущен!")
        await telegram_app.updater.start_polling(drop_pending_updates=True)
        while True: await asyncio.sleep(3600)
    
    telegram_app = None
    asyncio.run(run_bot())
