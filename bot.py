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
from lib.schedule_tasks import (
    add_subscriber_to_slot,
    get_booked_days_filtered,
    get_free_days,
    get_grouped_bookings_for_cancellation,
    get_schedule_for_day,
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


@main_bot.message_handler(func=lambda m: re.match(r"^\d{2}:00$", m.text))
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
    main_bot.send_message(chat_id, "Добавьте комментарий (или нажмите ОК):", reply_markup=markup)
    user_states[chat_id]["step"] = "waiting_for_comment"


def show_comment_prompt(chat_id: int):
    keyboard = types.ReplyKeyboardMarkup(resize_keyboard=True, row_width=2)
    keyboard.row(types.KeyboardButton("Прайс"), types.KeyboardButton("Ок"))
    main_bot.send_message(
        chat_id,
        "Если вам необходимы какие-либо дополнительные услуги из нашего прайса, "
        "пожалуйста, укажите их в комментарии.\n\nЕсли доп.услуги не требуются, "
        "нажмите 'Ок'.",
        reply_markup=keyboard,
    )


@main_bot.message_handler(func=lambda msg: isinstance(user_states.get(msg.chat.id), dict) and user_states[msg.chat.id].get("step") == "waiting_for_comment" and msg.text == "Прайс")
def show_price_list_during_booking(message):
    chat_id = message.chat.id
    try:
        with open("price.txt", "r", encoding="utf-8") as file:
            price_list = file.read().strip()
    except FileNotFoundError:
        price_list = "Информация о прайсе временно недоступна."
    main_bot.send_message(chat_id, price_list)
    show_comment_prompt(chat_id)


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
    end_time = f"{int(selected_time[:2]) + hours:02d}:00"
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
        f"с {selected_time} по {end_time} {format_date(selected_day)}\n"
        f"Группа: {group_name}",
        reply_markup=markup,
    )
    note = (
        "🔔 Новая бронь!\n"
        f"Дата: {selected_day}\n"
        f"Время: {selected_time}-{end_time}\n"
        f"Группа: {safe_group}\n"
        f"Тип: {safe_type}\n"
        f"Комментарий: {safe_comment}\n"
        f"Контакт: {safe_contact}\n"
        f"Создатель: {mention_safe}"
    )
    inline_kb = types.InlineKeyboardMarkup()
    inline_kb.add(
        types.InlineKeyboardButton("Подтвердить", callback_data=f"confirm:{booking_id}"),
        types.InlineKeyboardButton("Отклонить", callback_data=f"reject:{booking_id}"),
    )
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


@main_bot.message_handler(func=lambda msg: msg.text == "Отменить бронь")
def handle_cancel_booking(message):
    chat_id = message.chat.id
    conn = get_connection()
    try:
        cur = conn.cursor()
        today = datetime.now().strftime("%Y-%m-%d")
        cur.execute(
            "SELECT DISTINCT date FROM slots "
            "WHERE status IN (1, 2) AND user_id = %s AND date >= %s "
            "ORDER BY date",
            (chat_id, today),
        )
        all_dates = [row[0] for row in cur.fetchall()]
    finally:
        conn.close()
    valid_dates = []
    for date_str in all_dates:
        bookings = get_grouped_bookings_for_cancellation(date_str, chat_id)
        if bookings:
            valid_dates.append(date_str)
    if not valid_dates:
        main_bot.send_message(chat_id, "У вас нет активных броней.")
        show_menu(message)
        return
    state = {"step": "choose_date_for_cancellation", "valid_dates": valid_dates}
    iso_map = {format_date(d): d for d in valid_dates}
    state["date_btn_map_cancel"] = iso_map
    user_states[chat_id] = state
    markup = types.ReplyKeyboardMarkup(resize_keyboard=True)
    for label in iso_map.keys():
        markup.add(types.KeyboardButton(label))
    markup.add(types.KeyboardButton("На главную"))
    main_bot.send_message(chat_id, "Выберите дату:", reply_markup=markup)


@main_bot.message_handler(
    func=lambda msg: isinstance(user_states.get(msg.chat.id), dict)
    and user_states[msg.chat.id].get("step") == "choose_date_for_cancellation"
)
def handle_date_chosen_for_cancellation(message):
    chat_id = message.chat.id
    if message.text == "На главную":
        return_to_main_menu(message)
        return
    state = user_states.get(chat_id, {}) or {}
    iso_map = state.get("date_btn_map_cancel", {})
    selected_date = iso_map.get(message.text.strip())
    if not selected_date:
        main_bot.send_message(chat_id, "Выберите одну из предложенных дат.")
        markup = types.ReplyKeyboardMarkup(resize_keyboard=True)
        for label in iso_map.keys():
            markup.add(types.KeyboardButton(label))
        markup.add(types.KeyboardButton("На главную"))
        main_bot.send_message(chat_id, "Выберите дату:", reply_markup=markup)
        return
    valid_dates = [d if isinstance(d, str) else d.strftime("%Y-%m-%d") for d in state.get("valid_dates", [])]
    if selected_date not in valid_dates:
        main_bot.send_message(chat_id, "Выберите одну из предложенных дат.")
        return
    bookings = get_grouped_bookings_for_cancellation(selected_date, chat_id)
    now = datetime.now()
    deadline = now + timedelta(hours=24)
    filtered_bookings = []
    for booking in bookings:
        booking_start = datetime.strptime(
            f"{booking['date_str']} {booking['start_time'].strftime('%H:%M')}",
            "%Y-%m-%d %H:%M",
        )
        if booking_start > deadline:
            filtered_bookings.append(booking)
    if not filtered_bookings:
        main_bot.send_message(
            chat_id,
            "У вас нет броней, доступных для отмены в этот день.\n"
            "Отмена возможна только более чем за 24 часа до начала брони.\n\n"
            "Пожалуйста, свяжитесь с админом: @cyberokolade",
        )
        send_date_selection_keyboard(chat_id, state.get("valid_dates", []), main_bot)
        state["step"] = "choose_date_for_cancellation"
        user_states[chat_id] = state
        return
    state.update(
        {
            "step": "choose_booking_for_cancellation",
            "selected_date": selected_date,
            "bookings": filtered_bookings,
        }
    )
    user_states[chat_id] = state
    send_cancellation_options(chat_id, filtered_bookings)


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

@main_bot.message_handler(func=lambda msg: msg.text == "Быть в курсе, если освободится время")
def subscribe_to_free_slots(message):
    reset_user_state(message.chat.id, user_states)
    booked_days = get_booked_days_filtered()
    if not booked_days:
        main_bot.send_message(message.chat.id, "Нет забронированных дней.")
        return
    iso_days = [(d.strftime("%Y-%m-%d") if hasattr(d, "strftime") else str(d)) for d in booked_days]
    iso_map = {}
    for iso in iso_days:
        iso_map[format_date(iso)] = iso
    state = user_states.get(message.chat.id, {})
    if not isinstance(state, dict):
        state = {}
    state["date_btn_map_subscribe"] = iso_map
    state["step"] = "waiting_for_subscribe_day"
    user_states[message.chat.id] = state
    keyboard = types.ReplyKeyboardMarkup(resize_keyboard=True)
    for label in iso_map.keys():
        keyboard.add(types.KeyboardButton(label))
    keyboard.add(types.KeyboardButton("На главную"))
    main_bot.send_message(message.chat.id, "Выберите день:", reply_markup=keyboard)


@main_bot.message_handler(
    func=lambda msg: isinstance(user_states.get(msg.chat.id), dict)
    and user_states[msg.chat.id].get("step") == "waiting_for_subscribe_day"
)
def handle_subscribe_day_selection(message):
    if message.text == "На главную":
        return_to_main_menu(message)
        return
    state = user_states.get(message.chat.id, {}) or {}
    btn_map = state.get("date_btn_map_subscribe") or state.get("date_btn_map") or {}
    selected_day = btn_map.get(message.text.strip())
    if not selected_day:
        main_bot.send_message(message.chat.id, "Выберите дату из списка.")
        subscribe_to_free_slots(message)
        return
    chat_id = message.chat.id
    conn = get_connection()
    try:
        cur = conn.cursor()
        current_date = datetime.now().strftime("%Y-%m-%d")
        cur.execute(
            "SELECT time, subscribed_users "
            "FROM slots "
            "WHERE date = %s AND status IN (1, 2) AND date >= %s",
            (selected_day, current_date),
        )
        rows = cur.fetchall()
    finally:
        conn.close()
    if not rows:
        main_bot.send_message(message.chat.id, "В этот день нет подходящих слотов.")
        return
    available_times = []
    for time_str, subs in rows:
        subs_list = subs.split(",") if subs else []
        if str(chat_id) not in subs_list:
            available_times.append(time_str)
    if not available_times:
        main_bot.send_message(
            message.chat.id, "Вы уже подписаны на все доступные слоты этого дня."
        )
        return
    keyboard = types.ReplyKeyboardMarkup(resize_keyboard=True, row_width=3)
    keyboard.add(*[types.KeyboardButton(t) for t in available_times])
    keyboard.add(types.KeyboardButton("Выбрать другой день"))
    main_bot.send_message(
        message.chat.id,
        "Выберите время, на которое хотите подписаться:",
        reply_markup=keyboard,
    )
    state["step"] = "waiting_for_subscribe_time"
    state["subscribe_day"] = selected_day
    user_states[message.chat.id] = state


@main_bot.message_handler(
    func=lambda msg: isinstance(user_states.get(msg.chat.id), dict)
    and user_states[msg.chat.id].get("step") == "waiting_for_subscribe_time"
)
def handle_subscribe_time_selection(message):
    chat_id = message.chat.id
    state = user_states.get(chat_id, {}) or {}
    selected_day = state.get("subscribe_day")
    if message.text == "Выбрать другой день":
        state["step"] = "waiting_for_subscribe_day"
        user_states[chat_id] = state
        subscribe_to_free_slots(message)
        return
    if message.text == "На главную":
        return_to_main_menu(message)
        return
    selected_time = message.text.strip()
    conn = get_connection()
    try:
        cur = conn.cursor()
        cur.execute(
            "SELECT status FROM slots WHERE date = %s AND time = %s",
            (selected_day, selected_time),
        )
        row = cur.fetchone()
        status = row[0] if row else None
    finally:
        conn.close()
    if status not in (1, 2):
        main_bot.send_message(chat_id, "Это время недоступно.")
        return
    add_subscriber_to_slot(selected_day, selected_time, chat_id)
    main_bot.send_message(
        chat_id, "Спасибо! Мы оповестим вас, если это время освободится."
    )

    keyboard = types.ReplyKeyboardMarkup(resize_keyboard=True, row_width=2)
    keyboard.row(
        types.KeyboardButton("Оповестить про другое время"),
        types.KeyboardButton("Вернуться на главную"),
    )
    main_bot.send_message(chat_id, "Продолжить?", reply_markup=keyboard)
    state.clear()
    user_states[chat_id] = state


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