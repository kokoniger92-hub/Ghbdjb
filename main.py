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
                CREATE TABLE IF NOT EXISTS orders(id INTEGER PRIMARY KEY AUTOINCREMENT, user_id INTEGER, proxy_type TEXT, quantity INTEGER, country TEXT, duration TEXT, price REAL, status TEXT DEFAULT 'pending', admin_id INTEGER, proxy_data TEXT, created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP);
                CREATE TABLE IF NOT EXISTS settings(key TEXT PRIMARY KEY, value TEXT);
                CREATE TABLE IF NOT EXISTS countries(name TEXT PRIMARY KEY, is_active INTEGER DEFAULT 1);
                CREATE TABLE IF NOT EXISTS topups(id INTEGER PRIMARY KEY AUTOINCREMENT, user_id INTEGER, amount REAL, status TEXT DEFAULT 'pending', admin_id INTEGER, created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP);
                CREATE TABLE IF NOT EXISTS mirrors(id INTEGER PRIMARY KEY AUTOINCREMENT, name TEXT, token TEXT, url TEXT, is_active INTEGER DEFAULT 1);
                INSERT OR IGNORE INTO settings VALUES('orders_enabled','true'),('base_price_http','2.0'),('base_price_socks5','2.5'),('base_price_residential','5.0'),('base_price_datacenter','1.5'),('welcome','Добро пожаловать!');
            ''')
            for c in ['США','Россия','Германия','Франция','Великобритания','Канада','Япония','Корея','Нидерланды','Польша']:
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
    async def create_user(self, uid, uname, fname, lname): await self.q('INSERT OR REPLACE INTO users VALUES(?,?,?,?,?,0,0,0,0,CURRENT_TIMESTAMP)',[uid,uname or '',fname or '',lname or '',1 if uid in ADMIN_IDS else 0],False)
    async def get_balance(self, uid): u = await self.get_user(uid); return u['balance'] if u else 0
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
    async def add_mirror(self, n, t, u): await self.q('INSERT INTO mirrors(name,token,url) VALUES(?,?,?)',[n,t,u],False)
    async def get_mirrors(self): return await self.q('SELECT * FROM mirrors ORDER BY created_at DESC')
    async def toggle_mirror(self, mid):
        m = await self.q('SELECT * FROM mirrors WHERE id=?',[mid])
        if m: ns = 0 if m[0]['is_active'] else 1; await self.q('UPDATE mirrors SET is_active=? WHERE id=?',[ns,mid],False); return ns
    async def delete_mirror(self, mid): await self.q('DELETE FROM mirrors WHERE id=?',[mid],False)

db = DB()

ADMIN_KB = ReplyKeyboardMarkup([
    [KeyboardButton("📋 Заявки"), KeyboardButton("🔄 В работе")],
    [KeyboardButton("💰 Пополнения"), KeyboardButton("💬 Чат")],
    [KeyboardButton("📊 Статистика"), KeyboardButton("📢 Рассылка")],
    [KeyboardButton("⚙️ Настройки"), KeyboardButton("👥 Пользователи")],
    [KeyboardButton("🔗 Зеркала")]
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
    
    if u.id in ADMIN_IDS and u.id in active_chats: await chat_msg(update, context); return
    if u.id in user_states: await state_msg(update, context); return
    
    if u.id in ADMIN_IDS: await admin_menu(update, context, t)
    else: await user_menu(update, context, t)

async def chat_msg(update: Update, context):
    u = update.effective_user; t = update.message.text.strip()
    if t == "🔙 Выйти": del active_chats[u.id]; await update.message.reply_text("Чат закрыт", reply_markup=ADMIN_KB); return
    oid = active_chats[u.id]; o = await db.get_order(oid)
    if not o: del active_chats[u.id]; await update.message.reply_text("Заказ не найден", reply_markup=ADMIN_KB); return
    try: await context.bot.send_message(o['user_id'], f"💬 Поддержка:\n\n{t}"); await update.message.reply_text("✅ Отправлено")
    except Exception as e: await update.message.reply_text(f"❌ {e}")

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
        except ValueError: await update.message.reply_text("❌ Число:")
    elif a == 'broadcast':
        users = await db.get_broadcast_users(); ok = 0
        for uid in users:
            try: await context.bot.send_message(uid, t); ok += 1
            except: pass; await asyncio.sleep(0.03)
        del user_states[u.id]; await update.message.reply_text(f"📢 {ok}/{len(users)}", reply_markup=ADMIN_KB)
    elif a == 'welcome': await db.set_setting('welcome', t); del user_states[u.id]; await update.message.reply_text("✅", reply_markup=ADMIN_KB)
    elif a == 'price':
        try: await db.set_setting(f"base_price_{s['type']}", t); del user_states[u.id]; await update.message.reply_text("✅", reply_markup=ADMIN_KB)
        except: await update.message.reply_text("❌ Число:")
    elif a == 'new_country': await db.add_country(t); del user_states[u.id]; await update.message.reply_text(f"✅ {t}", reply_markup=ADMIN_KB)
    elif a == 'del_country': await db.remove_country(t); del user_states[u.id]; await update.message.reply_text(f"✅ {t}", reply_markup=ADMIN_KB)
    elif a == 'proxy_data':
        await db.update_order(s['oid'], 'completed', u.id, t); o = await db.get_order(s['oid']); del user_states[u.id]
        if o:
            try: await context.bot.send_message(o['user_id'], f"✅ Заказ #{s['oid']} готов!\n📦 Данные:\n{t}")
            except: pass
        await update.message.reply_text(f"✅ Заказ #{s['oid']} завершён!", reply_markup=ADMIN_KB)
    elif a == 'chat_order':
        try:
            oid = int(t); o = await db.get_order(oid)
            if not o: await update.message.reply_text("❌ Не найден"); return
            active_chats[u.id] = oid; del user_states[u.id]
            await update.message.reply_text(f"💬 Чат #{oid}\nВведите сообщение:", reply_markup=ReplyKeyboardMarkup([[KeyboardButton("🔙 Выйти")]], resize_keyboard=True))
        except: await update.message.reply_text("❌ ID (число):")
    elif a == 'mirror_name': s['mname'] = t; s['action'] = 'mirror_token'; await update.message.reply_text("🔑 Токен:")
    elif a == 'mirror_token': s['mtoken'] = t; s['action'] = 'mirror_url'; await update.message.reply_text("🔗 URL:")
    elif a == 'mirror_url': await db.add_mirror(s['mname'], s['mtoken'], t); del user_states[u.id]; await update.message.reply_text("✅", reply_markup=ADMIN_KB)

async def admin_menu(update: Update, context, t):
    u = update.effective_user
    if "пополнен" in t.lower():
        tops = await db.get_pending_topups()
        if not tops: await update.message.reply_text("✅ Нет"); return
        for tp in tops:
            bal = (await db.get_user(tp['user_id']))['balance']
            await update.message.reply_text(f"💰 #{tp['id']}\n{tp['first_name']}\n${tp['amount']:.2f}\nБаланс: ${bal:.2f}",
                reply_markup=InlineKeyboardMarkup([[InlineKeyboardButton("✅", callback_data=f"at_{tp['id']}"), InlineKeyboardButton("❌", callback_data=f"rt_{tp['id']}")]]))
    elif "заявк" in t.lower() and "работ" not in t.lower():
        orders = await db.get_pending_orders()
        if not orders: await update.message.reply_text("✅ Нет"); return
        for o in orders:
            await update.message.reply_text(f"📋 #{o['id']}\n{o['first_name']}\n{o['proxy_type']} x{o['quantity']}\n{o['country']} | {o['duration']}\n${o['price']:.2f}",
                reply_markup=InlineKeyboardMarkup([[InlineKeyboardButton("✅ Взять", callback_data=f"tk_{o['id']}"), InlineKeyboardButton("❌", callback_data=f"rj_{o['id']}")]]))
    elif "работ" in t.lower():
        orders = await db.get_in_progress()
        if not orders: await update.message.reply_text("✅ Нет"); return
        for o in orders:
            await update.message.reply_text(f"🔄 #{o['id']}\n{o['first_name']}\n{o['proxy_type']} x{o['quantity']}\n${o['price']:.2f}",
                reply_markup=InlineKeyboardMarkup([[InlineKeyboardButton("✅ Готово", callback_data=f"cp_{o['id']}"), InlineKeyboardButton("💬", callback_data=f"ch_{o['id']}")]]))
    elif "чат" in t.lower(): user_states[u.id] = {'action': 'chat_order'}; await update.message.reply_text("ID заказа:", reply_markup=CANCEL_KB)
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
        await update.message.reply_text(f"⚙️ Заказы: {'✅' if e=='true' else '❌'}\nHTTP:${p['http']} SOCKS5:${p['socks5']}\nRes:${p['residential']} DC:${p['datacenter']}",
            reply_markup=InlineKeyboardMarkup([[InlineKeyboardButton("🔄 Заказы", callback_data="tog")],[InlineKeyboardButton("✏️ Приветствие", callback_data="ew")],[InlineKeyboardButton("HTTP", callback_data="sp_http"),InlineKeyboardButton("SOCKS5", callback_data="sp_socks5")],[InlineKeyboardButton("Residential", callback_data="sp_residential"),InlineKeyboardButton("Datacenter", callback_data="sp_datacenter")]]))
    elif "зеркал" in t.lower():
        ms = await db.get_mirrors()
        msg = "🔗 Зеркала:\n" if ms else "🔗 Нет зеркал"
        for m in ms: msg += f"\n{'✅' if m['is_active'] else '❌'} {m['name']} | {m['url'][:30]}..."
        await update.message.reply_text(msg, reply_markup=InlineKeyboardMarkup([[InlineKeyboardButton("➕", callback_data="am"),InlineKeyboardButton("🔄", callback_data="tm"),InlineKeyboardButton("❌", callback_data="dm")]]))

async def user_menu(update: Update, context, t):
    u = update.effective_user
    if "заказ" in t.lower():
        orders = await db.get_orders(u.id)
        if not orders: await update.message.reply_text("📭 Нет"); return
        for o in orders:
            em = {'pending':'⏳','in_progress':'🔄','completed':'✅','cancelled':'❌','rejected':'🚫'}.get(o['status'],'?')
            msg = f"{em} #{o['id']}\n{o['proxy_type']} x{o['quantity']}\n{o['country']} | {o['duration']}\n${o['price']:.2f}"
            if o['proxy_data']: msg += f"\n📋 {o['proxy_data']}"
            kb = InlineKeyboardMarkup([[InlineKeyboardButton("❌ Отменить", callback_data=f"co_{o['id']}")]]) if o['status']=='pending' else None
            await update.message.reply_text(msg, reply_markup=kb)
    elif "баланс" in t.lower(): await update.message.reply_text(f"💳 ${(await db.get_user(u.id))['balance']:.2f}")
    elif "пополн" in t.lower(): user_states[u.id] = {'action': 'topup'}; await update.message.reply_text("Сумма USD:", reply_markup=CANCEL_KB)

async def callbacks(update: Update, context: ContextTypes.DEFAULT_TYPE):
    q = update.callback_query; u = q.from_user; d = q.data; await q.answer()
    
    if d.startswith("tk_"):
        oid = int(d.split("_")[1]); await db.update_order(oid, 'in_progress', u.id)
        o = await db.get_order(oid); await q.edit_message_text(f"✅ Взято")
        if o: await context.bot.send_message(o['user_id'], f"✅ Заказ #{oid} в работе!")
    elif d.startswith("rj_"):
        oid = int(d.split("_")[1]); o = await db.get_order(oid)
        if o:
            await db.update_order(oid, 'rejected', u.id)
            await db.q('UPDATE users SET balance=balance+? WHERE user_id=?',[o['price'],o['user_id']],False)
            await q.edit_message_text("❌ Отклонено")
            await context.bot.send_message(o['user_id'], f"❌ #{oid} отклонён\n💰 +${o['price']:.2f}")
    elif d.startswith("cp_"): oid = int(d.split("_")[1]); user_states[u.id] = {'action': 'proxy_data', 'oid': oid}; await q.edit_message_text("📝 Данные прокси:")
    elif d.startswith("ch_"): oid = int(d.split("_")[1]); active_chats[u.id] = oid; await q.edit_message_text(f"💬 Чат #{oid}\nПишите в чат")
    elif d.startswith("co_"):
        oid = int(d.split("_")[1]); ok, res = await db.cancel_order(oid, u.id)
        if ok: await q.edit_message_text(f"❌ Отменено\n💰 +${res:.2f}")
        else: await q.answer(res, show_alert=True)
    elif d.startswith("at_"):
        tid = int(d.split("_")[1]); tp = await db.approve_topup(tid, u.id)
        if tp: await q.edit_message_text("✅"); await context.bot.send_message(tp['user_id'], f"✅ +${tp['amount']:.2f}")
    elif d.startswith("rt_"):
        tid = int(d.split("_")[1]); tp = await db.reject_topup(tid, u.id)
        if tp: await q.edit_message_text("❌"); await context.bot.send_message(tp['user_id'], "❌ Отклонено")
    elif d == "tog":
        cur = await db.get_setting('orders_enabled'); new = 'false' if cur == 'true' else 'true'
        await db.set_setting('orders_enabled', new); await q.edit_message_text(f"Заказы: {'✅' if new=='true' else '❌'}")
    elif d == "ew": user_states[u.id] = {'action': 'welcome'}; await q.edit_message_text("Приветствие:")
    elif d.startswith("sp_"): user_states[u.id] = {'action': 'price', 'type': d.split("_")[1]}; await q.edit_message_text("Цена:")
    elif d == "am": user_states[u.id] = {'action': 'mirror_name'}; await q.edit_message_text("Название:")
    elif d == "tm":
        ms = await db.get_mirrors()
        if not ms: await q.edit_message_text("Нет"); return
        await q.edit_message_text("Выберите:", reply_markup=InlineKeyboardMarkup([[InlineKeyboardButton(f"{'✅' if m['is_active'] else '❌'} {m['name']}", callback_data=f"tgm_{m['id']}")] for m in ms]))
    elif d == "dm":
        ms = await db.get_mirrors()
        if not ms: await q.edit_message_text("Нет"); return
        await q.edit_message_text("Удалить:", reply_markup=InlineKeyboardMarkup([[InlineKeyboardButton(f"❌ {m['name']}", callback_data=f"dlm_{m['id']}")] for m in ms]))
    elif d.startswith("tgm_"):
        mid = int(d.split("_")[1]); ns = await db.toggle_mirror(mid)
        await q.edit_message_text(f"{'✅' if ns else '❌'}")
    elif d.startswith("dlm_"):
        mid = int(d.split("_")[1]); await db.delete_mirror(mid); await q.edit_message_text("❌ Удалено")

@asynccontextmanager
async def lifespan(app: FastAPI): await db.init(); yield

app = FastAPI(lifespan=lifespan)

@app.get("/")
async def root(): return {"status":"ok"}

@app.get("/health")
async def health(): return {"status":"healthy"}

@app.get("/app", response_class=HTMLResponse)
async def user_app():
    return HTMLResponse(content="""<!DOCTYPE html><html lang="ru"><head><meta charset="UTF-8"><meta name="viewport" content="width=device-width,initial-scale=1"><title>Магазин</title><script src="https://telegram.org/js/telegram-web-app.js"></script><style>
*{margin:0;padding:0;box-sizing:border-box}body{font-family:Arial,sans-serif;background:#000;color:#fff;padding:10px}.c{max-width:500px;margin:0 auto}h1{text-align:center;margin:15px 0;font-size:20px}.card{background:#1a1a1a;padding:15px;border-radius:10px;margin:10px 0}.btn{display:block;width:100%;padding:12px;margin:8px 0;background:#fff;color:#000;border:none;border-radius:8px;font-size:15px;font-weight:bold;cursor:pointer}.btn:active{background:#ddd}select,input{width:100%;padding:10px;margin:5px 0;background:#0a0a0a;border:1px solid #333;border-radius:8px;color:#fff;font-size:14px}.bal{font-size:30px;text-align:center;margin:10px 0;font-weight:bold}.hidden{display:none}.tabs{display:flex;gap:5px;margin:15px 0}.tab{flex:1;padding:10px;text-align:center;background:#1a1a1a;border-radius:8px;cursor:pointer;font-weight:bold}.tab.active{background:#fff;color:#000}.oc{background:#1a1a1a;padding:12px;border-radius:8px;margin:8px 0}.sp{color:#ff0}.sc{color:#0f0}.si{color:#08f}</style></head><body><div class="c">
<h1>Магазин прокси</h1>
<div class="tabs"><div class="tab active" id="ts" onclick="st('shop')">Заказать</div><div class="tab" id="to" onclick="st('orders')">Заказы</div></div>
<div id="shop"><div class="card"><h3>Новый заказ</h3>
<select id="type"><option value="http">HTTP</option><option value="socks5">SOCKS5</option><option value="residential">Residential</option><option value="datacenter">Datacenter</option></select>
<input type="number" id="qty" placeholder="Количество" value="1" min="1" max="100">
<select id="country"></select>
<select id="dur"><option value="1 день">1 день</option><option value="1 неделя">1 неделя</option><option value="1 месяц">1 месяц</option><option value="3 месяца">3 месяца</option></select>
<div style="text-align:center;margin:10px 0">Цена: <b id="price">$0</b></div>
<div class="bal" id="balance">$0</div>
<button class="btn" onclick="order()">Заказать</button></div></div>
<div id="orders" class="hidden"><div id="olist">Загрузка...</div></div>
</div><script>
var tg=window.Telegram.WebApp;tg.ready();tg.expand();
var uid=null,bal=0,prices={},countries=[];
if(tg.initDataUnsafe&&tg.initDataUnsafe.user)uid=tg.initDataUnsafe.user.id;
async function api(u,m,d){try{var o={method:m||'GET'};if(d){o.headers={'Content-Type':'application/json'};o.body=JSON.stringify(d)}var r=await fetch(u,o);return await r.json()}catch(e){return{error:e.message}}}
function up(){var t=document.getElementById('type').value,q=parseInt(document.getElementById('qty').value)||1,d=document.getElementById('dur').value,m={'1 день':1,'1 неделя':6,'1 месяц':20,'3 месяца':50};document.getElementById('price').textContent='$'+((prices[t]||2)*q*(m[d]||1)).toFixed(2)}
async function ld(){var c=await api('/api/countries');countries=c.countries||[];document.getElementById('country').innerHTML=countries.map(function(c){return'<option value="'+c+'">'+c+'</option>'}).join('');var p=await api('/api/prices');prices=p.prices||{};if(uid){var u=await api('/api/user/'+uid);bal=u.balance||0;document.getElementById('balance').textContent='$'+bal.toFixed(2)}up();document.getElementById('type').onchange=up;document.getElementById('qty').oninput=up;document.getElementById('dur').onchange=up}
async function order(){if(!uid){tg.showPopup({title:'Ошибка',message:'Откройте через бота'});return}
var t=document.getElementById('type').value,q=parseInt(document.getElementById('qty').value),c=document.getElementById('country').value,d=document.getElementById('dur').value,m={'1 день':1,'1 неделя':6,'1 месяц':20,'3 месяца':50},total=(prices[t]||2)*q*(m[d]||1);
if(bal<total){tg.showPopup({title:'Недостаточно',message:'Баланс: $'+bal.toFixed(2)+'\nНужно: $'+total.toFixed(2)});return}
var r=await api('/api/create_order','POST',{user_id:uid,proxy_type:t,quantity:q,country:c,duration:d,price:total});
if(r.success){bal=r.new_balance;document.getElementById('balance').textContent='$'+bal.toFixed(2);tg.showPopup({title:'Успешно!',message:'Заказ #'+r.order_id+' создан!'});tg.HapticFeedback.notificationOccurred('success')}else{tg.showPopup({title:'Ошибка',message:r.error||'Ошибка'})}}
function st(tab){document.getElementById('ts').classList.remove('active');document.getElementById('to').classList.remove('active');document.getElementById('shop').classList.add('hidden');document.getElementById('orders').classList.add('hidden');
if(tab=='shop'){document.getElementById('ts').classList.add('active');document.getElementById('shop').classList.remove('hidden')}else{document.getElementById('to').classList.add('active');document.getElementById('orders').classList.remove('hidden');lo()}}
async function lo(){if(!uid)return;var d=await api('/api/user/'+uid+'/orders');var o=d.orders||[];if(!o.length){document.getElementById('olist').innerHTML='<div class="card" style="text-align:center">Нет заказов</div>';return}
var e={'pending':'⏳','in_progress':'🔄','completed':'✅','cancelled':'❌','rejected':'🚫'},st={'pending':'Ожидает','in_progress':'В работе','completed':'Готов','cancelled':'Отменён','rejected':'Отклонён'};
document.getElementById('olist').innerHTML=o.map(function(o){return'<div class="oc"><b>#'+o.id+'</b> <span class="s'+o.status+'">'+e[o.status]+' '+st[o.status]+'</span><br>'+o.proxy_type.toUpperCase()+' x'+o.quantity+'<br>'+o.country+' | '+o.duration+'<br><b>$'+o.price.toFixed(2)+'</b>'+(o.status=='completed'&&o.proxy_data?'<div style="margin-top:8px;padding:8px;background:#000;border-radius:5px;font-size:12px">'+o.proxy_data+'</div>':'')+'</div>'}).join('')}
ld();</script></body></html>""")

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
        try: await telegram_app.bot.send_message(aid, f"📋 Новый заказ #{oid}\n👤 {u['first_name']}\n📦 {d['proxy_type']} x{d['quantity']}\n💰 ${d['price']:.2f}")
        except: pass
    return {"success":True,"order_id":oid,"new_balance":u['balance']-d['price']}

if __name__ == "__main__":
    import uvicorn
    Thread(target=lambda: uvicorn.run(app, host="0.0.0.0", port=PORT, log_level="warning"), daemon=True).start()
    
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
