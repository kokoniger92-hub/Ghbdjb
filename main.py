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
        # ВАЖНО: Начисляем баланс пользователю
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

# Клавиатура с кнопкой Mini App для пользователей
USER_KEYBOARD = ReplyKeyboardMarkup([
    [KeyboardButton("🛒 Заказать прокси", web_app=WebAppInfo(url=f"{APP_URL}/app"))],
    [KeyboardButton("📋 Мои заказы"), KeyboardButton("💳 Баланс")],
    [KeyboardButton("💰 Пополнить"), KeyboardButton("ℹ️ Информация")]
], resize_keyboard=True)

ADMIN_KEYBOARD = ReplyKeyboardMarkup([
    [KeyboardButton("🛒 Заказать прокси", web_app=WebAppInfo(url=f"{APP_URL}/app"))],
    [KeyboardButton("📋 Заявки"), KeyboardButton("💰 Пополнения")],
    [KeyboardButton("📊 Статистика"), KeyboardButton("👥 Пользователи")],
    [KeyboardButton("⚙️ Настройки"), KeyboardButton("🌍 Страны")]
], resize_keyboard=True)

CANCEL_KEYBOARD = ReplyKeyboardMarkup([[KeyboardButton("🔙 Отмена")]], resize_keyboard=True)

async def start_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user = update.effective_user
    await db.create_user(user.id, user.username, user.first_name, user.last_name)
    
    user_data = await db.get_user(user.id)
    if user_data and user_data['is_banned']:
        await update.message.reply_text("❌ Ваш аккаунт заблокирован.")
        return
    
    welcome = await db.get_setting('welcome_message', 'Добро пожаловать!')
    
    keyboard = ADMIN_KEYBOARD if user.id in ADMIN_IDS else USER_KEYBOARD
    
    await update.message.reply_text(
        f"👋 {welcome}\n\nВыберите действие или нажмите кнопку для Mini App:",
        reply_markup=keyboard
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
        keyboard = ADMIN_KEYBOARD if user.id in ADMIN_IDS else USER_KEYBOARD
        await update.message.reply_text("❌ Отменено", reply_markup=keyboard)
        return
    
    if state.get('action') == 'awaiting_topup':
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
            
            keyboard = ADMIN_KEYBOARD if user.id in ADMIN_IDS else USER_KEYBOARD
            
            await update.message.reply_text(
                f"✅ Заявка #{topup_id} создана!\n\n"
                f"💵 Сумма: ${amount:.2f}\n"
                f"⏳ Ожидайте подтверждения.",
                reply_markup=keyboard
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
            user_info = await db.get_user(t['user_id'])
            current_balance = user_info['balance'] if user_info else 0
            
            msg = (
                f"💰 *Пополнение #{t['id']}*\n\n"
                f"👤 {t['first_name']} (@{t['username']})\n"
                f"💵 Сумма: ${t['amount']:.2f}\n"
                f"💳 Текущий баланс: ${current_balance:.2f}\n"
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
        
        msg = "👥 *Пользователи:*\n\n"
        for u in users[:20]:
            ban = "🚫" if u['is_banned'] else "✅"
            username_str = f" (@{u['username']})" if u['username'] else ""
            msg += f"{ban} {u['first_name'] or 'Без имени'}{username_str} | 💳 ${u['balance']:.2f} | ID: `{u['user_id']}`\n"
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
        
        await update.message.reply_text(msg, reply_markup=InlineKeyboardMarkup(keyboard), parse_mode=ParseMode.MARKDOWN)
    
    elif text == "🌍 Страны":
        countries = await db.get_countries()
        msg = "🌍 *Доступные страны:*\n\n" + "\n".join(f"• {c}" for c in countries)
        keyboard = [[
            InlineKeyboardButton("➕ Добавить страну", callback_data="add_country"),
            InlineKeyboardButton("➖ Удалить страну", callback_data="remove_country")
        ]]
        await update.message.reply_text(msg, reply_markup=InlineKeyboardMarkup(keyboard), parse_mode=ParseMode.MARKDOWN)

async def handle_user_menu(update: Update, context, text):
    user = update.effective_user
    
    if text == "📋 Мои заказы":
        orders = await db.get_user_orders(user.id)
        if not orders:
            await update.message.reply_text("📭 У вас пока нет заказов")
            return
        
        for order in orders:
            emoji = {'pending': '⏳', 'in_progress': '🔄', 'completed': '✅', 'cancelled': '❌', 'rejected': '🚫'}.get(order['status'], '❓')
            msg = (
                f"{emoji} *Заказ #{order['id']}*\n\n"
                f"📦 {order['proxy_type'].upper()} × {order['quantity']}\n"
                f"🌍 {order['country']} | ⏱ {order['duration']}\n"
                f"💰 ${order['price']:.2f}\n📅 {order['created_at'][:10]}"
            )
            keyboard = InlineKeyboardMarkup([[
                InlineKeyboardButton("❌ Отменить заказ", callback_data=f"cancel_{order['id']}")
            ]]) if order['status'] == 'pending' else None
            await update.message.reply_text(msg, reply_markup=keyboard, parse_mode=ParseMode.MARKDOWN)
    
    elif text == "💳 Баланс":
        user_data = await db.get_user(user.id)
        balance = user_data['balance'] if user_data else 0
        spent = user_data['total_spent'] if user_data else 0
        await update.message.reply_text(
            f"💳 *Ваш баланс*\n\n💰 Доступно: ${balance:.2f}\n📊 Потрачено: ${spent:.2f}",
            parse_mode=ParseMode.MARKDOWN
        )
    
    elif text == "💰 Пополнить":
        user_states[user.id] = {'action': 'awaiting_topup'}
        await update.message.reply_text("💰 Введите сумму пополнения в USD (мин. $1):", reply_markup=CANCEL_KEYBOARD)
    
    elif text == "ℹ️ Информация":
        await update.message.reply_text(
            "ℹ️ *О сервисе*\n\nМы предоставляем качественные прокси.\n\n*Типы:* HTTP, SOCKS5, Residential, Datacenter\n*Сроки:* 1 день, 1 неделя, 1 месяц, 3 месяца",
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
    
    if data.startswith("take_"):
        order_id = int(data.split("_")[1])
        await db.update_order_status(order_id, 'in_progress', user.id)
        order = await db.get_order(order_id)
        await query.edit_message_text(f"{query.message.text}\n\n✅ *Взято в работу*", parse_mode=ParseMode.MARKDOWN)
        if order:
            await context.bot.send_message(order['user_id'], f"✅ Ваш заказ #{order_id} взят в работу!")
    
    elif data.startswith("reject_"):
        order_id = int(data.split("_")[1])
        order = await db.get_order(order_id)
        if order:
            await db.update_order_status(order_id, 'rejected', user.id)
            await db.execute('UPDATE users SET balance = balance + ? WHERE user_id = ?', [order['price'], order['user_id']], fetch=False)
            await query.edit_message_text(f"{query.message.text}\n\n❌ *Отклонено*", parse_mode=ParseMode.MARKDOWN)
            await context.bot.send_message(order['user_id'], f"❌ Заказ #{order_id} отклонен. 💰 ${order['price']:.2f} возвращены.")
    
    elif data.startswith("cancel_"):
        order_id = int(data.split("_")[1])
        success, result = await db.cancel_order(order_id, user.id)
        if success:
            await query.edit_message_text(f"{query.message.text}\n\n❌ *Отменено*\n💰 +${result:.2f}", parse_mode=ParseMode.MARKDOWN)
        else:
            await query.answer(result, show_alert=True)
    
    elif data.startswith("approve_topup_"):
        topup_id = int(data.split("_")[2])
        topup = await db.approve_topup(topup_id, user.id)
        if topup:
            await query.edit_message_text(f"{query.message.text}\n\n✅ *Подтверждено*", parse_mode=ParseMode.MARKDOWN)
            await context.bot.send_message(topup['user_id'], f"✅ Пополнение #{topup_id} на ${topup['amount']:.2f} зачислено!\n💳 Проверьте баланс: /start")
    
    elif data.startswith("reject_topup_"):
        topup_id = int(data.split("_")[2])
        topup = await db.reject_topup(topup_id, user.id)
        if topup:
            await query.edit_message_text(f"{query.message.text}\n\n❌ *Отклонено*", parse_mode=ParseMode.MARKDOWN)
            await context.bot.send_message(topup['user_id'], f"❌ Пополнение #{topup_id} отклонено.")
    
    elif data == "toggle_orders":
        current = await db.get_setting('orders_enabled')
        new_value = 'false' if current == 'true' else 'true'
        await db.set_setting('orders_enabled', new_value)
        await query.edit_message_text(f"⚙️ Прием заказов: {'✅ Включены' if new_value == 'true' else '❌ Отключены'}")
    
    elif data == "edit_welcome":
        user_states[user.id] = {'action': 'awaiting_welcome'}
        await query.edit_message_text("✏️ Введите новое приветствие:")
    
    elif data.startswith("setprice_"):
        proxy_type = data.split("_")[1]
        user_states[user.id] = {'action': 'awaiting_price', 'proxy_type': proxy_type}
        await query.edit_message_text(f"💵 Введите цену для {proxy_type.upper()}:")
    
    elif data == "add_country":
        user_states[user.id] = {'action': 'awaiting_new_country'}
        await query.edit_message_text("🌍 Введите название страны:")
    
    elif data == "remove_country":
        user_states[user.id] = {'action': 'awaiting_remove_country'}
        await query.edit_message_text("🌍 Введите название страны для удаления:")

@asynccontextmanager
async def lifespan(app: FastAPI):
    await db.init()
    yield

app = FastAPI(lifespan=lifespan, title="Proxy Bot")

@app.get("/")
async def root():
    return {"status": "ok", "message": "Proxy Bot is running"}

@app.get("/health")
async def health():
    return {"status": "healthy"}

@app.get("/app", response_class=HTMLResponse)
async def user_app():
    """Mini App для обычных пользователей"""
    html = """
<!DOCTYPE html>
<html lang="ru">
<head>
    <meta charset="UTF-8">
    <meta name="viewport" content="width=device-width, initial-scale=1.0">
    <title>Магазин прокси</title>
    <script src="https://telegram.org/js/telegram-web-app.js"></script>
    <style>
        *{margin:0;padding:0;box-sizing:border-box}
        body{font-family:-apple-system,system-ui,sans-serif;background:var(--tg-theme-bg-color,#0f172a);color:var(--tg-theme-text-color,#e2e8f0);padding:16px}
        .container{max-width:500px;margin:0 auto}
        h1{text-align:center;margin-bottom:24px;font-size:24px;color:var(--tg-theme-button-color,#60a5fa)}
        .card{background:var(--tg-theme-secondary-bg-color,#1e293b);padding:20px;border-radius:16px;margin-bottom:16px}
        .card h2{font-size:18px;margin-bottom:12px}
        .btn{display:block;width:100%;padding:14px;margin:8px 0;background:var(--tg-theme-button-color,#3b82f6);color:var(--tg-theme-button-text-color,white);border:none;border-radius:12px;font-size:16px;font-weight:600;cursor:pointer}
        .btn:hover{opacity:0.9}
        .btn-outline{background:transparent;border:2px solid var(--tg-theme-button-color,#3b82f6);color:var(--tg-theme-button-color,#3b82f6)}
        .balance{font-size:2em;font-weight:bold;text-align:center;color:#22c55e;margin:16px 0}
        .grid{display:grid;grid-template-columns:1fr 1fr;gap:10px}
        .order-card{background:var(--tg-theme-secondary-bg-color,#1e293b);padding:16px;border-radius:12px;margin-bottom:10px}
        .order-card .status{padding:4px 10px;border-radius:12px;font-size:12px;font-weight:600}
        .status-pending{background:#f59e0b;color:#000}
        .status-completed{background:#22c55e;color:#fff}
        .status-in_progress{background:#3b82f6;color:#fff}
        input,select{width:100%;padding:12px;margin:8px 0;background:var(--tg-theme-bg-color,#334155);border:2px solid var(--tg-theme-hint-color,#475569);border-radius:10px;color:var(--tg-theme-text-color,#e2e8f0);font-size:15px}
        .hidden{display:none}
        .tabs{display:flex;gap:10px;margin-bottom:20px}
        .tab{flex:1;padding:12px;text-align:center;background:var(--tg-theme-secondary-bg-color,#1e293b);border-radius:10px;cursor:pointer;font-weight:600}
        .tab.active{background:var(--tg-theme-button-color,#3b82f6);color:white}
    </style>
</head>
<body>
    <div class="container">
        <h1>🛒 Магазин прокси</h1>
        
        <div class="tabs">
            <div class="tab active" onclick="showTab('shop')">Заказать</div>
            <div class="tab" onclick="showTab('orders')">Мои заказы</div>
        </div>
        
        <div id="shopTab">
            <div class="card">
                <h2>Новый заказ</h2>
                <select id="proxyType">
                    <option value="http">HTTP</option>
                    <option value="socks5">SOCKS5</option>
                    <option value="residential">Residential</option>
                    <option value="datacenter">Datacenter</option>
                </select>
                <input type="number" id="quantity" placeholder="Количество (1-100)" min="1" max="100" value="1">
                <select id="country"></select>
                <select id="duration">
                    <option value="1 день">1 день</option>
                    <option value="1 неделя">1 неделя</option>
                    <option value="1 месяц">1 месяц</option>
                    <option value="3 месяца">3 месяца</option>
                </select>
                <div style="text-align:center;margin:16px 0;font-size:1.2em">
                    💰 Цена: <b id="price">$0.00</b>
                </div>
                <div class="balance" id="balance">💳 $0.00</div>
                <button class="btn" onclick="createOrder()">🛒 Заказать</button>
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
        
        let userId = null;
        let currentBalance = 0;
        let countries = [];
        let prices = {};
        
        // Получаем данные пользователя из Telegram
        if (tg.initDataUnsafe && tg.initDataUnsafe.user) {
            userId = tg.initDataUnsafe.user.id;
        }
        
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
            const [countriesData, pricesData] = await Promise.all([
                api('/api/countries'),
                api('/api/prices')
            ]);
            
            countries = countriesData.countries || [];
            prices = pricesData.prices || {};
            
            // Заполняем страны
            const select = document.getElementById('country');
            select.innerHTML = countries.map(c => `<option value="${c}">${c}</option>`).join('');
            
            updatePrice();
            
            if (userId) {
                const userData = await api(`/api/user/${userId}`);
                if (userData.balance !== undefined) {
                    currentBalance = userData.balance;
                    document.getElementById('balance').textContent = `💳 $${currentBalance.toFixed(2)}`;
                }
            }
        }
        
        function updatePrice() {
            const type = document.getElementById('proxyType').value;
            const qty = parseInt(document.getElementById('quantity').value) || 1;
            const duration = document.getElementById('duration').value;
            
            const basePrice = prices[type] || 2.0;
            const multipliers = {'1 день': 1, '1 неделя': 6, '1 месяц': 20, '3 месяца': 50};
            const total = basePrice * qty * (multipliers[duration] || 1);
            
            document.getElementById('price').textContent = `$${total.toFixed(2)}`;
        }
        
        document.getElementById('proxyType').addEventListener('change', updatePrice);
        document.getElementById('quantity').addEventListener('input', updatePrice);
        document.getElementById('duration').addEventListener('change', updatePrice);
        
        async function createOrder() {
            if (!userId) {
                tg.showPopup({ title: 'Ошибка', message: 'Откройте приложение через Telegram бота' });
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
            
            const basePrice = prices[type] || 2.0;
            const multipliers = {'1 день': 1, '1 неделя': 6, '1 месяц': 20, '3 месяца': 50};
            const total = basePrice * quantity * (multipliers[duration] || 1);
            
            if (currentBalance < total) {
                tg.showPopup({ 
                    title: 'Недостаточно средств', 
                    message: `Баланс: $${currentBalance.toFixed(2)}\nНужно: $${total.toFixed(2)}\nПополните баланс в боте`,
                    buttons: [{type: 'ok'}]
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
                document.getElementById('balance').textContent = `💳 $${currentBalance.toFixed(2)}`;
                tg.showPopup({ title: '✅ Успешно!', message: `Заказ #${result.order_id} создан!\nОжидайте обработки.` });
            } else {
                tg.showPopup({ title: 'Ошибка', message: result.error || 'Не удалось создать заказ' });
            }
        }
        
        async function showTab(tab) {
            document.querySelectorAll('.tab').forEach(t => t.classList.remove('active'));
            event.target.classList.add('active');
            
            document.getElementById('shopTab').classList.add('hidden');
            document.getElementById('ordersTab').classList.add('hidden');
            
            if (tab === 'shop') {
                document.getElementById('shopTab').classList.remove('hidden');
            } else {
                document.getElementById('ordersTab').classList.remove('hidden');
                await loadOrders();
            }
        }
        
        async function loadOrders() {
            if (!userId) return;
            const data = await api(`/api/user/${userId}/orders`);
            const orders = data.orders || [];
            
            if (orders.length === 0) {
                document.getElementById('ordersList').innerHTML = '<div class="card" style="text-align:center">📭 Нет заказов</div>';
                return;
            }
            
            const emoji = {'pending':'⏳','in_progress':'🔄','completed':'✅','cancelled':'❌','rejected':'🚫'};
            
            document.getElementById('ordersList').innerHTML = orders.map(o => `
                <div class="order-card">
                    <div style="display:flex;justify-content:space-between;align-items:center">
                        <b>#${o.id}</b>
                        <span class="status status-${o.status}">${emoji[o.status]||'❓'} ${o.status}</span>
                    </div>
                    <div style="margin-top:8px">📦 ${o.proxy_type.toUpperCase()} × ${o.quantity}</div>
                    <div>🌍 ${o.country} | ⏱ ${o.duration}</div>
                    <div>💰 $${o.price.toFixed(2)}</div>
                </div>
            `).join('');
        }
        
        loadData();
    </script>
</body>
</html>
    """
    return HTMLResponse(content=html)

@app.get("/admin", response_class=HTMLResponse)
async def admin_panel():
    """Админ-панель"""
    html = """
<!DOCTYPE html>
<html lang="ru">
<head>
    <meta charset="UTF-8"><meta name="viewport" content="width=device-width, initial-scale=1.0">
    <title>Админ-панель</title>
    <style>
        *{margin:0;padding:0;box-sizing:border-box}
        body{font-family:-apple-system,system-ui,sans-serif;background:#0f172a;color:#e2e8f0;padding:20px}
        .container{max-width:1400px;margin:0 auto}
        h1{color:#60a5fa;margin-bottom:30px}
        .login{background:#1e293b;padding:40px;border-radius:20px;max-width:400px;margin:100px auto;text-align:center}
        .login input{width:100%;padding:15px;margin:15px 0;background:#334155;border:2px solid #475569;border-radius:12px;color:#e2e8f0;font-size:16px}
        .login button{width:100%;padding:15px;background:#3b82f6;border:none;border-radius:12px;color:white;font-size:16px;font-weight:600;cursor:pointer}
        .dashboard{display:none}
        .stats{display:grid;grid-template-columns:repeat(auto-fit,minmax(250px,1fr));gap:20px;margin-bottom:40px}
        .stat-card{background:#1e293b;padding:25px;border-radius:16px}
        .stat-card h3{color:#94a3b8;font-size:14px;text-transform:uppercase;margin-bottom:10px}
        .stat-card .value{font-size:2.5em;font-weight:bold;color:#60a5fa}
        .section{background:#1e293b;padding:30px;border-radius:16px;margin-bottom:30px}
        .section h2{color:#60a5fa;margin-bottom:20px}
        table{width:100%;border-collapse:collapse}
        th,td{padding:14px 12px;text-align:left;border-bottom:1px solid #334155}
        th{color:#94a3b8;font-weight:600;font-size:13px}
        .btn{padding:8px 16px;border:none;border-radius:8px;cursor:pointer;font-size:14px;font-weight:600;margin:3px}
        .btn-success{background:#22c55e;color:white}
        .btn-danger{background:#ef4444;color:white}
        .header{display:flex;justify-content:space-between;align-items:center;margin-bottom:30px}
        .refresh-btn{background:#3b82f6;color:white;padding:12px 24px;border:none;border-radius:12px;cursor:pointer}
        .empty{text-align:center;color:#94a3b8;padding:40px}
    </style>
</head>
<body>
    <div class="container">
        <div class="login" id="loginBox">
            <h1>🔧 Proxy Bot</h1>
            <input type="password" id="password" placeholder="Пароль" onkeypress="if(event.key==='Enter')login()">
            <button onclick="login()">Войти</button>
        </div>
        <div class="dashboard" id="dashboard">
            <div class="header"><h1>📊 Панель управления</h1><button class="refresh-btn" onclick="loadAll()">🔄 Обновить</button></div>
            <div class="stats" id="stats"></div>
            <div class="section"><h2>📋 Заявки</h2><div id="orders"></div></div>
            <div class="section"><h2>💰 Пополнения</h2><div id="topups"></div></div>
        </div>
    </div>
    <script>
        let password='';
        function login(){password=document.getElementById('password').value;if(!password)return;loadAll();document.getElementById('loginBox').style.display='none';document.getElementById('dashboard').style.display='block';setInterval(loadAll,30000)}
        async function api(url){const res=await fetch(url+'?password='+password);if(res.status===403){alert('Неверный пароль');location.reload()}return res.json()}
        async function loadAll(){const[s,o,t]=await Promise.all([api('/api/stats'),api('/api/orders'),api('/api/topups')]);if(s)document.getElementById('stats').innerHTML=`<div class="stat-card"><h3>Пользователи</h3><div class="value">${s.total_users}</div></div><div class="stat-card"><h3>Заказы</h3><div class="value">${s.total_orders}</div></div><div class="stat-card"><h3>Выполнено</h3><div class="value">${s.completed_orders}</div></div><div class="stat-card"><h3>В ожидании</h3><div class="value">${s.pending_orders}</div></div><div class="stat-card"><h3>Доход</h3><div class="value">$${s.total_revenue}</div></div>`;if(o)document.getElementById('orders').innerHTML=o.length?o.map(o=>`<div style="padding:10px;margin:5px 0;background:#334155;border-radius:8px">#${o.id} | ${o.first_name} | ${o.proxy_type} x${o.quantity} | ${o.country} | $${o.price} <button class="btn btn-success" onclick="act('/api/order/${o.id}/status','in_progress')">✅</button><button class="btn btn-danger" onclick="act('/api/order/${o.id}/status','rejected')">❌</button></div>`).join(''):'<div class="empty">✅ Нет заявок</div>';if(t)document.getElementById('topups').innerHTML=t.length?t.map(t=>`<div style="padding:10px;margin:5px 0;background:#334155;border-radius:8px">#${t.id} | ${t.first_name} | $${t.amount} <button class="btn btn-success" onclick="act('/api/topup/${t.id}/approve')">✅</button><button class="btn btn-danger" onclick="act('/api/topup/${t.id}/reject')">❌</button></div>`).join(''):'<div class="empty">✅ Нет пополнений</div>'}
        async function act(url,s){const f=new FormData();if(s)f.append('status',s);f.append('password',password);await fetch(url,{method:'POST',body:f});loadAll()}
    </script>
</body>
</html>
    """
    return HTMLResponse(content=html)

@app.get("/api/countries")
async def api_countries():
    countries = await db.get_countries()
    return {"countries": countries}

@app.get("/api/prices")
async def api_prices():
    prices = {
        'http': float(await db.get_setting('base_price_http', '2.0')),
        'socks5': float(await db.get_setting('base_price_socks5', '2.5')),
        'residential': float(await db.get_setting('base_price_residential', '5.0')),
        'datacenter': float(await db.get_setting('base_price_datacenter', '1.5'))
    }
    return {"prices": prices}

@app.get("/api/user/{user_id}")
async def api_user(user_id: int):
    user = await db.get_user(user_id)
    if not user:
        return {"balance": 0, "orders_count": 0}
    return {"balance": user['balance'], "orders_count": user['orders_count']}

@app.get("/api/user/{user_id}/orders")
async def api_user_orders(user_id: int):
    orders = await db.get_user_orders(user_id)
    return {"orders": orders}

@app.post("/api/create_order")
async def api_create_order(request: Request):
    data = await request.json()
    user_id = data.get('user_id')
    proxy_type = data.get('proxy_type')
    quantity = data.get('quantity')
    country = data.get('country')
    duration = data.get('duration')
    price = data.get('price')
    
    user = await db.get_user(user_id)
    if not user:
        return {"success": False, "error": "Пользователь не найден"}
    
    if user['balance'] < price:
        return {"success": False, "error": "Недостаточно средств"}
    
    order_id = await db.create_order(user_id, proxy_type, quantity, country, duration, price)
    new_balance = user['balance'] - price
    
    for admin_id in ADMIN_IDS:
        try:
            await telegram_app.bot.send_message(admin_id, f"📋 Новый заказ #{order_id}\n👤 {user['first_name']}\n📦 {proxy_type.upper()} × {quantity}\n🌍 {country} | ⏱ {duration}\n💰 ${price:.2f}")
        except:
            pass
    
    return {"success": True, "order_id": order_id, "new_balance": new_balance}

@app.get("/api/stats")
async def api_stats(password: str = ""):
    if password != ADMIN_PASSWORD:
        raise HTTPException(403)
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
        raise HTTPException(404)
    await db.update_order_status(order_id, status)
    if status == 'rejected':
        await db.execute('UPDATE users SET balance = balance + ? WHERE user_id = ?', [order['price'], order['user_id']], fetch=False)
        try:
            await telegram_app.bot.send_message(order['user_id'], f"❌ Заказ #{order_id} отклонен. 💰 ${order['price']:.2f} возвращены.")
        except:
            pass
    elif status == 'in_progress':
        try:
            await telegram_app.bot.send_message(order['user_id'], f"✅ Заказ #{order_id} взят в работу!")
        except:
            pass
    return {"success": True}

@app.post("/api/topup/{topup_id}/approve")
async def api_approve_topup(topup_id: int, password: str = Form(...)):
    if password != ADMIN_PASSWORD:
        raise HTTPException(403)
    result = await db.approve_topup(topup_id, 0)
    if not result:
        raise HTTPException(404)
    try:
        await telegram_app.bot.send_message(result['user_id'], f"✅ Пополнение #{topup_id} на ${result['amount']:.2f} зачислено!\n💳 Проверьте баланс.")
    except:
        pass
    return {"success": True}

@app.post("/api/topup/{topup_id}/reject")
async def api_reject_topup(topup_id: int, password: str = Form(...)):
    if password != ADMIN_PASSWORD:
        raise HTTPException(403)
    result = await db.reject_topup(topup_id, 0)
    if not result:
        raise HTTPException(404)
    try:
        await telegram_app.bot.send_message(result['user_id'], f"❌ Пополнение #{topup_id} отклонено.")
    except:
        pass
    return {"success": True}

if __name__ == "__main__":
    import uvicorn
    
    def run_api():
        uvicorn.run(app, host="0.0.0.0", port=PORT, log_level="info")
    
    Thread(target=run_api, daemon=True).start()
    
    try:
        resp = requests.get(f"https://api.telegram.org/bot{BOT_TOKEN}/deleteWebhook")
        logger.info(f"Webhook deleted: {resp.json()}")
    except:
        pass
    
    async def run_bot():
        global telegram_app
        await db.init()
        
        telegram_app = Application.builder().token(BOT_TOKEN).build()
        
        telegram_app.add_handler(CommandHandler("start", start_command))
        telegram_app.add_handler(CallbackQueryHandler(handle_callbacks))
        telegram_app.add_handler(MessageHandler(filters.TEXT & ~filters.COMMAND, handle_all_messages))
        
        await telegram_app.initialize()
        await telegram_app.start()
        await telegram_app.bot.set_my_name("Proxy Bot")
        
        logger.info("Бот запущен!")
        await telegram_app.updater.start_polling(drop_pending_updates=True)
        
        while True:
            await asyncio.sleep(3600)
    
    telegram_app = None
    asyncio.run(run_bot())
