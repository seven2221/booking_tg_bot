import logging
import os
import re
import telebot
import time
from datetime import datetime, timedelta
from dotenv import load_dotenv
from telebot import types
from telebot.types import InlineKeyboardButton, InlineKeyboardMarkup
from lib.db_init import get_connection, init_db
from lib.schedule_generator import create_schedule_grid_image
from lib.keyboards import create_confirmation_keyboard
from lib.schedule_tasks import (
    add_subscriber_to_slot,
    get_booked_days_filtered,
    get_free_days,
    get_grouped_bookings_for_cancellation,
    get_schedule_for_day,
    get_busy_times_for_day,
)
from lib.utils import (
    book_slots,
    format_date,
    get_hour_word,
    is_admin,
    reset_user_state,
    validate_input,
    escape_markdown,
    get_booking_info_by_id,
)

logging.basicConfig(level=logging.INFO)
logger = logging.getLogger(__name__)

load_dotenv()
MAIN_BOT_TOKEN = os.getenv("MAIN_BOT_TOKEN")
ADMIN_BOT_TOKEN = os.getenv("ADMIN_BOT_TOKEN")
ADMIN_IDS = [int(x) for x in os.getenv("ADMIN_IDS", "").split(",") if x.strip().isdigit()]

if not MAIN_BOT_TOKEN:
    raise RuntimeError("MAIN_BOT_TOKEN не задан в окружении")

main_bot = telebot.TeleBot(MAIN_BOT_TOKEN)
admin_bot = telebot.TeleBot(ADMIN_BOT_TOKEN)
user_states: dict[int, dict] = {}

@main_bot.message_handler(func=lambda msg: msg.text == "На главную")
def handle_back_to_main(message):
    reset_user_state(message.chat.id, user_states)
    show_menu(message)


### Главное меню ###

def show_menu(message):
    keyboard = types.ReplyKeyboardMarkup(resize_keyboard=True)
    keyboard.add(types.KeyboardButton("Посмотреть расписание"))
    keyboard.add(types.KeyboardButton("Забронировать время"))
    keyboard.add(types.KeyboardButton("Отменить бронь"))
    keyboard.add(types.KeyboardButton("Быть в курсе, если освободится время"))
    keyboard.add(types.KeyboardButton("Посмотреть прайс"))
    main_bot.send_message(
        message.chat.id,
        "(Это БЕТА-версия бота. Большая просьба обо всех найденных неисправностях "
        "и пожеланиях по улучшениям сообщать @cyberocalypse или @seven2221)\n\n"
        "Выберите действие:",
        reply_markup=keyboard,
    )
    reset_user_state(message.chat.id, user_states)

@main_bot.message_handler(commands=["start"])
def start(message):
    main_bot.set_my_commands([telebot.types.BotCommand("/start", "Главное меню")])
    show_menu(message)


### Оформление брони ###

@main_bot.message_handler(func=lambda msg: msg.text == "Забронировать время")
def book_time(message):
    reset_user_state(message.chat.id, user_states)
    show_free_days(message)


def show_free_days(message):
    chat_id = message.chat.id
    all_days = get_free_days()
    free_days = []
    for day in all_days:
        schedule = get_schedule_for_day(day, chat_id)
        normalized = []
        for item in schedule:
            if isinstance(item, dict):
                normalized.append(item)
            else:
                normalized.append({
                    "time": item[0],
                    "status": item[1],
                    "booking_id": item[2] if len(item) > 2 else None,
                    "group_name": item[3] if len(item) > 3 else None
                })
        free_slots = [s for s in normalized if 11 <= int(s["time"][:2]) <= 23 and s["status"] == 0]
        if free_slots:
            free_days.append(day)
    if not free_days:
        main_bot.send_message(chat_id, "На ближайшие дни нет свободных слотов в интервале 11:00–23:00.")
        return
    markup = types.ReplyKeyboardMarkup(resize_keyboard=True)
    row = []
    for i, day in enumerate(free_days, start=1):
        row.append(format_date(day))
        if i % 3 == 0:
            markup.add(*row)
            row = []
    if row:
        markup.add(*row)
    markup.add("На главную")
    user_states[chat_id] = {"step": "waiting_for_day"}
    main_bot.send_message(chat_id, "Свободные дни:", reply_markup=markup)


def parse_date(text: str) -> str:
    ddmm = text[:5]
    try:
        day, month = map(int, ddmm.split("."))
    except Exception:
        try:
            datetime.strptime(text, "%Y-%m-%d")
            return text
        except Exception:
            raise ValueError(f"Некорректный формат даты: {text!r}")
    year = datetime.now().year
    try:
        date_obj = datetime(year, month, day)
    except ValueError as e:
        raise ValueError(f"Некорректная дата: {text!r}") from e
    return date_obj.strftime("%Y-%m-%d")


@main_bot.message_handler(func=lambda m: user_states.get(m.chat.id, {}).get("step") == "waiting_for_day")
def handle_day_selection(message):
    chat_id = message.chat.id
    selected_day = parse_date(message.text)
    schedule = get_schedule_for_day(selected_day, chat_id)
    normalized = []
    for item in schedule:
        if isinstance(item, dict):
            normalized.append(item)
        else:
            normalized.append({
                "time": item[0],
                "status": int(item[1]),
                "booking_id": item[2],
                "group_name": item[3]
            })
    slots = [s for s in normalized if 11 <= int(s["time"][:2]) <= 23]
    today_str = datetime.now().strftime("%Y-%m-%d")
    if selected_day == today_str:
        next_hour = datetime.now().hour + 1
        slots = [s for s in slots if int(s["time"][:2]) >= next_hour]
    is_admin_flag = is_admin(chat_id)
    slots_sorted = sorted(slots, key=lambda x: int(x["time"][:2]))
    text_lines = [f"Расписание на {message.text}:"]
    free_slots = []
    for slot in slots_sorted:
        t = slot["time"]
        if slot["status"] == 0:
            text_lines.append(f"{t} —")
            free_slots.append(t)
        else:
            if is_admin_flag and slot["group_name"]:
                text_lines.append(f"{t} — {slot['group_name']}")
            else:
                text_lines.append(f"{t} — Занято")
    markup = types.ReplyKeyboardMarkup(resize_keyboard=True)
    row = []
    for i, t in enumerate(free_slots, start=1):
        row.append(t)
        if i % 3 == 0:
            markup.add(*row)
            row = []
    if row:
        markup.add(*row)
    markup.add("Выбрать другой день", "На главную")
    user_states[chat_id] = {
        "step": "waiting_for_time",
        "day": selected_day
    }
    main_bot.send_message(chat_id, "\n".join(text_lines), reply_markup=markup)


@main_bot.message_handler(func=lambda msg: msg.text == "Выбрать другой день")
def handle_choose_other_day(message):
    reset_user_state(message.chat.id, user_states)
    user_states[message.chat.id] = {"step": "waiting_for_day"}
    show_free_days(message)


@main_bot.message_handler(func=lambda m: re.match(r"^\d{2}:00$", m.text) and user_states.get(m.chat.id, {}).get("step") == "waiting_for_time")
def handle_time_selection(message):
    chat_id = message.chat.id
    if user_states.get(chat_id, {}).get("step") != "waiting_for_time":
        return
    user_states[chat_id]["time"] = message.text
    user_states[chat_id]["step"] = "waiting_for_hours"
    main_bot.send_message(chat_id, "Сколько часов будет занято?\nУкажите числом.", reply_markup=types.ReplyKeyboardRemove())


@main_bot.message_handler(func=lambda m: user_states.get(m.chat.id, {}).get("step") == "waiting_for_hours")
def handle_hours_input(message):
    chat_id = message.chat.id
    try:
        hours = int(message.text)
    except ValueError:
        return main_bot.send_message(chat_id, "Введите корректное количество часов.")
    if not (1 <= hours <= 8):
        return main_bot.send_message(chat_id, "Максимум можно забронировать 8 часов.")
    start_day = user_states[chat_id]["day"]
    start_time_str = user_states[chat_id]["time"]
    start_hour = int(start_time_str[:2])
    for i in range(hours):
        cur_hour = (start_hour + i) % 24
        cur_date = datetime.strptime(start_day, "%Y-%m-%d") + timedelta(days=(start_hour + i) // 24)
        cur_date_str = cur_date.strftime("%Y-%m-%d")
        day_schedule = get_schedule_for_day(cur_date_str, chat_id)
        normalized = []
        for item in day_schedule:
            if isinstance(item, dict):
                normalized.append(item)
            else:
                normalized.append({
                    "time": item[0],
                    "status": item[1],
                    "booking_id": item[2] if len(item) > 2 else None,
                    "group_name": item[3] if len(item) > 3 else None
                })
        slot = next((s for s in normalized if int(s["time"][:2]) == cur_hour), None)
        if not slot or slot["status"] != 0:
            main_bot.send_message(chat_id, "Этот временной интервал уже занят. Выберите другое время.")
            return show_free_days(message)
    user_states[chat_id]["hours"] = hours
    user_states[chat_id]["step"] = "waiting_for_group_name"
    main_bot.send_message(chat_id, "Введите название группы:", reply_markup=types.ReplyKeyboardRemove())


@main_bot.message_handler(func=lambda m: user_states.get(m.chat.id, {}).get("step") == "waiting_for_group_name")
def handle_group_name_input(message):
    chat_id = message.chat.id
    if not validate_input(message.text, max_length=100):
        return main_bot.send_message(chat_id, "Название группы не должно превышать 100 символов и содержать символы: /, \\, *, \".")
    user_states[chat_id]["group_name"] = message.text
    user_states[chat_id]["step"] = "waiting_for_contact"
    main_bot.send_message(chat_id, "Введите ваш номер телефона, тег в телеграмме или способ связи:")


@main_bot.message_handler(func=lambda m: user_states.get(m.chat.id, {}).get("step") == "waiting_for_contact")
def handle_contact_input(message):
    chat_id = message.chat.id
    if not validate_input(message.text, max_length=100):
        return main_bot.send_message(chat_id, "Контакт не должен превышать 100 символов и содержать символы: /, \\, *, \".")
    user_states[chat_id]["contact"] = message.text
    markup = types.ReplyKeyboardMarkup(resize_keyboard=True)
    markup.add("Запись", "Репетиция", "Другое")
    main_bot.send_message(chat_id, "Выберите тип брони:", reply_markup=markup)
    user_states[chat_id]["step"] = "waiting_for_booking_type"


@main_bot.message_handler(func=lambda m: user_states.get(m.chat.id, {}).get("step") == "waiting_for_booking_type")
def handle_booking_type_selection(message):
    chat_id = message.chat.id
    if message.text not in ("Запись", "Репетиция", "Другое"):
        return main_bot.send_message(chat_id, "Выберите один из вариантов.")
    if message.text == "Другое":
        user_states[chat_id]["step"] = "waiting_for_custom_booking_type"
        return main_bot.send_message(chat_id, "Опишите, чем планируете заниматься:")
    user_states[chat_id]["booking_type"] = message.text
    ask_for_comment(chat_id)


@main_bot.message_handler(func=lambda m: user_states.get(m.chat.id, {}).get("step") == "waiting_for_custom_booking_type")
def handle_custom_booking_type(message):
    chat_id = message.chat.id
    user_states[chat_id]["booking_type"] = message.text
    ask_for_comment(chat_id)


def ask_for_comment(chat_id):
    markup = types.ReplyKeyboardMarkup(resize_keyboard=True)
    markup.add("Прайс", "ОК")
    main_bot.send_message(chat_id, "Если вам необходимы какие-либо дополнительные услуги из нашего прайса, пожалуйста, укажите их в комментарии.\n\nЕсли доп.услуги не требуются, нажмите 'Ок'.:", reply_markup=markup)
    user_states[chat_id]["step"] = "waiting_for_comment"


@main_bot.message_handler(func=lambda msg: isinstance(user_states.get(msg.chat.id), dict) and user_states[msg.chat.id].get("step") == "waiting_for_comment" and msg.text == "Прайс")
def show_price_list_during_booking(message):
    chat_id = message.chat.id
    try:
        with open("price.txt", "r", encoding="utf-8") as file:
            price_list = file.read().strip()
    except FileNotFoundError:
        price_list = "Информация о прайсе временно недоступна."
    main_bot.send_message(chat_id, price_list)


@main_bot.message_handler(func=lambda m: user_states.get(m.chat.id, {}).get("step") == "waiting_for_comment")
def handle_comment_input(message):
    chat_id = message.chat.id
    if message.text == "Прайс":
        return send_price(chat_id)
    comment = "" if message.text == "ОК" else message.text
    selected_day = user_states[chat_id]["day"]
    selected_time = user_states[chat_id]["time"]
    hours = user_states[chat_id]["hours"]
    group_name = user_states[chat_id]["group_name"]
    booking_type = user_states[chat_id]["booking_type"]
    contact_info = user_states[chat_id]["contact"]
    start_dt = datetime.strptime(f"{selected_day} {selected_time}", "%Y-%m-%d %H:%M")
    end_dt = start_dt + timedelta(hours=hours)
    start_text = start_dt.strftime("%H:%M")
    end_text  = end_dt.strftime("%H:%M")
    date_text = format_date(selected_day)
    if message.from_user.username:
        mention_raw = f"@{message.from_user.username}"
    elif contact_info.startswith('@'):
        mention_raw = contact_info
    elif contact_info.replace('+', '').isdigit():
        mention_raw = f"[{message.from_user.first_name}](tel:{contact_info})"
    else:
        mention_raw = f"{message.from_user.first_name} (ID: {message.from_user.id})"
    from lib.utils import escape_markdown
    mention_safe = escape_markdown(mention_raw)
    safe_group = escape_markdown(group_name or "")
    safe_type = escape_markdown(booking_type or "")
    safe_comment = escape_markdown(comment or "—")
    safe_contact = escape_markdown(contact_info or "—")
    booking_id = book_slots(
        selected_day,
        selected_time,
        hours,
        chat_id,
        group_name,
        booking_type,
        comment,
        contact_info,
        mention_safe
    )
    markup = types.ReplyKeyboardMarkup(resize_keyboard=True)
    markup.add("Забронировать другое время", "На главную")
    main_bot.send_message(
        chat_id,
        f"Спасибо! 👍\n"
        f"Вы забронировали {hours} {get_hour_word(hours)} "
        f"с {start_text} по {end_text} {format_date(selected_day)}\n"
        f"Группа: {group_name}",
        reply_markup=markup,
    )
    note = (
        "🔔 Новая бронь!\n"
        f"Дата: {date_text}\n"
        f"Время: {start_text}-{end_text}\n"
        f"Группа: {safe_group}\n"
        f"Тип: {safe_type}\n"
        f"Комментарий: {safe_comment}\n"
        f"Контакт: {safe_contact}\n"
        f"Создатель: {mention_safe}"
    )
    inline_kb = create_confirmation_keyboard(selected_day, selected_time, booking_id)
    for admin_id in ADMIN_IDS:
        try:
            admin_bot.send_message(admin_id, note, reply_markup=inline_kb, parse_mode="Markdown")
        except Exception as e:
            try:
                logger.error(f"Не удалось отправить уведомление админу {admin_id}: {e}")
            except Exception:
                pass
    reset_user_state(chat_id, user_states)


@main_bot.message_handler(func=lambda msg: msg.text == "Забронировать другое время")
def book_another_time(message):
    reset_user_state(message.chat.id, user_states)
    show_free_days(message)


@main_bot.message_handler(func=lambda msg: msg.text == "Вернуться на главную")
def return_to_main_menu(message):
    reset_user_state(message.chat.id, user_states)
    show_menu(message)


### Отмена брони ###

def send_date_selection_keyboard(chat_id: int, dates, bot):
    iso_dates = [(d.strftime("%Y-%m-%d") if hasattr(d, "strftime") else str(d)) for d in dates]
    btn_map = {}
    for iso in iso_dates:
        try:
            label = format_date(iso)
        except Exception:
            try:
                label = datetime.strptime(iso, "%Y-%m-%d").strftime("%d.%m")
            except Exception:
                label = iso
        btn_map[label] = iso
    raw_state = user_states.get(chat_id)
    state = raw_state if isinstance(raw_state, dict) else {}
    state["date_btn_map"] = btn_map
    state["shown_days"] = iso_dates
    user_states[chat_id] = state
    markup = types.ReplyKeyboardMarkup(resize_keyboard=True)
    for label in btn_map.keys():
        markup.add(types.KeyboardButton(label))
    markup.add(types.KeyboardButton("На главную"))
    bot.send_message(chat_id, "Выберите дату:", reply_markup=markup)


def _get_user_booking_ids(user_id: int) -> list[int]:
    conn = get_connection()
    try:
        cur = conn.cursor()
        cur.execute(
            "SELECT DISTINCT booking_id FROM slots "
            "WHERE user_id = %s AND booking_id IS NOT NULL AND status IN (1,2)",
            (user_id,),
        )
        rows = cur.fetchall()
    finally:
        conn.close()
    all_ids = []
    for r in rows:
        v = r[0] if isinstance(r, (list, tuple)) else r
        if v is None:
            continue
        try:
            all_ids.append(int(v))
        except Exception:
            try:
                all_ids.append(int(str(v).strip()))
            except Exception:
                continue
    now = datetime.now()
    cutoff = now + timedelta(hours=24)
    filtered_ids = []
    for bid in all_ids:
        info = None
        try:
            info = get_booking_info_by_id(bid)
        except Exception:
            info = None
        if not info:
            continue
        try:
            start_date = datetime.strptime(info["date"], "%d.%m.%Y").date()
            start_dt = datetime.combine(
                start_date,
                datetime.strptime(info["start_time"], "%H:%M").time()
            )
            if start_dt <= now:
                continue
            if start_dt <= cutoff:
                continue
            end_str = (info.get("end_time") or "").strip()
            try:
                parts = end_str.split()
                if len(parts) == 1:
                    end_dt = datetime.combine(
                        start_date,
                        datetime.strptime(parts[0], "%H:%M").time()
                    )
                else:
                    end_time_only, end_date_human = parts
                    end_dt = datetime.combine(
                        datetime.strptime(end_date_human, "%d.%m.%Y").date(),
                        datetime.strptime(end_time_only, "%H:%M").time()
                    )
            except Exception:
                end_dt = start_dt + timedelta(hours=1)
            if end_dt <= now:
                continue
            filtered_ids.append(bid)
        except Exception:
            continue
    return filtered_ids


@main_bot.message_handler(func=lambda msg: msg.text == "Отменить бронь")
def handle_cancel_booking(message):
    chat_id = message.chat.id
    booking_ids = _get_user_booking_ids(chat_id)
    if not booking_ids:
        main_bot.send_message(chat_id, "У вас нет активных будущих броней, доступных для отмены.")
        show_menu(message)
        return
    items = []
    for bid in booking_ids:
        try:
            info = get_booking_info_by_id(bid)
        except Exception:
            info = None
        if not info:
            continue
        start_time = (info.get("start_time") or "").strip()
        end_time = (info.get("end_time") or "").strip()
        group_name = (info.get("group_name") or "").strip()
        start_date_human = (info.get("date") or "").strip()
        try:
            dt = datetime.strptime(start_date_human, "%d.%m.%Y").date()
            iso_date = dt.strftime("%Y-%m-%d")
            weekday_label = format_date(iso_date).split()[-1]
        except Exception:
            weekday_label = ""
        try:
            ddmm = datetime.strptime(start_date_human, "%d.%m.%Y").strftime("%d.%m")
        except Exception:
            ddmm = start_date_human[:5] if len(start_date_human) >= 5 else start_date_human
        date_compact = f"{ddmm} {weekday_label}".strip()
        label = f"{date_compact} {start_time}-{end_time}  -  {group_name}".strip()
        items.append((label, bid))
    if not items:
        main_bot.send_message(chat_id, "У вас нет активных будущих броней, доступных для отмены.")
        show_menu(message)
        return
    markup = types.ReplyKeyboardMarkup(resize_keyboard=True, row_width=3)
    row = []
    for i, (label, _bid) in enumerate(items, start=1):
        row.append(types.KeyboardButton(label))
        if i % 3 == 0:
            markup.add(*row)
            row = []
    if row:
        markup.add(*row)
    markup.add(types.KeyboardButton("На главную"))
    user_states[chat_id] = {
        "step": "choose_booking_to_cancel_by_id",
        "booking_label_map": {label: bid for label, bid in items},
    }
    main_bot.send_message(chat_id, "Выберите бронь для отмены:", reply_markup=markup)


@main_bot.message_handler(func=lambda msg: isinstance(user_states.get(msg.chat.id), dict) and user_states[msg.chat.id].get("step") == "choose_booking_to_cancel_by_id")
def handle_choose_booking_to_cancel_by_id(message):
    chat_id = message.chat.id
    text = (message.text or "").strip()
    if text == "На главную":
        reset_user_state(chat_id, user_states)
        show_menu(message)
        return
    state = user_states.get(chat_id, {}) or {}
    label_map = state.get("booking_label_map", {})
    booking_id = label_map.get(text)
    if not booking_id:
        markup = types.ReplyKeyboardMarkup(resize_keyboard=True, row_width=2)
        for label in label_map.keys():
            markup.add(types.KeyboardButton(label))
        markup.add(types.KeyboardButton("На главную"))
        main_bot.send_message(chat_id, "Пожалуйста, выберите бронь из списка:", reply_markup=markup)
        return
    info = get_booking_info_by_id(booking_id)
    if not info:
        main_bot.send_message(chat_id, "Не удалось получить информацию о брони. Попробуйте позже.")
        reset_user_state(chat_id, user_states)
        show_menu(message)
        return
    group_name = info.get("group_name") or "—"
    date_str = info.get("date") or "—"
    start_time = info.get("start_time") or "—"
    end_time = info.get("end_time") or "—"
    state["step"] = "confirm_user_cancellation"
    state["pending_cancel_booking_id"] = booking_id
    user_states[chat_id] = state
    confirm_kb = types.ReplyKeyboardMarkup(resize_keyboard=True, row_width=2)
    confirm_kb.add(
        types.KeyboardButton("Да, отменить"),
        types.KeyboardButton("Нет, вернуться в меню"),
    )
    main_bot.send_message(
        chat_id,
        f"Вы уверены, что хотите отменить бронь?\n"
        f"Дата: {date_str}\n"
        f"Время: {start_time}–{end_time}\n"
        f"Группа: {group_name}",
        reply_markup=confirm_kb,
    )


@main_bot.message_handler(func=lambda m: isinstance(user_states.get(m.chat.id), dict) and user_states[m.chat.id].get("step") == "confirm_user_cancellation")
def handle_user_confirm_cancellation(message):
    chat_id = message.chat.id
    text = (message.text or "").strip()
    state = user_states.get(chat_id, {}) or {}
    booking_id = state.get("pending_cancel_booking_id")
    if text == "Нет, вернуться в меню":
        reset_user_state(chat_id, user_states)
        show_menu(message)
        return
    if text != "Да, отменить":
        confirm_kb = types.ReplyKeyboardMarkup(resize_keyboard=True, row_width=2)
        confirm_kb.add(
            types.KeyboardButton("Да, отменить"),
            types.KeyboardButton("Нет, вернуться в меню"),
        )
        main_bot.send_message(chat_id, "Пожалуйста, подтвердите отмену или вернитесь в меню.", reply_markup=confirm_kb)
        return
    info = get_booking_info_by_id(booking_id)
    if not info:
        main_bot.send_message(chat_id, "Не удалось получить информацию о брони. Попробуйте позже.")
        reset_user_state(chat_id, user_states)
        show_menu(message)
        return
    group_name = (info.get("group_name") or "").strip()
    date_str = (info.get("date") or "").strip()
    start_time = (info.get("start_time") or "").strip()
    end_time = (info.get("end_time") or "").strip()
    inline_kb = InlineKeyboardMarkup()
    inline_kb.add(
        InlineKeyboardButton("🚫 Подтвердить отмену", callback_data=f"cancel_booking_id:{booking_id}")
    )
    note = (
        "🚫 Запрос на отмену брони!\n"
        f"_Дата:_ *{date_str}*\n"
        f"_Время:_ *{start_time}–{end_time}*\n"
        f"_Группа:_ *{escape_markdown(group_name)}*"
    )
    for admin_id in ADMIN_IDS:
        try:
            admin_bot.send_message(admin_id, note, parse_mode="Markdown", reply_markup=inline_kb)
        except Exception as e:
            logger.error(f"Не удалось отправить запрос на отмену админу {admin_id}: {e}")
    main_bot.send_message(
        chat_id,
        "Запрос на отмену брони отправлен администратору. Пожалуйста, ожидайте подтверждения.",
        reply_markup=types.ReplyKeyboardRemove(),
    )
    reset_user_state(chat_id, user_states)
    show_menu(message)


def send_cancellation_options(chat_id, bookings):
    markup = types.ReplyKeyboardMarkup(resize_keyboard=True)
    for booking in bookings:
        start_time = booking["start_time"].strftime("%H:%M")
        end_time = booking["end_time"].strftime("%H:%M")
        group_name = booking["group_name"]
        markup.add(types.KeyboardButton(f"{start_time}–{end_time}, {group_name}"))
    markup.row(
        types.KeyboardButton("Выбрать другой день"),
        types.KeyboardButton("На главную"),
    )
    main_bot.send_message(chat_id, "Выберите бронь для отмены:", reply_markup=markup)


@main_bot.message_handler(
    func=lambda msg: isinstance(user_states.get(msg.chat.id), dict)
    and user_states[msg.chat.id].get("step") == "choose_booking_for_cancellation"
)
def handle_user_choose_booking_for_cancellation(message):
    chat_id = message.chat.id
    if message.text == "На главную":
        return_to_main_menu(message)
        return
    if message.text == "Выбрать другой день":
        state = user_states.get(chat_id, {}) or {}
        state["step"] = "choose_date_for_cancellation"
        user_states[chat_id] = state
        send_date_selection_keyboard(chat_id, state.get("valid_dates", []), main_bot)
        return
    selected_text = message.text.strip()
    state = user_states.get(chat_id, {}) or {}
    bookings = state.get("bookings", [])
    found = False
    index = -1
    for i, booking in enumerate(bookings):
        start_time = booking["start_time"].strftime("%H:%M")
        end_time = booking["end_time"].strftime("%H:%M")
        group_name = booking["group_name"]
        button_text = f"{start_time}–{end_time}, {group_name}"
        if selected_text == button_text:
            found = True
            index = i
            break
    if not found:
        main_bot.send_message(chat_id, "Выберите одну из предложенных броней.")
        send_cancellation_options(chat_id, bookings)
        return
    selected_booking = bookings[index]
    booking_ids = selected_booking["ids"]
    start_time = selected_booking["start_time"].strftime("%H:%M")
    end_time = selected_booking["end_time"].strftime("%H:%M")
    date_str = selected_booking["date_str"]
    group_name = selected_booking["group_name"]
    try:
        formatted_date = format_date(date_str).replace(" ", ".")[:-3]
    except ValueError:
        formatted_date = date_str
    if message.from_user.username:
        mention = f"@{message.from_user.username}"
    elif contact_info.startswith('@'):
        mention = contact_info
    elif contact_info.replace('+', '').isdigit():
        mention = f"[{message.from_user.first_name}](tel:{contact_info})"
    else:
        mention = f"{message.from_user.first_name} (ID: {message.from_user.id})"
    note = (
        f"🚫 *Запрос на отмену брони!*\n"
        f"_Дата:_ *{date_str}*\n"
        f"_Время:_ *{start_time}–{end_time}*\n"
        f"_Группа:_ *{group_name}*\n"
        f"_Создатель:_ *{mention}*"
    )
    for admin_id in ADMIN_IDS:
        try:
            admin_bot.send_message(
                admin_id,
                note,
                parse_mode='Markdown',
                reply_markup=create_cancellation_keyboard(date_str, start_time, booking_ids)
            )
        except Exception as e:
            print(f"[Error] Can't send cancellation request to admin {admin_id}: {e}")
    main_bot.send_message(
        chat_id,
        "Запрос на отмену брони отправлен администратору. Пожалуйста, ожидайте подтверждения.",
    )
    show_menu(message)


### Оформление подписки ###

def _get_already_subscribed_times(user_id: int, date_iso: str) -> list[str]:
    conn = get_connection()
    try:
        cur = conn.cursor()
        user_str = str(user_id)
        cur.execute(
            """
            SELECT LEFT(time,5) AS t
            FROM slots
            WHERE date = %s
              AND status IN (1, 2)
              AND LEFT(time,5) >= '11:00'
              AND LEFT(time,5) <= '23:00'
              AND (
                    subscribed_users = %s
                 OR subscribed_users LIKE %s
                 OR subscribed_users LIKE %s
                 OR subscribed_users LIKE %s
              )
            ORDER BY t
            """,
            (
                date_iso,
                user_str,
                f"{user_str},%",
                f"%,{user_str},%",
                f"%,{user_str}"
            ),
        )
        return [row[0] for row in cur.fetchall()]
    finally:
        conn.close()


@main_bot.message_handler(func=lambda msg: msg.text == "Быть в курсе, если освободится время")
def subscribe_to_free_slots(message):
    chat_id = message.chat.id
    reset_user_state(chat_id, user_states)
    days = get_booked_days_filtered()
    if not days:
        main_bot.send_message(chat_id, "Нет занятых дней в ближайшее время.")
        return
    iso_days = [(d.strftime("%Y-%m-%d") if hasattr(d, "strftime") else str(d)) for d in days]
    btn_map = {format_date(d): d for d in iso_days}
    user_states[chat_id] = {
        "step": "waiting_for_subscribe_day",
        "date_btn_map": btn_map,
    }
    kb = types.ReplyKeyboardMarkup(resize_keyboard=True)
    labels = list(btn_map.keys())
    row = []
    for i, lbl in enumerate(labels, start=1):
        row.append(types.KeyboardButton(lbl))
        if i % 3 == 0:
            kb.add(*row)
            row = []
    if row:
        kb.add(*row)
    kb.add(types.KeyboardButton("На главную"))
    main_bot.send_message(chat_id, "Выберите день:", reply_markup=kb)


@main_bot.message_handler(func=lambda msg: user_states.get(msg.chat.id, {}).get("step") == "waiting_for_subscribe_day")
def handle_subscribe_day_selection(message):
    chat_id = message.chat.id
    text = (message.text or "").strip()
    if text == "На главную":
        reset_user_state(chat_id, user_states)
        show_menu(message)
        return
    state = user_states.get(chat_id, {}) or {}
    btn_map = state.get("date_btn_map") or {}
    date_iso = btn_map.get(text)
    if not date_iso:
        main_bot.send_message(chat_id, "Пожалуйста, выберите дату из списка.")
        return
    times_all = get_busy_times_for_day(date_iso)
    if not times_all:
        main_bot.send_message(chat_id, "В этот день нет занятых слотов с 11:00 до 23:00.")
        subscribe_to_free_slots(message)
        return
    times_already = _get_already_subscribed_times(chat_id, date_iso)
    already_set = set(times_already)
    times = [t for t in times_all if t not in already_set]
    if not times:
        main_bot.send_message(chat_id, "Вы уже подписаны на все занятые слоты этого дня в интервале 11:00–23:00.")
        subscribe_to_free_slots(message)
        return
    kb = types.ReplyKeyboardMarkup(resize_keyboard=True)
    row = []
    for i, t in enumerate(times, start=1):
        row.append(types.KeyboardButton(t))
        if i % 3 == 0:
            kb.add(*row)
            row = []
    if row:
        kb.add(*row)
    kb.add(types.KeyboardButton("Другой день"))
    kb.add(types.KeyboardButton("На главную"))
    user_states[chat_id] = {
        "step": "waiting_for_subscribe_time",
        "subscribe_day": date_iso,
    }
    main_bot.send_message(
        chat_id,
        f"Выбран день: {format_date(date_iso)}\nВыберите занятый слот (11:00–23:00):",
        reply_markup=kb,
    )


@main_bot.message_handler(func=lambda msg: user_states.get(msg.chat.id, {}).get("step") == "waiting_for_subscribe_time")
def handle_subscribe_time_selection(message):
    chat_id = message.chat.id
    text = (message.text or "").strip()
    state = user_states.get(chat_id, {}) or {}
    date_iso = state.get("subscribe_day")
    if text == "Другой день":
        subscribe_to_free_slots(message)
        return
    if text == "На главную":
        reset_user_state(chat_id, user_states)
        show_menu(message)
        return
    if not date_iso:
        main_bot.send_message(chat_id, "Сессия устарела. Выберите день заново.")
        subscribe_to_free_slots(message)
        return
    if not re.match(r"^\d{2}:\d{2}$", text):
        times_all = get_busy_times_for_day(date_iso)
        times_already = _get_already_subscribed_times(chat_id, date_iso)
        times = [t for t in times_all if t not in set(times_already)]
        if not times:
            main_bot.send_message(chat_id, "Нет доступных слотов для подписки в этот день.")
            subscribe_to_free_slots(message)
            return
        kb = types.ReplyKeyboardMarkup(resize_keyboard=True)
        row = []
        for i, t in enumerate(times, start=1):
            row.append(types.KeyboardButton(t))
            if i % 3 == 0:
                kb.add(*row)
                row = []
        if row:
            kb.add(*row)
        kb.add(types.KeyboardButton("Другой день"))
        kb.add(types.KeyboardButton("На главную"))
        main_bot.send_message(chat_id, "Пожалуйста, выберите время из списка (формат HH:MM).", reply_markup=kb)
        return
    conn = get_connection()
    try:
        cur = conn.cursor()
        cur.execute(
            """
            SELECT status, subscribed_users
            FROM slots
            WHERE date = %s
              AND LEFT(time,5) = %s
              AND LEFT(time,5) >= '11:00'
              AND LEFT(time,5) <= '23:00'
            LIMIT 1
            """,
            (date_iso, text),
        )
        row = cur.fetchone()
    finally:
        conn.close()
    if not row:
        times_all = get_busy_times_for_day(date_iso)
        times_already = _get_already_subscribed_times(chat_id, date_iso)
        times = [t for t in times_all if t not in set(times_already)]
        if not times:
            main_bot.send_message(chat_id, "В этот день больше нет доступных слотов для подписки.")
            subscribe_to_free_slots(message)
            return
        kb = types.ReplyKeyboardMarkup(resize_keyboard=True)
        row_btn = []
        for i, t in enumerate(times, start=1):
            row_btn.append(types.KeyboardButton(t))
            if i % 3 == 0:
                kb.add(*row_btn)
                row_btn = []
        if row_btn:
            kb.add(*row_btn)
        kb.add(types.KeyboardButton("Другой день"))
        kb.add(types.KeyboardButton("На главную"))
        main_bot.send_message(chat_id, f"Выберите время ({format_date(date_iso)}):", reply_markup=kb)
        return
    status_val = int((row[0] or 0))
    if status_val == 0:
        main_bot.send_message(chat_id, "Этот слот уже свободен. Выберите другой занятый слот.")
        times_all = get_busy_times_for_day(date_iso)
        times_already = _get_already_subscribed_times(chat_id, date_iso)
        times = [t for t in times_all if t not in set(times_already)]
        if not times:
            main_bot.send_message(chat_id, "В этот день больше нет доступных слотов для подписки.")
            subscribe_to_free_slots(message)
            return
        kb = types.ReplyKeyboardMarkup(resize_keyboard=True)
        row_btn = []
        for i, t in enumerate(times, start=1):
            row_btn.append(types.KeyboardButton(t))
            if i % 3 == 0:
                kb.add(*row_btn)
                row_btn = []
        if row_btn:
            kb.add(*row_btn)
        kb.add(types.KeyboardButton("Другой день"))
        kb.add(types.KeyboardButton("На главную"))
        main_bot.send_message(chat_id, f"Выберите время ({format_date(date_iso)}):", reply_markup=kb)
        return
    subs_str = str(row[1] or "")
    uid_str = str(chat_id)
    already = (
        subs_str == uid_str
        or subs_str.startswith(uid_str + ",")
        or subs_str.endswith("," + uid_str)
        or ("," + uid_str + ",") in subs_str
    )
    if already:
        main_bot.send_message(chat_id, "Подписка на этот слот уже оформлена. Выберите другой занятый слот.")
        times_all = get_busy_times_for_day(date_iso)
        times_already = _get_already_subscribed_times(chat_id, date_iso)
        times = [t for t in times_all if t not in set(times_already)]
        if not times:
            main_bot.send_message(chat_id, "Нет доступных слотов для подписки в этот день.")
            subscribe_to_free_slots(message)
            return

        kb = types.ReplyKeyboardMarkup(resize_keyboard=True)
        row_btn = []
        for i, t in enumerate(times, start=1):
            row_btn.append(types.KeyboardButton(t))
            if i % 3 == 0:
                kb.add(*row_btn)
                row_btn = []
        if row_btn:
            kb.add(*row_btn)
        kb.add(types.KeyboardButton("Другой день"))
        kb.add(types.KeyboardButton("На главную"))
        main_bot.send_message(chat_id, f"Выберите время ({format_date(date_iso)}):", reply_markup=kb)
        return
    add_subscriber_to_slot(date_iso, text, chat_id)
    kb = types.ReplyKeyboardMarkup(resize_keyboard=True)
    kb.add(types.KeyboardButton("Оповестить про другой слот"))
    kb.add(types.KeyboardButton("На главную"))
    main_bot.send_message(chat_id, "Готово! Сообщим, если это время освободится.", reply_markup=kb)
    user_states[chat_id] = {
        "step": "waiting_for_subscribe_post_action",
        "subscribe_day": date_iso,
    }


@main_bot.message_handler(func=lambda msg: user_states.get(msg.chat.id, {}).get("step") == "waiting_for_subscribe_post_action")
def handle_subscribe_post_action(message):
    chat_id = message.chat.id
    text = (message.text or "").strip()
    if text in ("Оповестить про другой слот", "Другой день"):
        subscribe_to_free_slots(message)
        return
    if text == "На главную":
        reset_user_state(chat_id, user_states)
        show_menu(message)
        return
    kb = types.ReplyKeyboardMarkup(resize_keyboard=True)
    kb.add(types.KeyboardButton("Оповестить про другой слот"))
    kb.add(types.KeyboardButton("На главную"))
    main_bot.send_message(chat_id, "Пожалуйста, выберите действие:", reply_markup=kb)


### Прайс ###

@main_bot.message_handler(func=lambda msg: msg.text == "Посмотреть прайс")
def show_price_list(message):
    try:
        with open("price.txt", "r", encoding="utf-8") as file:
            price_list = file.read().strip()
    except FileNotFoundError:
        price_list = "Информация о прайсе временно недоступна."
    main_bot.send_message(message.chat.id, price_list)
    reset_user_state(message.chat.id, user_states)
    show_menu(message)


### Расписание ###

@main_bot.message_handler(func=lambda msg: msg.text == "Посмотреть расписание")
def view_schedule(message):
    main_bot.send_message(message.chat.id, "Рисуем...")
    path = None
    try:
        path = create_schedule_grid_image(message.chat.id)
        with open(path, "rb") as f:
            main_bot.send_photo(message.chat.id, f)
    finally:
        if path:
            try:
                os.remove(path)
            except Exception:
                pass
    reset_user_state(message.chat.id, user_states)
    show_menu(message)


def main():
    init_db()
    main_bot.polling(none_stop=True)


if __name__ == "__main__":
    main()