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
active_chats = {}

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
                
                CREATE TABLE IF NOT EXISTS mirrors (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    name TEXT NOT NULL,
                    token TEXT NOT NULL,
                    url TEXT NOT NULL,
                    is_active INTEGER DEFAULT 1,
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
            [user_id, username or '', first_name or '', last_name or '', 1 if user_id in ADMIN_IDS else 0], fetch=False
        )
    
    async def get_user_orders(self, user_id, limit=50):
        return await self.execute('SELECT * FROM orders WHERE user_id = ? ORDER BY created_at DESC LIMIT ?', [user_id, limit])
    
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
        order = await self.execute('SELECT * FROM orders WHERE id = ? AND user_id = ? AND status = ?', [order_id, user_id, 'pending'])
        if not order: return False, "Заказ не найден"
        order = order[0]
        await self.execute("UPDATE orders SET status = 'cancelled' WHERE id = ?", [order_id], fetch=False)
        await self.execute('UPDATE users SET balance = balance + ? WHERE user_id = ?', [order['price'], user_id], fetch=False)
        return True, order['price']
    
    async def get_pending_orders(self):
        return await self.execute("SELECT o.*, u.username, u.first_name FROM orders o JOIN users u ON o.user_id = u.user_id WHERE o.status = 'pending' ORDER BY o.created_at DESC LIMIT 50")
    
    async def get_in_progress_orders(self):
        return await self.execute("SELECT o.*, u.username, u.first_name FROM orders o JOIN users u ON o.user_id = u.user_id WHERE o.status = 'in_progress' ORDER BY o.updated_at DESC LIMIT 50")
    
    async def get_order(self, order_id):
        result = await self.execute('SELECT o.*, u.username, u.first_name FROM orders o JOIN users u ON o.user_id = u.user_id WHERE o.id = ?', [order_id])
        return result[0] if result else None
    
    async def update_order_status(self, order_id, status, admin_id=None, proxy_data=''):
        if proxy_data:
            await self.execute('UPDATE orders SET status = ?, admin_id = ?, proxy_data = ?, updated_at = CURRENT_TIMESTAMP WHERE id = ?', [status, admin_id, proxy_data, order_id], fetch=False)
        else:
            await self.execute('UPDATE orders SET status = ?, admin_id = ?, updated_at = CURRENT_TIMESTAMP WHERE id = ?', [status, admin_id, order_id], fetch=False)
    
    async def get_pending_topups(self):
        return await self.execute("SELECT t.*, u.username, u.first_name FROM topups t JOIN users u ON t.user_id = u.user_id WHERE t.status = 'pending' ORDER BY t.created_at DESC LIMIT 50")
    
    async def create_topup(self, user_id, amount):
        return await self.execute('INSERT INTO topups (user_id, amount) VALUES (?, ?)', [user_id, amount], fetch=False)
    
    async def approve_topup(self, topup_id, admin_id):
        topup = await self.execute('SELECT * FROM topups WHERE id = ? AND status = ?', [topup_id, 'pending'])
        if not topup: return None
        topup = topup[0]
        await self.execute("UPDATE topups SET status = 'completed', admin_id = ? WHERE id = ?", [admin_id, topup_id], fetch=False)
        await self.execute('UPDATE users SET balance = balance + ? WHERE user_id = ?', [topup['amount'], topup['user_id']], fetch=False)
        return topup
    
    async def reject_topup(self, topup_id, admin_id):
        topup = await self.execute('SELECT * FROM topups WHERE id = ? AND status = ?', [topup_id, 'pending'])
        if not topup: return None
        await self.execute("UPDATE topups SET status = 'rejected', admin_id = ? WHERE id = ?", [admin_id, topup_id], fetch=False)
        return topup[0]
    
    async def get_stats(self):
        u = await self.execute('SELECT COUNT(*) as c FROM users')
        o = await self.execute('SELECT COUNT(*) as c FROM orders')
        c = await self.execute("SELECT COUNT(*) as c FROM orders WHERE status='completed'")
        p = await self.execute("SELECT COUNT(*) as c FROM orders WHERE status='pending'")
        ip = await self.execute("SELECT COUNT(*) as c FROM orders WHERE status='in_progress'")
        r = await self.execute("SELECT COALESCE(SUM(price),0) as t FROM orders WHERE status='completed'")
        return {'total_users': u[0]['c'], 'total_orders': o[0]['c'], 'completed_orders': c[0]['c'], 'pending_orders': p[0]['c'], 'in_progress_orders': ip[0]['c'], 'total_revenue': round(r[0]['t'], 2)}
    
    async def get_all_users(self):
        return await self.execute('SELECT * FROM users ORDER BY created_at DESC LIMIT 200')
    
    async def get_countries(self):
        result = await self.execute('SELECT name FROM countries WHERE is_active = 1 ORDER BY name')
        return [r['name'] for r in result]
    
    async def add_country(self, name): await self.execute('INSERT OR IGNORE INTO countries (name) VALUES (?)', [name], fetch=False)
    async def remove_country(self, name): await self.execute('UPDATE countries SET is_active = 0 WHERE name = ?', [name], fetch=False)
    
    async def add_chat_message(self, order_id, user_id, admin_id, message, from_admin=0):
        await self.execute('INSERT INTO chat_messages (order_id, user_id, admin_id, message, from_admin) VALUES (?, ?, ?, ?, ?)', [order_id, user_id, admin_id, message, from_admin], fetch=False)
    
    async def get_chat_messages(self, order_id):
        return await self.execute('SELECT * FROM chat_messages WHERE order_id = ? ORDER BY created_at ASC', [order_id])
    
    async def get_all_users_for_broadcast(self):
        result = await self.execute('SELECT user_id FROM users WHERE is_admin = 0 AND is_banned = 0')
        return [r['user_id'] for r in result]
    
    async def add_mirror(self, name, token, url):
        return await self.execute('INSERT INTO mirrors (name, token, url) VALUES (?, ?, ?)', [name, token, url], fetch=False)
    
    async def get_mirrors(self):
        return await self.execute('SELECT * FROM mirrors ORDER BY created_at DESC')
    
    async def toggle_mirror(self, mirror_id):
        mirror = await self.execute('SELECT * FROM mirrors WHERE id = ?', [mirror_id])
        if mirror:
            new_status = 0 if mirror[0]['is_active'] else 1
            await self.execute('UPDATE mirrors SET is_active = ? WHERE id = ?', [new_status, mirror_id], fetch=False)
            return new_status
        return None
    
    async def delete_mirror(self, mirror_id):
        await self.execute('DELETE FROM mirrors WHERE id = ?', [mirror_id], fetch=False)
    
    async def get_active_mirrors(self):
        return await self.execute('SELECT * FROM mirrors WHERE is_active = 1')

db = Database()

ADMIN_KEYBOARD = ReplyKeyboardMarkup([
    [KeyboardButton("📋 Новые заявки"), KeyboardButton("🔄 В работе")],
    [KeyboardButton("💰 Пополнения"), KeyboardButton("💬 Чат")],
    [KeyboardButton("📊 Статистика"), KeyboardButton("👥 Пользователи")],
    [KeyboardButton("📢 Рассылка"), KeyboardButton("⚙️ Настройки")],
    [KeyboardButton("🌍 Страны"), KeyboardButton("🔗 Зеркала")]
], resize_keyboard=True)

CANCEL_KEYBOARD = ReplyKeyboardMarkup([[KeyboardButton("🔙 Отмена")]], resize_keyboard=True)

USER_KEYBOARD = ReplyKeyboardMarkup([
    [KeyboardButton("🛒 Открыть магазин", web_app=WebAppInfo(url=f"{APP_URL}/app"))],
    [KeyboardButton("📋 Мои заказы"), KeyboardButton("💳 Баланс")],
    [KeyboardButton("💰 Пополнить"), KeyboardButton("ℹ️ Информация")]
], resize_keyboard=True)

async def start_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user = update.effective_user
    await db.create_user(user.id, user.username, user.first_name, user.last_name)
    
    if user.id in ADMIN_IDS:
        await update.message.reply_text(
            f"🔧 *Админ-панель*\n\nВыберите действие:",
            reply_markup=ADMIN_KEYBOARD, parse_mode=ParseMode.MARKDOWN
        )
    else:
        await update.message.reply_text(
            "👋 Добро пожаловать в магазин прокси!\n\n🛒 Нажмите кнопку ниже для заказа:",
            reply_markup=USER_KEYBOARD
        )

async def handle_all_messages(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not update.message or not update.message.text: return
    user = update.effective_user
    text = update.message.text.strip()
    
    await db.create_user(user.id, user.username, user.first_name, user.last_name)
    user_data = await db.get_user(user.id)
    
    if user_data and user_data['is_banned']:
        await update.message.reply_text("❌ Вы заблокированы."); return
    
    # Проверяем чат для админа
    if user.id in ADMIN_IDS and user.id in active_chats:
        await handle_chat_message(update, context); return
    
    # Проверяем состояния
    if user.id in user_states:
        await handle_state_message(update, context); return
    
    # Обрабатываем меню
    if user.id in ADMIN_IDS:
        await handle_admin_menu(update, context, text)
    else:
        await handle_user_menu(update, context, text)

async def handle_chat_message(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user = update.effective_user
    text = update.message.text.strip()
    
    if text == "🔙 Выйти из чата":
        del active_chats[user.id]
        await update.message.reply_text("💬 Чат закрыт", reply_markup=ADMIN_KEYBOARD); return
    
    order_id = active_chats[user.id]
    order = await db.get_order(order_id)
    if not order:
        del active_chats[user.id]
        await update.message.reply_text("Заказ не найден", reply_markup=ADMIN_KEYBOARD); return
    
    await db.add_chat_message(order_id, order['user_id'], user.id, text, from_admin=1)
    try:
        await context.bot.send_message(order['user_id'], f"💬 *Сообщение от поддержки*\n\n{text}", parse_mode=ParseMode.MARKDOWN)
        await update.message.reply_text("✅ Отправлено")
    except Exception as e:
        await update.message.reply_text(f"❌ Ошибка: {e}")

async def handle_state_message(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user = update.effective_user
    text = update.message.text.strip()
    state = user_states.get(user.id)
    if not state: return
    
    if text == "🔙 Отмена":
        del user_states[user.id]
        kb = ADMIN_KEYBOARD if user.id in ADMIN_IDS else USER_KEYBOARD
        await update.message.reply_text("❌ Отменено", reply_markup=kb); return
    
    action = state.get('action')
    
    if action == 'awaiting_topup':
        try:
            amount = float(text)
            if amount < 1 or amount > 1000:
                await update.message.reply_text("❌ От $1 до $1000:"); return
            topup_id = await db.create_topup(user.id, amount)
            del user_states[user.id]
            await update.message.reply_text(f"✅ Заявка #{topup_id} на ${amount:.2f} создана!\n⏳ Ожидайте подтверждения.", reply_markup=USER_KEYBOARD)
            for aid in ADMIN_IDS:
                try: await context.bot.send_message(aid, f"💰 Пополнение #{topup_id}\n👤 {user.first_name}\n💵 ${amount:.2f}")
                except: pass
        except ValueError: await update.message.reply_text("❌ Введите число:")
    
    elif action == 'awaiting_broadcast':
        users = await db.get_all_users_for_broadcast()
        success = 0
        for uid in users:
            try: await context.bot.send_message(uid, text); success += 1
            except: pass
            await asyncio.sleep(0.05)
        del user_states[user.id]
        await update.message.reply_text(f"📢 Отправлено: {success}/{len(users)}", reply_markup=ADMIN_KEYBOARD)
    
    elif action == 'awaiting_welcome':
        await db.set_setting('welcome_message', text); del user_states[user.id]
        await update.message.reply_text("✅ Приветствие обновлено!", reply_markup=ADMIN_KEYBOARD)
    
    elif action == 'awaiting_price':
        try:
            price = float(text)
            await db.set_setting(f'base_price_{state["type"]}', str(price)); del user_states[user.id]
            await update.message.reply_text(f"✅ Цена: ${price:.2f}", reply_markup=ADMIN_KEYBOARD)
        except ValueError: await update.message.reply_text("❌ Введите число:")
    
    elif action == 'awaiting_new_country':
        await db.add_country(text); del user_states[user.id]
        await update.message.reply_text(f"✅ {text} добавлена!", reply_markup=ADMIN_KEYBOARD)
    
    elif action == 'awaiting_remove_country':
        await db.remove_country(text); del user_states[user.id]
        await update.message.reply_text(f"✅ {text} удалена!", reply_markup=ADMIN_KEYBOARD)
    
    elif action == 'awaiting_proxy_data':
        order_id = state.get('order_id')
        await db.update_order_status(order_id, 'completed', user.id, text)
        order = await db.get_order(order_id); del user_states[user.id]
        if order:
            try: await context.bot.send_message(order['user_id'], f"✅ *Заказ #{order_id} выполнен!*\n\n📦 Данные:\n`{text}`", parse_mode=ParseMode.MARKDOWN)
            except: pass
        await update.message.reply_text(f"✅ Заказ #{order_id} завершен!", reply_markup=ADMIN_KEYBOARD)
    
    elif action == 'awaiting_chat_order':
        try:
            oid = int(text)
            order = await db.get_order(oid)
            if not order: await update.message.reply_text("❌ Заказ не найден"); return
            active_chats[user.id] = oid
            messages = await db.get_chat_messages(oid)
            history = f"💬 *Чат #{oid}*\n\n"
            for m in messages[-20:]:
                sender = "Вы" if m['from_admin'] else "Клиент"
                history += f"*{sender}*: {m['message']}\n"
            history += "\n_Введите сообщение_"
            del user_states[user.id]
            await update.message.reply_text(history, parse_mode=ParseMode.MARKDOWN, reply_markup=InlineKeyboardMarkup([[InlineKeyboardButton("🔙 Выйти", callback_data="close_chat")]]))
        except ValueError: await update.message.reply_text("❌ Введите ID (число):")
    
    elif action == 'awaiting_mirror_name':
        state['mirror_name'] = text; state['action'] = 'awaiting_mirror_token'
        await update.message.reply_text("🔑 Введите токен бота-зеркала:")
    
    elif action == 'awaiting_mirror_token':
        state['mirror_token'] = text; state['action'] = 'awaiting_mirror_url'
        await update.message.reply_text("🔗 Введите URL зеркала:")
    
    elif action == 'awaiting_mirror_url':
        await db.add_mirror(state['mirror_name'], state['mirror_token'], text); del user_states[user.id]
        await update.message.reply_text(f"✅ Зеркало *{state['mirror_name']}* добавлено!", reply_markup=ADMIN_KEYBOARD, parse_mode=ParseMode.MARKDOWN)

async def handle_admin_menu(update: Update, context, text):
    user = update.effective_user
    
    # Проверяем разные варианты текста
    if "пополнения" in text.lower() or "пополнение" in text.lower():
        topups = await db.get_pending_topups()
        if not topups:
            await update.message.reply_text("✅ Нет ожидающих пополнений\n\nСоздайте заявку через кнопку 💰 Пополнить")
            return
        
        for t in topups:
            ui = await db.get_user(t['user_id'])
            bal = ui['balance'] if ui else 0
            msg = f"💰 *Пополнение #{t['id']}*\n\n👤 {t['first_name']} (@{t['username']})\n💵 Сумма: ${t['amount']:.2f}\n💳 Баланс: ${bal:.2f}\n📅 {t['created_at'][:19]}"
            kb = [[InlineKeyboardButton("✅ Подтвердить", callback_data=f"approve_topup_{t['id']}"), InlineKeyboardButton("❌ Отклонить", callback_data=f"reject_topup_{t['id']}")]]
            await update.message.reply_text(msg, reply_markup=InlineKeyboardMarkup(kb), parse_mode=ParseMode.MARKDOWN)
        return
    
    if "новые заявки" in text.lower() or "заявк" in text.lower():
        orders = await db.get_pending_orders()
        if not orders:
            await update.message.reply_text("✅ Нет новых заявок"); return
        for o in orders:
            msg = f"📋 *Заявка #{o['id']}*\n\n👤 {o['first_name']} (@{o['username']})\n🆔 `{o['user_id']}`\n📦 {o['proxy_type'].upper()} × {o['quantity']}\n🌍 {o['country']} | ⏱ {o['duration']}\n💰 ${o['price']:.2f}\n📅 {o['created_at'][:19]}"
            kb = [[InlineKeyboardButton("✅ Взять", callback_data=f"take_{o['id']}"), InlineKeyboardButton("❌ Отклонить", callback_data=f"reject_{o['id']}")]]
            await update.message.reply_text(msg, reply_markup=InlineKeyboardMarkup(kb), parse_mode=ParseMode.MARKDOWN)
        return
    
    if "в работе" in text.lower():
        orders = await db.get_in_progress_orders()
        if not orders:
            await update.message.reply_text("✅ Нет заявок в работе"); return
        for o in orders:
            msg = f"🔄 *Заявка #{o['id']}*\n\n👤 {o['first_name']}\n📦 {o['proxy_type'].upper()} × {o['quantity']}\n🌍 {o['country']} | ⏱ {o['duration']}\n💰 ${o['price']:.2f}"
            kb = [[InlineKeyboardButton("✅ Выполнено", callback_data=f"complete_{o['id']}"), InlineKeyboardButton("💬 Чат", callback_data=f"chat_{o['id']}")]]
            await update.message.reply_text(msg, reply_markup=InlineKeyboardMarkup(kb), parse_mode=ParseMode.MARKDOWN)
        return
    
    if "чат" in text.lower():
        user_states[user.id] = {'action': 'awaiting_chat_order'}
        await update.message.reply_text("Введите ID заказа для чата:", reply_markup=CANCEL_KEYBOARD); return
    
    if "статистик" in text.lower():
        s = await db.get_stats()
        msg = f"📊 *Статистика*\n\n👥 Пользователей: {s['total_users']}\n📦 Заказов: {s['total_orders']}\n✅ Выполнено: {s['completed_orders']}\n🔄 В работе: {s['in_progress_orders']}\n⏳ Ожидают: {s['pending_orders']}\n💰 Доход: ${s['total_revenue']:.2f}"
        await update.message.reply_text(msg, parse_mode=ParseMode.MARKDOWN); return
    
    if "пользовател" in text.lower():
        users = await db.get_all_users()
        msg = "👥 *Пользователи:*\n\n"
        for u in users[:30]:
            ban = "🚫" if u['is_banned'] else "✅"
            uname = f" (@{u['username']})" if u['username'] else ""
            msg += f"{ban} {u['first_name'] or '—'}{uname} | 💳 ${u['balance']:.2f} | ID: `{u['user_id']}`\n"
        await update.message.reply_text(msg, parse_mode=ParseMode.MARKDOWN); return
    
    if "рассылк" in text.lower():
        user_states[user.id] = {'action': 'awaiting_broadcast'}
        await update.message.reply_text("📢 Введите текст рассылки:", reply_markup=CANCEL_KEYBOARD); return
    
    if "настройк" in text.lower():
        enabled = await db.get_setting('orders_enabled')
        status = "✅" if enabled == 'true' else "❌"
        prices = {
            'http': await db.get_setting('base_price_http','2.0'),
            'socks5': await db.get_setting('base_price_socks5','2.5'),
            'residential': await db.get_setting('base_price_residential','5.0'),
            'datacenter': await db.get_setting('base_price_datacenter','1.5')
        }
        msg = f"⚙️ *Настройки*\n\nЗаказы: {status}\n\n*Цены за день:*\n• HTTP: ${prices['http']}\n• SOCKS5: ${prices['socks5']}\n• Residential: ${prices['residential']}\n• Datacenter: ${prices['datacenter']}"
        kb = [
            [InlineKeyboardButton("🔄 Заказы", callback_data="toggle_orders")],
            [InlineKeyboardButton("✏️ Приветствие", callback_data="edit_welcome")],
            [InlineKeyboardButton("💵 HTTP", callback_data="setprice_http"), InlineKeyboardButton("💵 SOCKS5", callback_data="setprice_socks5")],
            [InlineKeyboardButton("💵 Residential", callback_data="setprice_residential"), InlineKeyboardButton("💵 Datacenter", callback_data="setprice_datacenter")]
        ]
        await update.message.reply_text(msg, reply_markup=InlineKeyboardMarkup(kb), parse_mode=ParseMode.MARKDOWN); return
    
    if "стран" in text.lower():
        countries = await db.get_countries()
        msg = "🌍 *Страны:*\n\n" + "\n".join(f"• {c}" for c in countries)
        kb = [[InlineKeyboardButton("➕ Добавить", callback_data="add_country"), InlineKeyboardButton("➖ Удалить", callback_data="remove_country")]]
        await update.message.reply_text(msg, reply_markup=InlineKeyboardMarkup(kb), parse_mode=ParseMode.MARKDOWN); return
    
    if "зеркал" in text.lower():
        mirrors = await db.get_mirrors()
        if not mirrors: msg = "🔗 *Зеркала*\n\nПока нет зеркал."
        else:
            msg = "🔗 *Зеркала:*\n\n"
            for m in mirrors:
                status = "✅" if m['is_active'] else "❌"
                msg += f"{status} *{m['name']}*\n   URL: `{m['url']}`\n   ID: {m['id']}\n\n"
        kb = [
            [InlineKeyboardButton("➕ Добавить", callback_data="add_mirror")],
            [InlineKeyboardButton("🔄 Переключить", callback_data="toggle_mirror_menu")],
            [InlineKeyboardButton("❌ Удалить", callback_data="delete_mirror_menu")]
        ]
        await update.message.reply_text(msg, reply_markup=InlineKeyboardMarkup(kb), parse_mode=ParseMode.MARKDOWN); return

async def handle_user_menu(update: Update, context, text):
    user = update.effective_user
    
    if "заказы" in text.lower():
        orders = await db.get_user_orders(user.id)
        if not orders: await update.message.reply_text("📭 Нет заказов"); return
        for o in orders:
            emoji = {'pending':'⏳','in_progress':'🔄','completed':'✅','cancelled':'❌','rejected':'🚫'}.get(o['status'],'❓')
            msg = f"{emoji} *Заказ #{o['id']}*\n📦 {o['proxy_type'].upper()} × {o['quantity']}\n🌍 {o['country']} | ⏱ {o['duration']}\n💰 ${o['price']:.2f}"
            if o['status'] == 'completed' and o['proxy_data']: msg += f"\n📋 `{o['proxy_data']}`"
            kb = InlineKeyboardMarkup([[InlineKeyboardButton("❌ Отменить", callback_data=f"cancel_{o['id']}")]]) if o['status'] == 'pending' else None
            await update.message.reply_text(msg, reply_markup=kb, parse_mode=ParseMode.MARKDOWN)
    
    elif "баланс" in text.lower():
        ud = await db.get_user(user.id)
        bal = ud['balance'] if ud else 0
        await update.message.reply_text(f"💳 *Баланс: ${bal:.2f}*", parse_mode=ParseMode.MARKDOWN)
    
    elif "пополнить" in text.lower():
        user_states[user.id] = {'action': 'awaiting_topup'}
        await update.message.reply_text("💰 Введите сумму USD (мин. $1):", reply_markup=CANCEL_KEYBOARD)
    
    elif "информаци" in text.lower() or "инфо" in text.lower():
        await update.message.reply_text("ℹ️ Прокси: HTTP, SOCKS5, Residential, Datacenter\nСроки: 1 день - 3 месяца\n\nПоддержка: @admin")

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
        await query.edit_message_text(f"✅ *Взято в работу*", parse_mode=ParseMode.MARKDOWN)
        if order: await context.bot.send_message(order['user_id'], f"✅ Заказ #{oid} в работе!")
    
    elif data.startswith("reject_"):
        oid = int(data.split("_")[1])
        order = await db.get_order(oid)
        if order:
            await db.update_order_status(oid, 'rejected', user.id)
            await db.execute('UPDATE users SET balance = balance + ? WHERE user_id = ?', [order['price'], order['user_id']], fetch=False)
            await query.edit_message_text("❌ *Отклонено*", parse_mode=ParseMode.MARKDOWN)
            await context.bot.send_message(order['user_id'], f"❌ Заказ #{oid} отклонен. 💰 ${order['price']:.2f} возвращены.")
    
    elif data.startswith("complete_"):
        oid = int(data.split("_")[1])
        user_states[user.id] = {'action': 'awaiting_proxy_data', 'order_id': oid}
        await query.edit_message_text("📝 *Введите данные прокси:*", parse_mode=ParseMode.MARKDOWN)
    
    elif data.startswith("chat_"):
        oid = int(data.split("_")[1])
        active_chats[user.id] = oid
        order = await db.get_order(oid)
        messages = await db.get_chat_messages(oid)
        history = f"💬 *Чат #{oid}*\n\n"
        for m in messages[-20:]:
            sender = "Вы" if m['from_admin'] else "Клиент"
            history += f"*{sender}*: {m['message']}\n"
        history += "\n_Введите сообщение_"
        await query.edit_message_text(history, parse_mode=ParseMode.MARKDOWN, reply_markup=InlineKeyboardMarkup([[InlineKeyboardButton("🔙 Выйти", callback_data="close_chat")]]))
    
    elif data == "close_chat":
        if user.id in active_chats: del active_chats[user.id]
        await query.edit_message_text("💬 Чат закрыт")
    
    elif data.startswith("cancel_"):
        oid = int(data.split("_")[1])
        success, result = await db.cancel_order(oid, user.id)
        if success: await query.edit_message_text(f"❌ *Отменено*\n💰 +${result:.2f}", parse_mode=ParseMode.MARKDOWN)
        else: await query.answer(result, show_alert=True)
    
    elif data.startswith("approve_topup_"):
        tid = int(data.split("_")[2])
        topup = await db.approve_topup(tid, user.id)
        if topup:
            await query.edit_message_text("✅ *Подтверждено*", parse_mode=ParseMode.MARKDOWN)
            await context.bot.send_message(topup['user_id'], f"✅ Пополнение #{tid} на ${topup['amount']:.2f} зачислено!")
    
    elif data.startswith("reject_topup_"):
        tid = int(data.split("_")[2])
        topup = await db.reject_topup(tid, user.id)
        if topup:
            await query.edit_message_text("❌ *Отклонено*", parse_mode=ParseMode.MARKDOWN)
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
    
    elif data == "add_mirror":
        user_states[user.id] = {'action': 'awaiting_mirror_name'}
        await query.edit_message_text("🔗 Название зеркала:")
    
    elif data == "toggle_mirror_menu":
        mirrors = await db.get_mirrors()
        if not mirrors: await query.edit_message_text("Нет зеркал"); return
        kb = [[InlineKeyboardButton(f"{'✅' if m['is_active'] else '❌'} {m['name']}", callback_data=f"toggle_mirror_{m['id']}")] for m in mirrors]
        await query.edit_message_text("Выберите:", reply_markup=InlineKeyboardMarkup(kb))
    
    elif data == "delete_mirror_menu":
        mirrors = await db.get_mirrors()
        if not mirrors: await query.edit_message_text("Нет зеркал"); return
        kb = [[InlineKeyboardButton(f"❌ {m['name']}", callback_data=f"delete_mirror_{m['id']}")] for m in mirrors]
        await query.edit_message_text("Выберите:", reply_markup=InlineKeyboardMarkup(kb))
    
    elif data.startswith("toggle_mirror_"):
        mid = int(data.split("_")[2])
        status = await db.toggle_mirror(mid)
        await query.edit_message_text(f"🔗 {'✅ Активен' if status else '❌ Неактивен'}")
    
    elif data.startswith("delete_mirror_"):
        mid = int(data.split("_")[2])
        await db.delete_mirror(mid)
        await query.edit_message_text("❌ Зеркало удалено")

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
    return HTMLResponse(content="""<!DOCTYPE html>
<html lang="ru">
<head>
    <meta charset="UTF-8">
    <meta name="viewport" content="width=device-width, initial-scale=1.0, user-scalable=no">
    <title>Магазин прокси</title>
    <script src="https://telegram.org/js/telegram-web-app.js"></script>
    <style>
        *{margin:0;padding:0;box-sizing:border-box}
        body{font-family:-apple-system,BlinkMacSystemFont,'Segoe UI',sans-serif;background:#000;color:#fff;min-height:100vh;overflow-x:hidden;position:relative}
        
        .stars-container{position:fixed;top:0;left:0;width:100%;height:100%;pointer-events:none;z-index:0}
        
        .star{position:absolute;color:#fff;animation:starFall linear infinite;text-shadow:0 0 3px rgba(255,255,255,0.4)}
        
        @keyframes starFall{
            0%{transform:translateY(-30px) translateX(0);opacity:0}
            10%{opacity:0.7}
            50%{opacity:0.4}
            90%{opacity:0.1}
            100%{transform:translateY(105vh) translateX(40px);opacity:0}
        }
        
        .container{max-width:480px;margin:0 auto;padding:20px 16px;position:relative;z-index:1}
        h1{text-align:center;margin-bottom:24px;font-size:22px;font-weight:700;color:#fff;letter-spacing:-0.5px}
        .card{background:#111;border:1px solid #333;padding:20px;border-radius:16px;margin-bottom:14px}
        .card h2{font-size:16px;font-weight:600;margin-bottom:14px;color:#fff}
        .btn{display:block;width:100%;padding:14px;margin:6px 0;background:#fff;color:#000;border:none;border-radius:12px;font-size:15px;font-weight:600;cursor:pointer;transition:all 0.2s}
        .btn:active{background:#ccc;transform:scale(0.98)}
        .balance{font-size:36px;font-weight:700;text-align:center;color:#fff;margin:16px 0}
        .price-display{text-align:center;margin:16px 0;font-size:18px;font-weight:600}
        .price-display b{font-size:22px}
        .order-card{background:#111;border:1px solid #333;padding:16px;border-radius:12px;margin-bottom:10px}
        .status{display:inline-block;padding:4px 10px;border-radius:20px;font-size:11px;font-weight:600}
        .status-pending{background:#fff;color:#000}
        .status-in_progress{background:#333;color:#fff}
        .status-completed{background:#fff;color:#000}
        .status-cancelled,.status-rejected{background:transparent;border:1px solid #555;color:#999}
        input,select{width:100%;padding:12px 14px;margin:6px 0;background:#0a0a0a;border:1px solid #333;border-radius:10px;color:#fff;font-size:15px;font-family:inherit;-webkit-appearance:none;appearance:none}
        input:focus,select:focus{outline:none;border-color:#666}
        select{background-image:url("data:image/svg+xml,%3Csvg xmlns='http://www.w3.org/2000/svg' width='12' height='12' viewBox='0 0 12 12'%3E%3Cpath d='M6 8L1 3h10z' fill='%23999'/%3E%3C/svg%3E");background-repeat:no-repeat;background-position:right 14px center;padding-right:36px}
        .hidden{display:none!important}
        .tabs{display:flex;gap:2px;margin-bottom:20px;background:#111;border-radius:12px;padding:3px;border:1px solid #333}
        .tab{flex:1;padding:11px;text-align:center;border-radius:10px;cursor:pointer;font-weight:600;font-size:14px;color:#999}
        .tab.active{background:#fff;color:#000}
        .order-header{display:flex;justify-content:space-between;align-items:center;margin-bottom:8px}
        .order-id{font-weight:700;font-size:15px;color:#fff}
        .order-details{font-size:13px;color:#999;line-height:1.6}
        .order-details span{color:#fff;font-weight:500}
        .proxy-data{margin-top:10px;padding:10px;background:#0a0a0a;border:1px solid #333;border-radius:8px;font-family:'Courier New',monospace;font-size:12px;word-break:break-all;color:#fff}
        .empty-state{text-align:center;color:#666;padding:40px 20px;font-size:14px}
        .empty-state-icon{font-size:40px;margin-bottom:10px}
    </style>
</head>
<body>
    <div class="stars-container" id="stars"></div>
    
    <div class="container">
        <h1>✦ Магазин прокси</h1>
        
        <div class="tabs">
            <div class="tab active" onclick="showTab('shop')">Заказать</div>
            <div class="tab" onclick="showTab('orders')">Мои заказы</div>
        </div>
        
        <div id="shopTab">
            <div class="card">
                <h2>Новый заказ</h2>
                <select id="proxyType">
                    <option value="http">HTTP прокси</option>
                    <option value="socks5">SOCKS5 прокси</option>
                    <option value="residential">Residential</option>
                    <option value="datacenter">Datacenter</option>
                </select>
                <input type="number" id="quantity" placeholder="Количество (1-100)" min="1" max="100" value="1">
                <select id="country"></select>
                <select id="duration">
                    <option value="1 день">Срок: 1 день</option>
                    <option value="1 неделя">Срок: 1 неделя</option>
                    <option value="1 месяц">Срок: 1 месяц</option>
                    <option value="3 месяца">Срок: 3 месяца</option>
                </select>
                <div class="price-display">Стоимость: <b id="price">$0.00</b></div>
                <div class="balance" id="balance">$0.00</div>
                <button class="btn" onclick="createOrder()">Оформить заказ</button>
            </div>
        </div>
        
        <div id="ordersTab" class="hidden">
            <div id="ordersList"></div>
        </div>
    </div>
    
    <script>
        const tg = window.Telegram.WebApp;
        tg.ready();
        tg.expand();
        
        let userId = null, currentBalance = 0, countries = [], prices = {};
        
        if (tg.initDataUnsafe && tg.initDataUnsafe.user) {
            userId = tg.initDataUnsafe.user.id;
        }
        
        // Создаём падающие звёзды
        function createStars() {
            const container = document.getElementById('stars');
            const symbols = ['✦', '✧', '⋆', '·', '•', '◦', '⋄', '∗'];
            
            for (let i = 0; i < 35; i++) {
                const star = document.createElement('span');
                star.className = 'star';
                star.textContent = symbols[Math.floor(Math.random() * symbols.length)];
                star.style.left = Math.random() * 100 + '%';
                star.style.fontSize = (Math.random() * 12 + 6) + 'px';
                star.style.animationDuration = (Math.random() * 10 + 5) + 's';
                star.style.animationDelay = (Math.random() * 10) + 's';
                container.appendChild(star);
            }
        }
        
        createStars();
        
        async function api(url, method = 'GET', body = null) {
            const options = { method, headers: {} };
            if (body) {
                options.headers['Content-Type'] = 'application/json';
                options.body = JSON.stringify(body);
            }
            const res = await fetch(url, options);
            return res.json();
        }
        
        async function loadData() {
            const [c, p] = await Promise.all([
                api('/api/countries'),
                api('/api/prices')
            ]);
            
            countries = c.countries || [];
            prices = p.prices || {};
            
            document.getElementById('country').innerHTML = countries.map(c => `<option value="${c}">${c}</option>`).join('');
            updatePrice();
            
            if (userId) {
                const u = await api('/api/user/' + userId);
                if (u.balance !== undefined) {
                    currentBalance = u.balance;
                    document.getElementById('balance').textContent = '$' + currentBalance.toFixed(2);
                }
            }
        }
        
        function updatePrice() {
            const type = document.getElementById('proxyType').value;
            const qty = parseInt(document.getElementById('quantity').value) || 1;
            const duration = document.getElementById('duration').value;
            const multipliers = {'1 день': 1, '1 неделя': 6, '1 месяц': 20, '3 месяца': 50};
            const total = (prices[type] || 2) * qty * (multipliers[duration] || 1);
            document.getElementById('price').textContent = '$' + total.toFixed(2);
        }
        
        document.getElementById('proxyType').onchange = updatePrice;
        document.getElementById('quantity').oninput = updatePrice;
        document.getElementById('duration').onchange = updatePrice;
        
        async function createOrder() {
            if (!userId) {
                tg.showPopup({ title: 'Ошибка', message: 'Откройте через Telegram бота' });
                return;
            }
            
            const type = document.getElementById('proxyType').value;
            const quantity = parseInt(document.getElementById('quantity').value);
            const country = document.getElementById('country').value;
            const duration = document.getElementById('duration').value;
            
            if (quantity < 1 || quantity > 100) {
                tg.showPopup({ title: 'Ошибка', message: 'Количество: 1-100' });
                return;
            }
            
            const multipliers = {'1 день': 1, '1 неделя': 6, '1 месяц': 20, '3 месяца': 50};
            const total = (prices[type] || 2) * quantity * (multipliers[duration] || 1);
            
            if (currentBalance < total) {
                tg.showPopup({
                    title: 'Недостаточно средств',
                    message: 'Баланс: $' + currentBalance.toFixed(2) + '\nНужно: $' + total.toFixed(2)
                });
                return;
            }
            
            const result = await api('/api/create_order', 'POST', {
                user_id: userId,
                proxy_type: type,
                quantity: quantity,
                country: country,
                duration: duration,
                price: total
            });
            
            if (result.success) {
                currentBalance = result.new_balance;
                document.getElementById('balance').textContent = '$' + currentBalance.toFixed(2);
                tg.showPopup({
                    title: '✅ Успешно!',
                    message: 'Заказ #' + result.order_id + ' создан!\nОжидайте обработки.'
                });
                tg.HapticFeedback.notificationOccurred('success');
            } else {
                tg.showPopup({ title: 'Ошибка', message: result.error || 'Ошибка' });
                tg.HapticFeedback.notificationOccurred('error');
            }
        }
        
        function showTab(tab) {
            document.querySelectorAll('.tab').forEach(t => t.classList.remove('active'));
            event.target.classList.add('active');
            document.getElementById('shopTab').classList.add('hidden');
            document.getElementById('ordersTab').classList.add('hidden');
            
            if (tab === 'shop') {
                document.getElementById('shopTab').classList.remove('hidden');
            } else {
                document.getElementById('ordersTab').classList.remove('hidden');
                loadOrders();
            }
        }
        
        async function loadOrders() {
            if (!userId) return;
            const data = await api('/api/user/' + userId + '/orders');
            const orders = data.orders || [];
            
            if (!orders.length) {
                document.getElementById('ordersList').innerHTML = '<div class="card"><div class="empty-state"><div class="empty-state-icon">📭</div>У вас пока нет заказов</div></div>';
                return;
            }
            
            const emoji = {'pending':'⏳','in_progress':'⟳','completed':'✓','cancelled':'✕','rejected':'✕'};
            const statusText = {'pending':'Ожидает','in_progress':'В работе','completed':'Выполнен','cancelled':'Отменён','rejected':'Отклонён'};
            
            document.getElementById('ordersList').innerHTML = orders.map(o => `
                <div class="order-card">
                    <div class="order-header">
                        <span class="order-id">#${o.id}</span>
                        <span class="status status-${o.status}">${emoji[o.status]||''} ${statusText[o.status]||o.status}</span>
                    </div>
                    <div class="order-details">
                        <span>${o.proxy_type.toUpperCase()}</span> × ${o.quantity}<br>
                        ${o.country} · ${o.duration}<br>
                        Сумма: <span>$${o.price.toFixed(2)}</span>
                    </div>
                    ${o.status === 'completed' && o.proxy_data ? `<div class="proxy-data">${o.proxy_data}</div>` : ''}
                </div>
            `).join('');
        }
        
        loadData();
    </script>
</body>
</html>""")

@app.get("/api/countries")
async def api_countries(): return {"countries": await db.get_countries()}

@app.get("/api/prices")
async def api_prices(): return {"prices": {'http': float(await db.get_setting('base_price_http','2.0')), 'socks5': float(await db.get_setting('base_price_socks5','2.5')), 'residential': float(await db.get_setting('base_price_residential','5.0')), 'datacenter': float(await db.get_setting('base_price_datacenter','1.5'))}}

@app.get("/api/user/{user_id}")
async def api_user(user_id: int): u = await db.get_user(user_id); return {"balance": u['balance'] if u else 0}

@app.get("/api/user/{user_id}/orders")
async def api_user_orders(user_id: int): return {"orders": await db.get_user_orders(user_id)}

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
        await telegram_app.initialize(); await telegram_app.start()
        logger.info("Бот запущен!")
        await telegram_app.updater.start_polling(drop_pending_updates=True)
        while True: await asyncio.sleep(3600)
    
    telegram_app = None
    asyncio.run(run_bot())
