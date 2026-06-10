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
PORT = int(os.getenv('PORT', 8000))
APP_URL = os.getenv('APP_URL', 'https://ghbdjb-tusy.onrender.com')

user_states = {}

class DB:
    def __init__(self): self.path = '/tmp/proxy_bot.db'
    
    async def init(self):
        async with aiosqlite.connect(self.path) as db:
            await db.execute('PRAGMA journal_mode=WAL')
            await db.executescript('''
                CREATE TABLE IF NOT EXISTS users(user_id INTEGER PRIMARY KEY, username TEXT, first_name TEXT, balance REAL DEFAULT 0, total_spent REAL DEFAULT 0, orders_count INTEGER DEFAULT 0, is_banned INTEGER DEFAULT 0, created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP);
                CREATE TABLE IF NOT EXISTS orders(id INTEGER PRIMARY KEY AUTOINCREMENT, user_id INTEGER, proxy_type TEXT, quantity INTEGER, country TEXT, duration TEXT, price REAL, status TEXT DEFAULT 'pending', admin_id INTEGER, proxy_data TEXT, created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP);
                CREATE TABLE IF NOT EXISTS settings(key TEXT PRIMARY KEY, value TEXT);
                CREATE TABLE IF NOT EXISTS countries(name TEXT PRIMARY KEY);
                CREATE TABLE IF NOT EXISTS topups(id INTEGER PRIMARY KEY AUTOINCREMENT, user_id INTEGER, amount REAL, status TEXT DEFAULT 'pending', admin_id INTEGER, created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP);
                INSERT OR IGNORE INTO settings VALUES('orders_enabled','true'),('base_price_http','2.0'),('base_price_socks5','2.5'),('base_price_residential','5.0'),('base_price_datacenter','1.5');
            ''')
            for c in ['США','Россия','Германия','Франция','Великобритания','Канада','Япония','Корея','Нидерланды','Польша','Испания','Италия','Швеция']:
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
    async def create_user(self, uid, uname, fname):
        await self.q('INSERT OR IGNORE INTO users(user_id,username,first_name) VALUES(?,?,?)',[uid,uname or '',fname or ''],False)
    
    async def create_order(self, uid, ptype, qty, country, dur, price):
        oid = await self.q('INSERT INTO orders(user_id,proxy_type,quantity,country,duration,price) VALUES(?,?,?,?,?,?)',[uid,ptype,qty,country,dur,price],False)
        await self.q('UPDATE users SET balance=balance-?, orders_count=orders_count+1, total_spent=total_spent+? WHERE user_id=?',[price,price,uid],False)
        return oid
    
    async def get_orders(self, uid): return await self.q('SELECT * FROM orders WHERE user_id=? ORDER BY id DESC LIMIT 30',[uid])
    async def get_pending_orders(self): return await self.q("SELECT o.*,u.username,u.first_name FROM orders o JOIN users u ON o.user_id=u.user_id WHERE o.status='pending' ORDER BY o.id DESC")
    async def get_in_progress(self): return await self.q("SELECT o.*,u.username,u.first_name FROM orders o JOIN users u ON o.user_id=u.user_id WHERE o.status='in_progress' ORDER BY o.id DESC")
    async def get_order(self, oid): r = await self.q('SELECT o.*,u.username,u.first_name FROM orders o JOIN users u ON o.user_id=u.user_id WHERE o.id=?',[oid]); return r[0] if r else None
    async def update_order(self, oid, status, aid=None, pdata=''):
        if pdata: await self.q('UPDATE orders SET status=?,admin_id=?,proxy_data=? WHERE id=?',[status,aid,pdata,oid],False)
        else: await self.q('UPDATE orders SET status=?,admin_id=? WHERE id=?',[status,aid,oid],False)
    async def cancel_order(self, oid, uid):
        o = await self.q('SELECT * FROM orders WHERE id=? AND user_id=? AND status=?',[oid,uid,'pending'])
        if not o: return False,"Не найден"
        await self.q("UPDATE orders SET status='cancelled' WHERE id=?",[oid],False)
        await self.q('UPDATE users SET balance=balance+? WHERE user_id=?',[o[0]['price'],uid],False)
        return True,o[0]['price']
    async def get_pending_topups(self): return await self.q("SELECT t.*,u.username,u.first_name FROM topups t JOIN users u ON t.user_id=u.user_id WHERE t.status='pending' ORDER BY t.id DESC")
    async def create_topup(self, uid, amt): return await self.q('INSERT INTO topups(user_id,amount) VALUES(?,?)',[uid,amt],False)
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
        r = await self.q("SELECT COALESCE(SUM(price),0) as t FROM orders WHERE status='completed'")
        return {'users':u[0]['c'],'orders':o[0]['c'],'completed':c[0]['c'],'revenue':round(r[0]['t'],2)}
    async def get_all_users(self): return await self.q('SELECT * FROM users ORDER BY user_id DESC LIMIT 50')
    async def get_countries(self): return [r['name'] for r in await self.q('SELECT name FROM countries ORDER BY name')]
    async def get_broadcast_users(self): return [r['user_id'] for r in await self.q('SELECT user_id FROM users WHERE is_banned=0')]

db = DB()

ADMIN_KB = ReplyKeyboardMarkup([
    [KeyboardButton("📋 Заявки"), KeyboardButton("🔄 В работе")],
    [KeyboardButton("💰 Пополнения"), KeyboardButton("📊 Статистика")],
    [KeyboardButton("📢 Рассылка"), KeyboardButton("👥 Пользователи")]
], resize_keyboard=True)

USER_KB = ReplyKeyboardMarkup([
    [KeyboardButton("🛒 Магазин", web_app=WebAppInfo(url=f"{APP_URL}/app"))],
    [KeyboardButton("📋 Мои заказы"), KeyboardButton("💳 Баланс")],
    [KeyboardButton("💰 Пополнить")]
], resize_keyboard=True)

CANCEL_KB = ReplyKeyboardMarkup([[KeyboardButton("🔙 Отмена")]], resize_keyboard=True)

async def start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    u = update.effective_user
    await db.create_user(u.id, u.username, u.first_name)
    if u.id in ADMIN_IDS:
        await update.message.reply_text("🔧 Админ-панель", reply_markup=ADMIN_KB)
    else:
        await update.message.reply_text("👋 Добро пожаловать!\nНажмите кнопку для заказа:", reply_markup=USER_KB)

async def handle_msg(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not update.message or not update.message.text: return
    u = update.effective_user; t = update.message.text.strip()
    await db.create_user(u.id, u.username, u.first_name)
    
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
            if amt < 1: await update.message.reply_text("❌ Мин $1"); return
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
    elif a == 'proxy_data':
        await db.update_order(s['oid'], 'completed', u.id, t); o = await db.get_order(s['oid']); del user_states[u.id]
        if o:
            try: await context.bot.send_message(o['user_id'], f"✅ Заказ #{s['oid']} готов!\n📦 Данные:\n{t}")
            except: pass
        await update.message.reply_text(f"✅ #{s['oid']} завершён!", reply_markup=ADMIN_KB)

async def admin_menu(update: Update, context, t):
    u = update.effective_user
    
    if "пополнен" in t.lower():
        tops = await db.get_pending_topups()
        if not tops: await update.message.reply_text("✅ Нет"); return
        for tp in tops:
            bal = (await db.get_user(tp['user_id']))['balance']
            await update.message.reply_text(f"💰 #{tp['id']}\n{tp['first_name']}\n${tp['amount']:.2f}\nБаланс: ${bal:.2f}",
                reply_markup=InlineKeyboardMarkup([[
                    InlineKeyboardButton("✅ Подтвердить", callback_data=f"at_{tp['id']}"),
                    InlineKeyboardButton("❌ Отклонить", callback_data=f"rt_{tp['id']}")
                ]]))
    
    elif "заявк" in t.lower():
        orders = await db.get_pending_orders()
        if not orders: await update.message.reply_text("✅ Нет заявок"); return
        for o in orders:
            await update.message.reply_text(
                f"📋 #{o['id']}\n{o['first_name']}\n{o['proxy_type']} x{o['quantity']}\n{o['country']} | {o['duration']}\n${o['price']:.2f}",
                reply_markup=InlineKeyboardMarkup([[
                    InlineKeyboardButton("✅ Взять", callback_data=f"tk_{o['id']}"),
                    InlineKeyboardButton("❌ Отклонить", callback_data=f"rj_{o['id']}")
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
        await update.message.reply_text(f"📊\n👥 {s['users']}\n📦 {s['orders']}\n✅ {s['completed']}\n💰 ${s['revenue']}")
    
    elif "пользовател" in t.lower():
        users = await db.get_all_users()
        msg = "👥\n"
        for uu in users: msg += f"{'🚫' if uu['is_banned'] else '✅'} {uu['first_name'] or '—'} | ${uu['balance']:.2f} | {uu['user_id']}\n"
        await update.message.reply_text(msg)
    
    elif "рассылк" in t.lower(): user_states[u.id] = {'action': 'broadcast'}; await update.message.reply_text("Текст:", reply_markup=CANCEL_KB)

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
            await q.edit_message_text(f"❌ Отклонено")
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
        if tp: await q.edit_message_text(f"✅ +${tp['amount']:.2f}"); await context.bot.send_message(tp['user_id'], f"✅ +${tp['amount']:.2f}")
    
    elif d.startswith("rt_"):
        tid = int(d.split("_")[1]); tp = await db.reject_topup(tid, u.id)
        if tp: await q.edit_message_text("❌"); await context.bot.send_message(tp['user_id'], "❌ Отклонено")

# FastAPI
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
    <title>Магазин</title>
    <script src="https://telegram.org/js/telegram-web-app.js"></script>
    <style>
        *{margin:0;padding:0;box-sizing:border-box}
        body{font-family:Arial,sans-serif;background:#0a1929;color:#fff;padding:10px}
        .c{max-width:500px;margin:0 auto}
        h1{text-align:center;margin:15px 0;font-size:20px;color:#60a5fa}
        .card{background:#132f4c;padding:15px;border-radius:12px;margin:10px 0;border:1px solid #1e4976}
        h3{color:#93c5fd;margin-bottom:10px}
        .btn{display:block;width:100%;padding:12px;margin:8px 0;background:#3b82f6;color:#fff;border:none;border-radius:10px;font-size:15px;font-weight:bold;cursor:pointer}
        .btn:active{background:#2563eb}
        select,input{width:100%;padding:10px;margin:5px 0;background:#0d2137;border:1px solid #1e4976;border-radius:8px;color:#fff;font-size:14px}
        .bal{font-size:30px;text-align:center;margin:10px 0;font-weight:bold;color:#60a5fa}
        .hidden{display:none}
        .tabs{display:flex;gap:5px;margin:15px 0}
        .tab{flex:1;padding:10px;text-align:center;background:#132f4c;border-radius:8px;cursor:pointer;font-weight:bold;color:#94a3b8}
        .tab.active{background:#3b82f6;color:#fff}
        .oc{background:#132f4c;padding:12px;border-radius:10px;margin:8px 0;border:1px solid #1e4976}
        .sp{color:#f59e0b}.sc{color:#22c55e}.si{color:#3b82f6}
    </style>
</head>
<body>
<div class="c">
<h1>🛒 Магазин прокси</h1>
<div class="tabs"><div class="tab active" id="ts" onclick="st('shop')">Заказать</div><div class="tab" id="to" onclick="st('orders')">Заказы</div></div>

<div id="shop">
<div class="card">
<h3>Новый заказ</h3>
<select id="type"><option value="http">HTTP</option><option value="socks5">SOCKS5</option><option value="residential">Residential</option><option value="datacenter">Datacenter</option></select>
<input type="number" id="qty" value="1" min="1" max="100">
<select id="country"><option value="">Загрузка...</option></select>
<select id="dur"><option value="1 день">1 день</option><option value="1 неделя">1 неделя</option><option value="1 месяц">1 месяц</option><option value="3 месяца">3 месяца</option></select>
<div style="text-align:center;margin:10px 0">💰 <b id="price">$0.00</b></div>
<div class="bal" id="balance">$0.00</div>
<button class="btn" onclick="order()">Оформить заказ</button>
</div></div>

<div id="orders" class="hidden"><div id="olist"></div></div>
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
    if(uid){var u=await api('/api/user/'+uid);bal=u.balance||0;document.getElementById('balance').textContent='💳 $'+bal.toFixed(2)}
    updatePrice();
    document.getElementById('type').onchange=updatePrice;
    document.getElementById('qty').oninput=updatePrice;
    document.getElementById('dur').onchange=updatePrice;
}

function updatePrice(){var t=document.getElementById('type').value,q=parseInt(document.getElementById('qty').value)||1,d=document.getElementById('dur').value,m={'1 день':1,'1 неделя':6,'1 месяц':20,'3 месяца':50};document.getElementById('price').textContent='$'+((prices[t]||2)*q*(m[d]||1)).toFixed(2)}

async function order(){
    if(!uid){tg.showPopup({title:'Ошибка',message:'Откройте через кнопку в боте Telegram'});return}
    var t=document.getElementById('type').value,q=parseInt(document.getElementById('qty').value),c=document.getElementById('country').value,d=document.getElementById('dur').value,m={'1 день':1,'1 неделя':6,'1 месяц':20,'3 месяца':50},total=(prices[t]||2)*q*(m[d]||1);
    if(bal<total){tg.showPopup({title:'Недостаточно',message:'Баланс: $'+bal.toFixed(2)+'\nНужно: $'+total.toFixed(2)});return}
    var r=await api('/api/create_order','POST',{user_id:uid,proxy_type:t,quantity:q,country:c,duration:d,price:total});
    if(r.success){bal=r.new_balance;document.getElementById('balance').textContent='💳 $'+bal.toFixed(2);tg.showPopup({title:'✅ Успешно!',message:'Заказ #'+r.order_id+' создан!'})}
    else tg.showPopup({title:'Ошибка',message:r.error||'Ошибка'})
}

function st(tab){
    document.getElementById('ts').classList.remove('active');document.getElementById('to').classList.remove('active');
    document.getElementById('shop').classList.add('hidden');document.getElementById('orders').classList.add('hidden');
    if(tab==='shop'){document.getElementById('ts').classList.add('active');document.getElementById('shop').classList.remove('hidden')}
    else{document.getElementById('to').classList.add('active');document.getElementById('orders').classList.remove('hidden');loadOrders()}
}

async function loadOrders(){
    if(!uid)return;var d=await api('/api/user/'+uid+'/orders');var orders=d.orders||[];
    if(!orders.length){document.getElementById('olist').innerHTML='<div class="card" style="text-align:center">📭 Нет заказов</div>';return}
    var e={'pending':'⏳','in_progress':'🔄','completed':'✅','cancelled':'❌','rejected':'🚫'},s={'pending':'Ожидает','in_progress':'В работе','completed':'Готов','cancelled':'Отменён','rejected':'Отклонён'};
    document.getElementById('olist').innerHTML=orders.map(function(o){return'<div class="oc"><b>#'+o.id+'</b> <span class="s'+o.status+'">'+e[o.status]+' '+s[o.status]+'</span><br>'+o.proxy_type.toUpperCase()+' x'+o.quantity+'<br>'+o.country+' | '+o.duration+'<br><b>$'+o.price.toFixed(2)+'</b>'+(o.status==='completed'&&o.proxy_data?'<div style="margin-top:8px;padding:8px;background:#0d2137;border-radius:5px;font-size:12px">'+o.proxy_data+'</div>':'')+'</div>'}).join('')
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
