import logging
import os
import telebot
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
user_states: dict[int, dict] = {}


def create_confirmation_keyboard(selected_day: str, selected_time: str, booking_ids):
    markup = InlineKeyboardMarkup()
    markup.add(
        InlineKeyboardButton(
            "Подтвердить", callback_data=f"confirm:{selected_day}:{selected_time}"
        )
    )
    markup.add(
        InlineKeyboardButton(
            "Отклонить", callback_data=f"reject:{selected_day}:{selected_time}"
        )
    )
    return markup


def create_cancellation_keyboard(date_str: str, start_time: str, booking_ids):
    markup = InlineKeyboardMarkup()
    markup.add(
        InlineKeyboardButton(
            "Подтвердить отмену", callback_data=f"cancel:{date_str}:{start_time}"
        )
    )
    return markup


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


@main_bot.message_handler(func=lambda msg: msg.text == "Забронировать время")
def book_time(message):
    reset_user_state(message.chat.id, user_states)
    show_free_days(message)


@main_bot.message_handler(func=lambda msg: msg.text == "Посмотреть расписание")
def view_schedule(message):
    path = create_schedule_grid_image(message.chat.id)
    try:
        with open(path, "rb") as img:
            main_bot.send_photo(message.chat.id, img, caption="Расписание на ближайшие 28 дней:")
    finally:
        try:
            os.remove(path)
        except Exception:
            pass
    reset_user_state(message.chat.id, user_states)
    show_menu(message)


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


def show_free_days(message):
    free_days = get_free_days()
    if not free_days:
        main_bot.send_message(message.chat.id, "Все дни заняты.")
        return
    iso_days = [(d.strftime("%Y-%m-%d") if hasattr(d, "strftime") else str(d)) for d in free_days]
    send_date_selection_keyboard(message.chat.id, iso_days, main_bot)
    st = user_states.get(message.chat.id)
    st = st if isinstance(st, dict) else {}
    st["step"] = "waiting_for_day"
    user_states[message.chat.id] = st


@main_bot.message_handler(
    func=lambda msg: isinstance(user_states.get(msg.chat.id), dict)
    and user_states[msg.chat.id].get("step") == "waiting_for_day"
)
def handle_day_selection(message):
    if message.text == "На главную":
        return_to_main_menu(message)
        return
    state = user_states.get(message.chat.id, {}) or {}
    btn_map = state.get("date_btn_map", {})
    selected_day = btn_map.get(message.text.strip())
    if not selected_day:
        main_bot.send_message(message.chat.id, "Выберите дату из списка.")
        show_free_days(message)
        return
    shown_days = state.get("shown_days", [])
    if selected_day not in shown_days:
        main_bot.send_message(message.chat.id, "День недоступен. Попробуйте другой.")
        show_free_days(message)
        return
    chat_id = message.chat.id
    schedule = get_schedule_for_day(selected_day, chat_id)
    filtered_all = [(t, b, g) for t, b, g in schedule if 11 <= int(t.split(':')[0]) < 24]
    today = datetime.now().strftime("%Y-%m-%d")
    current_hour = datetime.now().hour
    if selected_day == today:
        filtered_all = [x for x in filtered_all if int(x[0].split(':')[0]) > current_hour]
    lines = []
    is_admin_user = is_admin(chat_id)
    for time_str, is_booked, group_name in filtered_all:
        if is_booked:
            display = group_name if is_admin_user else "Занято"
            lines.append(f"{time_str} - *{display}*")
        else:
            lines.append(f"{time_str} -")
    formatted_date = format_date(selected_day)
    schedule_text = f"Расписание на {formatted_date}:\n" + "\n".join(lines)
    main_bot.send_message(chat_id, schedule_text, parse_mode="Markdown")
    available_times = [t for t, b, _ in filtered_all if not b]
    if not available_times:
        main_bot.send_message(chat_id, "На этот день нет свободного времени.")
        show_free_days(message)
        return
    keyboard = types.ReplyKeyboardMarkup(resize_keyboard=True, row_width=3)
    keyboard.add(*[types.KeyboardButton(t) for t in available_times])
    keyboard.add(types.KeyboardButton("Выбрать другой день"))
    main_bot.send_message(chat_id, "Выберите время:", reply_markup=keyboard)
    state["step"] = "waiting_for_time"
    state["selected_day"] = selected_day
    user_states[chat_id] = state


@main_bot.message_handler(
    func=lambda msg: isinstance(user_states.get(msg.chat.id), dict)
    and user_states[msg.chat.id].get("step") == "waiting_for_time"
)
def handle_time_selection(message):
    chat_id = message.chat.id
    state = user_states.get(chat_id, {}) or {}
    selected_day = state.get("selected_day")
    if message.text == "Выбрать другой день":
        show_free_days(message)
        return
    if message.text == "На главную":
        return_to_main_menu(message)
        return
    selected_time = message.text.strip()
    schedule = get_schedule_for_day(selected_day, chat_id)
    available_times = [t for t, b, _ in schedule if (11 <= int(t.split(':')[0]) < 24) and not b]
    today = datetime.now().strftime("%Y-%m-%d")
    current_hour = datetime.now().hour
    if selected_day == today:
        available_times = [t for t in available_times if int(t.split(':')[0]) > current_hour]
    if selected_time not in available_times:
        main_bot.send_message(chat_id, "Время занято или недоступно. Попробуйте снова.")
        return
    main_bot.send_message(
        chat_id,
        "Сколько часов будет занято?\nУкажите числом.",
        reply_markup=types.ReplyKeyboardRemove(),
    )
    state["step"] = "waiting_for_hours"
    state["selected_time"] = selected_time
    user_states[chat_id] = state


@main_bot.message_handler(
    func=lambda msg: isinstance(user_states.get(msg.chat.id), dict)
    and user_states[msg.chat.id].get("step") == "waiting_for_hours"
)
def handle_hours_input(message):
    chat_id = message.chat.id
    state = user_states.get(chat_id, {}) or {}
    try:
        hours = int(message.text.strip())
        if hours <= 0:
            raise ValueError
    except ValueError:
        main_bot.send_message(chat_id, "Введите корректное количество часов.")
        return
    selected_day = state.get("selected_day")
    selected_time = state.get("selected_time")
    start_hour = int(selected_time.split(':')[0])
    if hours > 8:
        main_bot.send_message(chat_id, "Максимум можно забронировать 8 часов.")
        return
    conflict = False
    for i in range(hours):
        current_hour = start_hour + i
        days_passed = current_hour // 24
        hour_in_day = current_hour % 24
        current_date = datetime.strptime(selected_day, "%Y-%m-%d") + timedelta(days=days_passed)
        current_date_str = current_date.strftime("%Y-%m-%d")
        full_schedule = get_schedule_for_day(current_date_str, chat_id)
        schedule_dict = {t: b for t, b, _ in full_schedule}
        time_str = f"{hour_in_day:02d}:00"
        if schedule_dict.get(time_str, 0) != 0:
            conflict = True
            break
    if conflict:
        main_bot.send_message(chat_id, "Этот временной интервал уже занят. Выберите другое время.")
        show_free_days(message)
        return
    main_bot.send_message(
        chat_id, "Введите название группы:", reply_markup=types.ReplyKeyboardRemove()
    )
    state["step"] = "waiting_for_group_name"
    state["hours"] = hours
    user_states[chat_id] = state


@main_bot.message_handler(
    func=lambda msg: isinstance(user_states.get(msg.chat.id), dict)
    and user_states[msg.chat.id].get("step") == "waiting_for_group_name"
)
def handle_group_name_input(message):
    chat_id = message.chat.id
    state = user_states.get(chat_id, {}) or {}
    group_name = message.text.strip()
    if not validate_input(group_name):
        main_bot.send_message(
            chat_id,
            'Название группы не должно превышать 100 символов и содержать символы: /, \\, *, ". Попробуйте снова.',
        )
        return
    state["group_name"] = group_name
    state["step"] = "waiting_for_contact"
    user_states[chat_id] = state
    main_bot.send_message(
        chat_id,
        "Введите ваш номер телефона, тег в телеграмме или укажите другой способ "
        "связаться с вами.\n\nМы сообщим о непредвиденных изменениях графика работы "
        "репетиционной базы.",
    )


@main_bot.message_handler(
    func=lambda msg: isinstance(user_states.get(msg.chat.id), dict)
    and user_states[msg.chat.id].get("step") == "waiting_for_contact"
)
def handle_contact_input(message):
    chat_id = message.chat.id
    state = user_states.get(chat_id, {}) or {}
    contact_info = message.text.strip()
    if not validate_input(contact_info):
        main_bot.send_message(chat_id, "Некорректная контактная информация. Попробуйте снова.")
        return
    state["contact_info"] = contact_info
    state["step"] = "waiting_for_booking_type"
    user_states[chat_id] = state
    keyboard = types.ReplyKeyboardMarkup(resize_keyboard=True)
    keyboard.add("Репетиция", "Запись", "Другое")
    main_bot.send_message(
        chat_id,
        "Тип брони.\n\nКак планируете использовать пространство репетиционной базы в бронируемое время?",
        reply_markup=keyboard,
    )


@main_bot.message_handler(
    func=lambda msg: isinstance(user_states.get(msg.chat.id), dict)
    and user_states[msg.chat.id].get("step") == "waiting_for_booking_type"
)
def handle_booking_type_selection(message):
    chat_id = message.chat.id
    state = user_states.get(chat_id, {}) or {}
    allowed_types = ["Репетиция", "Запись", "Другое"]
    if message.text not in allowed_types:
        main_bot.send_message(chat_id, "Пожалуйста, выберите тип брони из предложенных.")
        return
    if message.text == "Другое":
        main_bot.send_message(
            chat_id, "Чем планируете заниматься?", reply_markup=types.ReplyKeyboardRemove()
        )
        state["step"] = "waiting_for_custom_booking_type"
    else:
        state["booking_type"] = message.text.strip()
        state["step"] = "waiting_for_comment"
        show_comment_prompt(chat_id)
    user_states[chat_id] = state


@main_bot.message_handler(
    func=lambda msg: isinstance(user_states.get(msg.chat.id), dict)
    and user_states[msg.chat.id].get("step") == "waiting_for_custom_booking_type"
)
def handle_custom_booking_type(message):
    chat_id = message.chat.id
    state = user_states.get(chat_id, {}) or {}
    booking_type = message.text.strip()
    if not validate_input(booking_type):
        main_bot.send_message(chat_id, "Некорректный тип брони. Попробуйте снова.")
        return
    state["booking_type"] = booking_type
    state["step"] = "waiting_for_comment"
    user_states[chat_id] = state
    show_comment_prompt(chat_id)


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


@main_bot.message_handler(
    func=lambda msg: isinstance(user_states.get(msg.chat.id), dict)
    and user_states[msg.chat.id].get("step") == "waiting_for_comment"
    and msg.text == "Прайс"
)
def show_price_list_during_booking(message):
    chat_id = message.chat.id
    try:
        with open("price.txt", "r", encoding="utf-8") as file:
            price_list = file.read().strip()
    except FileNotFoundError:
        price_list = "Информация о прайсе временно недоступна."
    main_bot.send_message(chat_id, price_list)
    show_comment_prompt(chat_id)


@main_bot.message_handler(
    func=lambda msg: isinstance(user_states.get(msg.chat.id), dict)
    and user_states[msg.chat.id].get("step") == "waiting_for_comment"
)
def handle_comment_input(message):
    chat_id = message.chat.id
    state = user_states.get(chat_id, {}) or {}
    if message.text == "Прайс":
        return
    comment = "" if message.text == "Ок" else message.text.strip()
    if comment and not validate_input(comment, max_length=200):
        main_bot.send_message(
            chat_id,
            'Комментарий не должен превышать 200 символов и содержать символы: /, \\, *, ". '
            "Попробуйте снова.",
        )
        return
    selected_day = state.get("selected_day")
    selected_time = state.get("selected_time")
    hours = state.get("hours")
    start_hour = int(selected_time.split(':')[0])
    date_obj = datetime.strptime(selected_day, "%Y-%m-%d")
    conn = get_connection()
    try:
        cur = conn.cursor()
        conflict = False
        for i in range(hours):
            current_hour = start_hour + i
            days_passed = current_hour // 24
            hour_in_day = current_hour % 24
            slot_date = (date_obj + timedelta(days=days_passed)).strftime("%Y-%m-%d")
            slot_time = f"{hour_in_day:02d}:00"
            cur.execute(
                "SELECT status FROM slots WHERE date = %s AND time = %s",
                (slot_date, slot_time),
            )
            row = cur.fetchone()
            status = row[0] if row else 0
            if status != 0:
                conflict = True
                break
    finally:
        conn.close()
    if conflict:
        main_bot.send_message(
            chat_id,
            "Это время уже занято другим пользователем. Пожалуйста, выберите другое время.",
        )
        reset_user_state(chat_id, user_states)
        show_menu(message)
        return
    group_name = state.get("group_name")
    booking_type = state.get("booking_type")
    contact_info = state.get("contact_info")
    start_datetime = datetime.combine(date_obj.date(), datetime.min.time()).replace(
        hour=start_hour, minute=0
    )
    end_datetime = start_datetime + timedelta(hours=hours)
    end_time = f"{end_datetime.hour:02d}:00"
    book_slots(
        selected_day,
        selected_time,
        hours,
        chat_id,
        group_name,
        booking_type,
        comment,
        contact_info,
    )
    conn = get_connection()
    booking_ids = []
    try:
        cur = conn.cursor()
        current_date = datetime.strptime(selected_day, "%Y-%m-%d")
        for i in range(hours):
            current_hour = start_hour + i
            days_passed = current_hour // 24
            hour_in_day = current_hour % 24
            slot_date = (current_date + timedelta(days=days_passed)).strftime("%Y-%m-%d")
            slot_time = f"{hour_in_day:02d}:00"
            cur.execute(
                "SELECT id FROM slots WHERE date = %s AND time = %s AND user_id = %s",
                (slot_date, slot_time, chat_id),
            )
            row = cur.fetchone()
            if row:
                booking_ids.append(row[0])
    finally:
        conn.close()
    try:
        formatted_date = format_date(selected_day).replace(" ", ".")[:-3]
    except ValueError:
        formatted_date = selected_day
    main_bot.send_message(
        chat_id,
        "Спасибо! 👍\n"
        f"Вы забронировали *{hours}* {get_hour_word(hours)} с *{selected_time} по {end_time}* *{formatted_date}*\n"
        f"Группа: *{group_name}*\n"
        "Пожалуйста, ожидайте подтверждения брони администратором.",
        parse_mode="Markdown",
    )
    if message.from_user.username:
        mention = f"@{message.from_user.username}"
    elif contact_info.startswith("@"):
        mention = contact_info
    elif contact_info.replace("+", "").isdigit():
        mention = f"[{message.from_user.first_name}](tel:{contact_info})"
    else:
        mention = f"{message.from_user.first_name} (ID: {message.from_user.id})"
    note = (
        "🔔 *Новая бронь!*\n"
        f"_Дата:_ *{selected_day}*\n"
        f"_Время:_ *{selected_time}-{end_time}*\n"
        f"_Группа:_ *{group_name}*\n"
        f"_Тип:_ *{booking_type}*\n"
        f"_Комментарий:_ {comment}\n"
        f"_Контакт:_ {contact_info}\n"
        f"_Создатель:_ {mention}"
    )
    for admin_id in ADMIN_IDS:
        try:
            admin_bot.send_message(
                admin_id,
                note,
                parse_mode='Markdown',
                reply_markup=create_confirmation_keyboard(selected_day, selected_time, booking_ids)
            )
        except Exception as e:
            print(f"[Error] Can't send message to admin {admin_id}: {e}")
    keyboard = types.ReplyKeyboardMarkup(resize_keyboard=True)
    keyboard.add(types.KeyboardButton("Забронировать другое время"))
    keyboard.add(types.KeyboardButton("Вернуться на главную"))
    main_bot.send_message(chat_id, "Продолжить?", reply_markup=keyboard)
    reset_user_state(chat_id, user_states)


@main_bot.message_handler(func=lambda msg: msg.text == "Забронировать другое время")
def book_another_time(message):
    reset_user_state(message.chat.id, user_states)
    show_free_days(message)


@main_bot.message_handler(func=lambda msg: msg.text == "Вернуться на главную")
def return_to_main_menu(message):
    reset_user_state(message.chat.id, user_states)
    show_menu(message)


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


def main():
    init_db()
    main_bot.polling(none_stop=True)


if __name__ == "__main__":
    main()