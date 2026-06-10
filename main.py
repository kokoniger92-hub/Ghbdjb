import os, sys, asyncio, logging, requests
from datetime import datetime
from threading import Thread
from contextlib import asynccontextmanager
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
if not BOT_TOKEN: logger.error("BOT_TOKEN not found!"); sys.exit(1)

ADMIN_IDS = list(map(int, os.getenv('ADMIN_IDS', '').split(','))) if os.getenv('ADMIN_IDS') else []
ADMIN_PASSWORD = os.getenv('ADMIN_PASSWORD', 'admin123')
PORT = int(os.getenv('PORT', 8000))
APP_URL = os.getenv('APP_URL', 'https://ghbdjb-tusy.onrender.com')

user_states = {}
active_chats = {}

class DB:
    def __init__(self): self.path = '/tmp/proxy_bot.db'
    
    async def init(self):
        async with aiosqlite.connect(self.path) as db:
            await db.execute('PRAGMA journal_mode=WAL')
            await db.execute('PRAGMA synchronous=NORMAL')
            await db.executescript('''
                CREATE TABLE IF NOT EXISTS users(user_id INTEGER PRIMARY KEY, username TEXT, first_name TEXT, last_name TEXT, is_admin INTEGER DEFAULT 0, is_banned INTEGER DEFAULT 0, balance REAL DEFAULT 0, total_spent REAL DEFAULT 0, orders_count INTEGER DEFAULT 0, created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP);
                CREATE TABLE IF NOT EXISTS orders(id INTEGER PRIMARY KEY AUTOINCREMENT, user_id INTEGER, proxy_type TEXT, quantity INTEGER, country TEXT, duration TEXT, price REAL, status TEXT DEFAULT 'pending', admin_id INTEGER, proxy_data TEXT, created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP, updated_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP);
                CREATE TABLE IF NOT EXISTS settings(key TEXT PRIMARY KEY, value TEXT);
                CREATE TABLE IF NOT EXISTS countries(name TEXT PRIMARY KEY, is_active INTEGER DEFAULT 1);
                CREATE TABLE IF NOT EXISTS topups(id INTEGER PRIMARY KEY AUTOINCREMENT, user_id INTEGER, amount REAL, status TEXT DEFAULT 'pending', admin_id INTEGER, created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP);
                INSERT OR IGNORE INTO settings VALUES('orders_enabled','true'),('base_price_http','2.0'),('base_price_socks5','2.5'),('base_price_residential','5.0'),('base_price_datacenter','1.5');
            ''')
            countries = ['США','Россия','Германия','Франция','Великобритания','Канада','Япония','Корея','Нидерланды','Польша','Испания','Италия','Швеция','Норвегия','Финляндия']
            for c in countries:
                await db.execute('INSERT OR IGNORE INTO countries(name) VALUES(?)',(c,))
            await db.commit()
    
    async def q(self, sql, params=None, fetch=True):
        async with aiosqlite.connect(self.path) as db:
            db.row_factory = aiosqlite.Row
            c = await db.execute(sql, params or [])
            if fetch: return [dict(r) for r in await c.fetchall()]
            await db.commit(); return c.lastrowid
    
    async def get_setting(self, k, d=None): r = await self.q('SELECT value FROM settings WHERE key=?',[k]); return r[0]['value'] if r else d
    async def set_setting(self, k, v): await self.q('INSERT OR REPLACE INTO settings VALUES(?,?)',[k,v],False)
    async def get_user(self, uid): r = await self.q('SELECT * FROM users WHERE user_id=?',[uid]); return r[0] if r else None
    async def create_user(self, uid, uname, fname, lname): await self.q('INSERT OR REPLACE INTO users(user_id,username,first_name,last_name,is_admin) VALUES(?,?,?,?,?)',[uid,uname or '',fname or '',lname or '',1 if uid in ADMIN_IDS else 0],False)
    async def create_order(self, uid, ptype, qty, country, dur, price):
        oid = await self.q('INSERT INTO orders(user_id,proxy_type,quantity,country,duration,price) VALUES(?,?,?,?,?,?)',[uid,ptype,qty,country,dur,price],False)
        await self.q('UPDATE users SET balance=balance-?, orders_count=orders_count+1, total_spent=total_spent+? WHERE user_id=?',[price,price,uid],False)
        return oid
    async def get_orders(self, uid): return await self.q('SELECT * FROM orders WHERE user_id=? ORDER BY created_at DESC LIMIT 50',[uid])
    async def get_pending_orders(self): return await self.q("SELECT o.*,u.username,u.first_name FROM orders o JOIN users u ON o.user_id=u.user_id WHERE o.status='pending' ORDER BY o.created_at DESC LIMIT 50")
    async def get_in_progress(self): return await self.q("SELECT o.*,u.username,u.first_name FROM orders o JOIN users u ON o.user_id=u.user_id WHERE o.status='in_progress' ORDER BY o.updated_at DESC LIMIT 50")
    async def get_order(self, oid): r = await self.q('SELECT o.*,u.username,u.first_name FROM orders o JOIN users u ON o.user_id=u.user_id WHERE o.id=?',[oid]); return r[0] if r else None
    async def update_order(self, oid, status, admin_id=None, pdata=''):
        if pdata: await self.q('UPDATE orders SET status=?,admin_id=?,proxy_data=?,updated_at=CURRENT_TIMESTAMP WHERE id=?',[status,admin_id,pdata,oid],False)
        else: await self.q('UPDATE orders SET status=?,admin_id=?,updated_at=CURRENT_TIMESTAMP WHERE id=?',[status,admin_id,oid],False)
    async def cancel_order(self, oid, uid):
        o = await self.q('SELECT * FROM orders WHERE id=? AND user_id=? AND status=?',[oid,uid,'pending'])
        if not o: return False,"Не найден"
        await self.q("UPDATE orders SET status='cancelled' WHERE id=?",[oid],False)
        await self.q('UPDATE users SET balance=balance+? WHERE user_id=?',[o[0]['price'],uid],False)
        return True,o[0]['price']
    async def get_pending_topups(self): return await self.q("SELECT t.*,u.username,u.first_name FROM topups t JOIN users u ON t.user_id=u.user_id WHERE t.status='pending' ORDER BY t.created_at DESC LIMIT 50")
    async def create_topup(self, uid, amount): return await self.q('INSERT INTO topups(user_id,amount) VALUES(?,?)',[uid,amount],False)
    async def approve_topup(self, tid, aid):
        t = await self.q('SELECT * FROM topups WHERE id=? AND status=?',[tid,'pending'])
        if not t: return None
        t = t[0]
        await self.q("UPDATE topups SET status='completed',admin_id=? WHERE id=?",[aid,tid],False)
        await self.q('UPDATE users SET balance=balance+? WHERE user_id=?',[t['amount'],t['user_id']],False)
        return t
    async def reject_topup(self, tid, aid):
        t = await self.q('SELECT * FROM topups WHERE id=? AND status=?',[tid,'pending'])
        if not t: return None
        await self.q("UPDATE topups SET status='rejected',admin_id=? WHERE id=?",[aid,tid],False)
        return t[0]
    async def get_stats(self):
        u = await self.q('SELECT COUNT(*) as c FROM users')
        o = await self.q('SELECT COUNT(*) as c FROM orders')
        c = await self.q("SELECT COUNT(*) as c FROM orders WHERE status='completed'")
        p = await self.q("SELECT COUNT(*) as c FROM orders WHERE status='pending'")
        ip = await self.q("SELECT COUNT(*) as c FROM orders WHERE status='in_progress'")
        r = await self.q("SELECT COALESCE(SUM(price),0) as t FROM orders WHERE status='completed'")
        return {'users':u[0]['c'],'orders':o[0]['c'],'completed':c[0]['c'],'pending':p[0]['c'],'in_progress':ip[0]['c'],'revenue':round(r[0]['t'],2)}
    async def get_all_users(self): return await self.q('SELECT * FROM users ORDER BY created_at DESC LIMIT 100')
    async def get_countries(self): return [r['name'] for r in await self.q('SELECT name FROM countries WHERE is_active=1 ORDER BY name')]
    async def add_country(self, n): await self.q('INSERT OR IGNORE INTO countries VALUES(?,1)',[n],False)
    async def remove_country(self, n): await self.q('UPDATE countries SET is_active=0 WHERE name=?',[n],False)
    async def get_broadcast_users(self): return [r['user_id'] for r in await self.q('SELECT user_id FROM users WHERE is_admin=0 AND is_banned=0')]

db = DB()

ADMIN_KB = ReplyKeyboardMarkup([
    [KeyboardButton("📋 Заявки"), KeyboardButton("🔄 В работе")],
    [KeyboardButton("💰 Пополнения"), KeyboardButton("📊 Статистика")],
    [KeyboardButton("📢 Рассылка"), KeyboardButton("⚙️ Настройки")],
    [KeyboardButton("👥 Пользователи")]
], resize_keyboard=True)

USER_KB = ReplyKeyboardMarkup([
    [KeyboardButton("🛒 Магазин", web_app=WebAppInfo(url=f"{APP_URL}/app"))],
    [KeyboardButton("📋 Мои заказы"), KeyboardButton("💳 Баланс")],
    [KeyboardButton("💰 Пополнить")]
], resize_keyboard=True)

CANCEL_KB = ReplyKeyboardMarkup([[KeyboardButton("🔙 Отмена")]], resize_keyboard=True)

async def start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    u = update.effective_user
    await db.create_user(u.id, u.username, u.first_name, u.last_name)
    if u.id in ADMIN_IDS:
        await update.message.reply_text("🔧 Админ-панель", reply_markup=ADMIN_KB)
    else:
        await update.message.reply_text("👋 Добро пожаловать!\nНажмите кнопку для заказа:", reply_markup=USER_KB)

async def handle_msg(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not update.message or not update.message.text: return
    u = update.effective_user; t = update.message.text.strip()
    await db.create_user(u.id, u.username, u.first_name, u.last_name)
    ud = await db.get_user(u.id)
    if ud and ud['is_banned']: await update.message.reply_text("❌ Заблокирован"); return
    
    if u.id in user_states: await state_msg(update, context); return
    if u.id in ADMIN_IDS: await admin_menu(update, context, t)
    else: await user_menu(update, context, t)

async def state_msg(update: Update, context):
    u = update.effective_user; t = update.message.text.strip(); s = user_states.get(u.id)
    if not s: return
    if t == "🔙 Отмена": del user_states[u.id]; await update.message.reply_text("❌ Отменено", reply_markup=ADMIN_KB if u.id in ADMIN_IDS else USER_KB); return
    
    a = s.get('action')
    if a == 'topup':
        try:
            amt = float(t)
            if amt < 1 or amt > 1000: await update.message.reply_text("❌ $1-$1000"); return
            tid = await db.create_topup(u.id, amt); del user_states[u.id]
            await update.message.reply_text(f"✅ Заявка #{tid} на ${amt:.2f}", reply_markup=USER_KB)
            for aid in ADMIN_IDS:
                try: await context.bot.send_message(aid, f"💰 #{tid}\n{u.first_name}\n${amt:.2f}")
                except: pass
        except: await update.message.reply_text("❌ Число:")
    elif a == 'broadcast':
        users = await db.get_broadcast_users(); ok = 0
        for uid in users:
            try: await context.bot.send_message(uid, t); ok += 1
            except: pass; await asyncio.sleep(0.03)
        del user_states[u.id]; await update.message.reply_text(f"📢 {ok}/{len(users)}", reply_markup=ADMIN_KB)
    elif a == 'welcome': await db.set_setting('welcome', t); del user_states[u.id]; await update.message.reply_text("✅", reply_markup=ADMIN_KB)
    elif a == 'price':
        try: await db.set_setting(f"base_price_{s['type']}", t); del user_states[u.id]; await update.message.reply_text("✅", reply_markup=ADMIN_KB)
        except: await update.message.reply_text("❌")
    elif a == 'new_country': await db.add_country(t); del user_states[u.id]; await update.message.reply_text(f"✅ {t}", reply_markup=ADMIN_KB)
    elif a == 'del_country': await db.remove_country(t); del user_states[u.id]; await update.message.reply_text(f"✅ {t}", reply_markup=ADMIN_KB)
    elif a == 'proxy_data':
        await db.update_order(s['oid'], 'completed', u.id, t); o = await db.get_order(s['oid']); del user_states[u.id]
        if o:
            try: await context.bot.send_message(o['user_id'], f"✅ Заказ #{s['oid']} готов!\n📦 Данные:\n{t}")
            except: pass
        await update.message.reply_text(f"✅ Заказ #{s['oid']} завершён!", reply_markup=ADMIN_KB)

async def admin_menu(update: Update, context, t):
    u = update.effective_user
    
    if "пополнен" in t.lower():
        tops = await db.get_pending_topups()
        if not tops: await update.message.reply_text("✅ Нет"); return
        for tp in tops:
            bal = (await db.get_user(tp['user_id']))['balance']
            await update.message.reply_text(f"💰 #{tp['id']}\n{tp['first_name']}\n${tp['amount']:.2f}\nБаланс: ${bal:.2f}",
                reply_markup=InlineKeyboardMarkup([[
                    InlineKeyboardButton("✅", callback_data=f"at_{tp['id']}"),
                    InlineKeyboardButton("❌", callback_data=f"rt_{tp['id']}")
                ]]))
    
    elif "заявк" in t.lower():
        orders = await db.get_pending_orders()
        if not orders: await update.message.reply_text("✅ Нет заявок"); return
        for o in orders:
            await update.message.reply_text(
                f"📋 #{o['id']}\n{o['first_name']}\n{o['proxy_type']} x{o['quantity']}\n{o['country']} | {o['duration']}\n${o['price']:.2f}",
                reply_markup=InlineKeyboardMarkup([[
                    InlineKeyboardButton("✅ Взять", callback_data=f"tk_{o['id']}"),
                    InlineKeyboardButton("❌", callback_data=f"rj_{o['id']}")
                ]]))
    
    elif "работ" in t.lower():
        orders = await db.get_in_progress()
        if not orders: await update.message.reply_text("✅ Нет"); return
        for o in orders:
            await update.message.reply_text(
                f"🔄 #{o['id']}\n{o['first_name']}\n{o['proxy_type']} x{o['quantity']}\n${o['price']:.2f}",
                reply_markup=InlineKeyboardMarkup([[
                    InlineKeyboardButton("✅ Готово", callback_data=f"cp_{o['id']}")
                ]]))
    
    elif "статистик" in t.lower():
        s = await db.get_stats()
        await update.message.reply_text(f"📊\n👥 {s['users']}\n📦 {s['orders']}\n✅ {s['completed']}\n🔄 {s['in_progress']}\n💰 ${s['revenue']}")
    
    elif "пользовател" in t.lower():
        users = await db.get_all_users()
        msg = "👥\n"
        for uu in users[:20]: msg += f"{'🚫' if uu['is_banned'] else '✅'} {uu['first_name'] or '—'} | ${uu['balance']:.2f} | {uu['user_id']}\n"
        await update.message.reply_text(msg)
    
    elif "рассылк" in t.lower(): user_states[u.id] = {'action': 'broadcast'}; await update.message.reply_text("Текст:", reply_markup=CANCEL_KB)
    
    elif "настройк" in t.lower():
        e = await db.get_setting('orders_enabled')
        p = {'http':await db.get_setting('base_price_http','2'),'socks5':await db.get_setting('base_price_socks5','2.5'),'residential':await db.get_setting('base_price_residential','5'),'datacenter':await db.get_setting('base_price_datacenter','1.5')}
        await update.message.reply_text(
            f"⚙️ Заказы: {'✅' if e=='true' else '❌'}\nHTTP:${p['http']} SOCKS5:${p['socks5']}\nRes:${p['residential']} DC:${p['datacenter']}",
            reply_markup=InlineKeyboardMarkup([
                [InlineKeyboardButton("🔄 Заказы", callback_data="tog")],
                [InlineKeyboardButton("HTTP", callback_data="sp_http"), InlineKeyboardButton("SOCKS5", callback_data="sp_socks5")],
                [InlineKeyboardButton("Residential", callback_data="sp_residential"), InlineKeyboardButton("Datacenter", callback_data="sp_datacenter")]
            ]))

async def user_menu(update: Update, context, t):
    u = update.effective_user
    if "заказ" in t.lower():
        orders = await db.get_orders(u.id)
        if not orders: await update.message.reply_text("📭 Нет заказов"); return
        for o in orders:
            em = {'pending':'⏳','in_progress':'🔄','completed':'✅','cancelled':'❌','rejected':'🚫'}.get(o['status'],'?')
            msg = f"{em} #{o['id']}\n{o['proxy_type']} x{o['quantity']}\n{o['country']} | {o['duration']}\n${o['price']:.2f}"
            if o['proxy_data']: msg += f"\n📋 {o['proxy_data']}"
            kb = InlineKeyboardMarkup([[InlineKeyboardButton("❌ Отменить", callback_data=f"co_{o['id']}")]]) if o['status']=='pending' else None
            await update.message.reply_text(msg, reply_markup=kb)
    elif "баланс" in t.lower():
        uu = await db.get_user(u.id)
        await update.message.reply_text(f"💳 ${uu['balance']:.2f}" if uu else "💳 $0")
    elif "пополн" in t.lower(): user_states[u.id] = {'action': 'topup'}; await update.message.reply_text("Сумма USD:", reply_markup=CANCEL_KB)

async def callbacks(update: Update, context: ContextTypes.DEFAULT_TYPE):
    q = update.callback_query; u = q.from_user; d = q.data; await q.answer()
    
    if d.startswith("tk_"):
        oid = int(d.split("_")[1]); await db.update_order(oid, 'in_progress', u.id)
        o = await db.get_order(oid); await q.edit_message_text(f"✅ Взято #{oid}")
        if o: await context.bot.send_message(o['user_id'], f"✅ Заказ #{oid} в работе!")
    
    elif d.startswith("rj_"):
        oid = int(d.split("_")[1]); o = await db.get_order(oid)
        if o:
            await db.update_order(oid, 'rejected', u.id)
            await db.q('UPDATE users SET balance=balance+? WHERE user_id=?',[o['price'],o['user_id']],False)
            await q.edit_message_text(f"❌ Отклонено #{oid}")
            await context.bot.send_message(o['user_id'], f"❌ #{oid} отклонён\n💰 +${o['price']:.2f}")
    
    elif d.startswith("cp_"):
        oid = int(d.split("_")[1]); user_states[u.id] = {'action': 'proxy_data', 'oid': oid}
        await q.edit_message_text("📝 Введите данные прокси:")
    
    elif d.startswith("co_"):
        oid = int(d.split("_")[1]); ok, res = await db.cancel_order(oid, u.id)
        if ok: await q.edit_message_text(f"❌ Отменено\n💰 +${res:.2f}")
        else: await q.answer(res, show_alert=True)
    
    elif d.startswith("at_"):
        tid = int(d.split("_")[1]); tp = await db.approve_topup(tid, u.id)
        if tp: await q.edit_message_text(f"✅ +${tp['amount']:.2f}"); await context.bot.send_message(tp['user_id'], f"✅ +${tp['amount']:.2f} на баланс")
    
    elif d.startswith("rt_"):
        tid = int(d.split("_")[1]); tp = await db.reject_topup(tid, u.id)
        if tp: await q.edit_message_text("❌"); await context.bot.send_message(tp['user_id'], "❌ Отклонено")
    
    elif d == "tog":
        cur = await db.get_setting('orders_enabled'); new = 'false' if cur == 'true' else 'true'
        await db.set_setting('orders_enabled', new); await q.edit_message_text(f"Заказы: {'✅' if new=='true' else '❌'}")
    
    elif d.startswith("sp_"): user_states[u.id] = {'action': 'price', 'type': d.split("_")[1]}; await q.edit_message_text("Цена:")

@asynccontextmanager
async def lifespan(app: FastAPI): await db.init(); yield

app = FastAPI(lifespan=lifespan)

@app.get("/")
async def root(): return {"status":"ok"}

@app.get("/health")
async def health(): return {"status":"healthy"}

@app.get("/app", response_class=HTMLResponse)
async def user_app():
    return HTMLResponse(content="""<!DOCTYPE html>
<html lang="ru">
<head>
    <meta charset="UTF-8"><meta name="viewport" content="width=device-width,initial-scale=1">
    <title>Магазин прокси</title>
    <script src="https://telegram.org/js/telegram-web-app.js"></script>
    <style>
        *{margin:0;padding:0;box-sizing:border-box}
        body{font-family:-apple-system,BlinkMacSystemFont,'Segoe UI',Arial,sans-serif;background:#0a1929;color:#fff;padding:12px;min-height:100vh}
        .container{max-width:500px;margin:0 auto}
        h1{text-align:center;margin:16px 0;font-size:22px;color:#60a5fa}
        .card{background:#132f4c;padding:18px;border-radius:14px;margin:12px 0;border:1px solid #1e4976}
        .card h3{color:#93c5fd;margin-bottom:12px;font-size:16px}
        .btn{display:block;width:100%;padding:13px;margin:8px 0;background:linear-gradient(135deg,#3b82f6,#2563eb);color:#fff;border:none;border-radius:10px;font-size:15px;font-weight:600;cursor:pointer}
        .btn:active{transform:scale(0.97)}
        select,input{width:100%;padding:11px;margin:6px 0;background:#0d2137;border:2px solid #1e4976;border-radius:10px;color:#e2e8f0;font-size:14px;font-family:inherit}
        select:focus,input:focus{outline:none;border-color:#3b82f6}
        select{-webkit-appearance:none;background-image:url("data:image/svg+xml,%3Csvg xmlns='http://www.w3.org/2000/svg' width='12' height='12' viewBox='0 0 12 12'%3E%3Cpath d='M6 8L1 3h10z' fill='%2360a5fa'/%3E%3C/svg%3E");background-repeat:no-repeat;background-position:right 12px center;padding-right:36px}
        .balance{font-size:34px;text-align:center;margin:14px 0;font-weight:800;color:#60a5fa}
        .price-box{text-align:center;margin:12px 0;padding:10px;background:#0d2137;border-radius:10px;font-size:17px;font-weight:600}
        .price-box b{color:#60a5fa;font-size:20px}
        .hidden{display:none!important}
        .tabs{display:flex;gap:3px;margin:18px 0;background:#132f4c;border-radius:12px;padding:3px;border:1px solid #1e4976}
        .tab{flex:1;padding:11px;text-align:center;border-radius:10px;cursor:pointer;font-weight:600;font-size:14px;color:#94a3b8}
        .tab.active{background:#3b82f6;color:#fff}
        .order-card{background:#132f4c;padding:14px;border-radius:12px;margin:10px 0;border:1px solid #1e4976}
        .order-header{display:flex;justify-content:space-between;align-items:center;margin-bottom:8px}
        .order-id{font-weight:700;font-size:15px;color:#93c5fd}
        .status{display:inline-block;padding:4px 10px;border-radius:20px;font-size:11px;font-weight:700}
        .status-pending{background:#f59e0b;color:#000}
        .status-in_progress{background:#3b82f6;color:#fff}
        .status-completed{background:#22c55e;color:#fff}
        .order-details{font-size:13px;color:#94a3b8;line-height:1.7}
        .order-details span{color:#e2e8f0;font-weight:500}
        .proxy-data{margin-top:10px;padding:10px;background:#0d2137;border-radius:8px;font-family:monospace;font-size:12px;word-break:break-all;color:#60a5fa}
        .empty{text-align:center;color:#64748b;padding:30px;font-size:14px}
    </style>
</head>
<body>
<div class="container">
    <h1>🛒 Магазин прокси</h1>
    <div class="tabs">
        <div class="tab active" id="tabShop" onclick="switchTab('shop')">Заказать</div>
        <div class="tab" id="tabOrders" onclick="switchTab('orders')">Заказы</div>
    </div>
    <div id="shopTab">
        <div class="card">
            <h3>📦 Новый заказ</h3>
            <select id="proxyType"><option value="http">HTTP</option><option value="socks5">SOCKS5</option><option value="residential">Residential</option><option value="datacenter">Datacenter</option></select>
            <input type="number" id="quantity" value="1" min="1" max="100">
            <select id="country"><option value="">Загрузка...</option></select>
            <select id="duration"><option value="1 день">1 день</option><option value="1 неделя">1 неделя</option><option value="1 месяц">1 месяц</option><option value="3 месяца">3 месяца</option></select>
            <div class="price-box">💰 <b id="price">$0.00</b></div>
            <div class="balance" id="balance">$0.00</div>
            <button class="btn" onclick="createOrder()">🛒 Оформить</button>
        </div>
    </div>
    <div id="ordersTab" class="hidden"><div id="ordersList"></div></div>
</div>
<script>
var tg=window.Telegram.WebApp;tg.ready();tg.expand();
var uid=null,bal=0,prices={};
if(tg.initDataUnsafe&&tg.initDataUnsafe.user)uid=tg.initDataUnsafe.user.id;

async function api(u,m,d){try{var o={method:m||'GET'};if(d){o.headers={'Content-Type':'application/json'};o.body=JSON.stringify(d)}var r=await fetch(u,o);return await r.json()}catch(e){return{error:e.message}}}

async function loadData(){
    var c=await api('/api/countries');var countries=c.countries||[];
    document.getElementById('country').innerHTML=countries.map(function(c){return'<option value="'+c+'">'+c+'</option>'}).join('');
    var p=await api('/api/prices');prices=p.prices||{};
    if(uid){var u=await api('/api/user/'+uid);bal=u.balance||0;document.getElementById('balance').textContent='$'+bal.toFixed(2)}
    updatePrice();
    document.getElementById('proxyType').onchange=updatePrice;
    document.getElementById('quantity').oninput=updatePrice;
    document.getElementById('duration').onchange=updatePrice;
}

function updatePrice(){var t=document.getElementById('proxyType').value,q=parseInt(document.getElementById('quantity').value)||1,d=document.getElementById('duration').value,m={'1 день':1,'1 неделя':6,'1 месяц':20,'3 месяца':50};document.getElementById('price').textContent='$'+((prices[t]||2)*q*(m[d]||1)).toFixed(2)}

async function createOrder(){
    if(!uid){tg.showPopup({title:'Ошибка',message:'Откройте через бота'});return}
    var t=document.getElementById('proxyType').value,q=parseInt(document.getElementById('quantity').value),c=document.getElementById('country').value,d=document.getElementById('duration').value,m={'1 день':1,'1 неделя':6,'1 месяц':20,'3 месяца':50},total=(prices[t]||2)*q*(m[d]||1);
    if(bal<total){tg.showPopup({title:'Недостаточно',message:'Баланс: $'+bal.toFixed(2)+'\nНужно: $'+total.toFixed(2)});return}
    var r=await api('/api/create_order','POST',{user_id:uid,proxy_type:t,quantity:q,country:c,duration:d,price:total});
    if(r.success){bal=r.new_balance;document.getElementById('balance').textContent='$'+bal.toFixed(2);tg.showPopup({title:'✅ Успешно!',message:'Заказ #'+r.order_id+' создан!'});tg.HapticFeedback.notificationOccurred('success')}
    else tg.showPopup({title:'Ошибка',message:r.error||'Ошибка'})
}

function switchTab(tab){
    document.getElementById('tabShop').classList.remove('active');document.getElementById('tabOrders').classList.remove('active');
    document.getElementById('shopTab').classList.add('hidden');document.getElementById('ordersTab').classList.add('hidden');
    if(tab==='shop'){document.getElementById('tabShop').classList.add('active');document.getElementById('shopTab').classList.remove('hidden')}
    else{document.getElementById('tabOrders').classList.add('active');document.getElementById('ordersTab').classList.remove('hidden');loadOrders()}
}

async function loadOrders(){
    if(!uid)return;var d=await api('/api/user/'+uid+'/orders');var orders=d.orders||[];
    if(!orders.length){document.getElementById('ordersList').innerHTML='<div class="card"><div class="empty">📭 Нет заказов</div></div>';return}
    var e={'pending':'⏳','in_progress':'🔄','completed':'✅','cancelled':'❌','rejected':'🚫'},st={'pending':'Ожидает','in_progress':'В работе','completed':'Готов','cancelled':'Отменён','rejected':'Отклонён'};
    document.getElementById('ordersList').innerHTML=orders.map(function(o){return'<div class="order-card"><div class="order-header"><span class="order-id">#'+o.id+'</span><span class="status status-'+o.status+'">'+e[o.status]+' '+st[o.status]+'</span></div><div class="order-details"><span>'+o.proxy_type.toUpperCase()+'</span> × '+o.quantity+'<br>'+o.country+' · '+o.duration+'<br><span>$'+o.price.toFixed(2)+'</span></div>'+(o.status==='completed'&&o.proxy_data?'<div class="proxy-data">'+o.proxy_data+'</div>':'')+'</div>'}).join('')
}

loadData();
</script>
</body>
</html>""")

@app.get("/api/countries")
async def api_countries(): return {"countries": await db.get_countries()}

@app.get("/api/prices")
async def api_prices(): return {"prices": {'http':float(await db.get_setting('base_price_http','2')),'socks5':float(await db.get_setting('base_price_socks5','2.5')),'residential':float(await db.get_setting('base_price_residential','5')),'datacenter':float(await db.get_setting('base_price_datacenter','1.5'))}}

@app.get("/api/user/{uid}")
async def api_user(uid: int): u = await db.get_user(uid); return {"balance": u['balance'] if u else 0}

@app.get("/api/user/{uid}/orders")
async def api_orders(uid: int): return {"orders": await db.get_orders(uid)}

@app.post("/api/create_order")
async def api_create_order(r: Request):
    d = await r.json()
    u = await db.get_user(d['user_id'])
    if not u: return {"success":False,"error":"Пользователь не найден"}
    if u['balance'] < d['price']: return {"success":False,"error":"Недостаточно средств"}
    oid = await db.create_order(d['user_id'], d['proxy_type'], d['quantity'], d['country'], d['duration'], d['price'])
    for aid in ADMIN_IDS:
        try: await telegram_app.bot.send_message(aid, f"📋 #{oid}\n{u['first_name']}\n{d['proxy_type']} x{d['quantity']}\n💰 ${d['price']:.2f}")
        except: pass
    return {"success":True,"order_id":oid,"new_balance":u['balance']-d['price']}

if __name__ == "__main__":
    import uvicorn
    Thread(target=lambda: uvicorn.run(app, host="0.0.0.0", port=PORT, log_level="warning"), daemon=True).start()
    try: requests.get(f"https://api.telegram.org/bot{BOT_TOKEN}/deleteWebhook")
    except: pass
    
    async def run():
        global telegram_app
        await db.init()
        telegram_app = Application.builder().token(BOT_TOKEN).build()
        telegram_app.add_handler(CommandHandler("start", start))
        telegram_app.add_handler(CallbackQueryHandler(callbacks))
        telegram_app.add_handler(MessageHandler(filters.TEXT & ~filters.COMMAND, handle_msg))
        await telegram_app.initialize(); await telegram_app.start()
        logger.info("Bot started!")
        await telegram_app.updater.start_polling(drop_pending_updates=True)
        while True: await asyncio.sleep(3600)
    
    telegram_app = None
    asyncio.run(run())
