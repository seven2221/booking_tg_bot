import os
import re
import sqlite3
import telebot
import logging
from datetime import datetime, timedelta
from dotenv import load_dotenv
from telebot import types
from telebot.types import InlineKeyboardMarkup, InlineKeyboardButton

from schedule_generator import create_schedule_grid_image
from utils import is_admin, reset_user_state, format_date, format_date_to_db, get_schedule_for_day, get_hour_word, update_booking_status, get_free_days, book_slots, create_confirmation_keyboard, create_cancellation_keyboard, get_booked_days_filtered, add_subscriber_to_slot, get_grouped_bookings_for_cancellation, send_date_selection_keyboard
from db_init import init_db

logging.basicConfig(level=logging.INFO)
load_dotenv()

MAIN_BOT_TOKEN = os.getenv("MAIN_BOT_TOKEN")
ADMIN_BOT_TOKEN = os.getenv("ADMIN_BOT_TOKEN")
ADMIN_IDS = list(map(int, os.getenv("ADMIN_IDS", "").split(",")))

main_bot = telebot.TeleBot(MAIN_BOT_TOKEN)
admin_bot = telebot.TeleBot(ADMIN_BOT_TOKEN)

user_states = {}
today = datetime.now().strftime("%Y-%m-%d")

@main_bot.message_handler(func=lambda msg: msg.text == "Посмотреть прайс")
def show_price_list(message):
    try:
        with open('price.txt', 'r', encoding='utf-8') as file:
            price_list = file.read().strip()
    except FileNotFoundError:
        price_list = "Информация о прайсе временно недоступна."
    main_bot.send_message(message.chat.id, price_list)
    reset_user_state(message.chat.id, user_states)
    show_menu(message)

def show_menu(message):
    keyboard = types.ReplyKeyboardMarkup(resize_keyboard=True)
    keyboard.add(types.KeyboardButton("Посмотреть расписание"))
    keyboard.add(types.KeyboardButton("Забронировать время"))
    keyboard.add(types.KeyboardButton("Отменить бронь"))
    keyboard.add(types.KeyboardButton("Посмотреть прайс"))
    keyboard.add(types.KeyboardButton("Быть в курсе, если освободится время"))
    main_bot.send_message(message.chat.id, "Выберите действие:", reply_markup=keyboard)
    reset_user_state(message.chat.id, user_states)

@main_bot.message_handler(commands=['start'])
def start(message):
    main_bot.set_my_commands([telebot.types.BotCommand("/start", "Главное меню")])
    show_menu(message)

@main_bot.message_handler(func=lambda msg: msg.text == "Забронировать время")
def book_time(message):
    reset_user_state(message.chat.id, user_states)
    show_free_days(message)

@main_bot.message_handler(func=lambda msg: msg.text == "Посмотреть расписание")
def view_schedule(message):
    path = create_schedule_grid_image(message.chat.id)
    with open(path, "rb") as img:
        main_bot.send_photo(message.chat.id, img, caption="Расписание на ближайшие 28 дней:")
    os.remove(path)
    reset_user_state(message.chat.id, user_states)
    show_menu(message)

@main_bot.message_handler(func=lambda msg: msg.text == "Быть в курсе, если освободится время")
def subscribe_to_free_slots(message):
    reset_user_state(message.chat.id, user_states)
    booked_days = get_booked_days_filtered()
    if not booked_days:
        main_bot.send_message(message.chat.id, "Нет забронированных дней.")
        return
    keyboard = types.ReplyKeyboardMarkup(resize_keyboard=True)
    keyboard.add(*[types.KeyboardButton(format_date(day)) for day in booked_days])
    main_bot.send_message(message.chat.id, "Выберите день:", reply_markup=keyboard)
    user_states[message.chat.id] = 'waiting_for_subscribe_day'

@main_bot.message_handler(func=lambda msg: user_states.get(msg.chat.id) == 'waiting_for_subscribe_day')
def handle_subscribe_day_selection(message):
    try:
        selected_day = datetime.strptime(message.text.split()[0], '%d.%m').replace(year=datetime.now().year).strftime('%Y-%m-%d')
    except ValueError:
        main_bot.send_message(message.chat.id, "Неверный формат. Попробуйте снова.")
        subscribe_to_free_slots(message)
        return
    chat_id = message.chat.id
    now = datetime.now()
    if selected_day == today:
        current_hour = now.hour
        conn = sqlite3.connect('bookings.db')
        cursor = conn.cursor()
        cursor.execute('''
            SELECT time, subscribed_users FROM slots 
            WHERE date = ? AND status IN (1, 2) AND time > ?
        ''', (selected_day, f"{current_hour}:00"))
        rows = cursor.fetchall()
        conn.close()
    else:
        conn = sqlite3.connect('bookings.db')
        cursor = conn.cursor()
        cursor.execute('''
            SELECT time, subscribed_users FROM slots 
            WHERE date = ? AND status IN (1, 2)
        ''', (selected_day,))
        rows = cursor.fetchall()
        conn.close()
    if not rows:
        main_bot.send_message(message.chat.id, "В этот день нет подходящих слотов.")
        return
    available_times = []
    for time, subs in rows:
        subs_list = subs.split(',') if subs else []
        if str(chat_id) not in subs_list:
            available_times.append(time)
    if not available_times:
        main_bot.send_message(message.chat.id, "Вы уже подписаны на все доступные слоты этого дня.")
        return
    keyboard = types.ReplyKeyboardMarkup(resize_keyboard=True, row_width=3)
    keyboard.add(*[types.KeyboardButton(t) for t in available_times])
    keyboard.add(types.KeyboardButton("Выбрать другой день"))
    main_bot.send_message(message.chat.id, "Выберите время, на которое хотите подписаться:", reply_markup=keyboard)
    user_states[message.chat.id] = 'waiting_for_subscribe_time'
    user_states[f"{message.chat.id}_subscribe_day"] = selected_day

@main_bot.message_handler(func=lambda msg: user_states.get(msg.chat.id) == 'waiting_for_subscribe_time')
def handle_subscribe_time_selection(message):
    chat_id = message.chat.id
    selected_day = user_states.get(f"{chat_id}_subscribe_day")
    if message.text == "Выбрать другой день":
        reset_user_state(chat_id, user_states)
        subscribe_to_free_slots(message)
        return
    selected_time = message.text.strip()
    conn = sqlite3.connect('bookings.db')
    cursor = conn.cursor()
    cursor.execute('''
        SELECT status FROM slots 
        WHERE date = ? AND time = ?
    ''', (selected_day, selected_time))
    result = cursor.fetchone()
    conn.close()
    if not result or result[0] not in (1, 2):
        main_bot.send_message(chat_id, "Это время недоступно.")
        return
    add_subscriber_to_slot(selected_day, selected_time, chat_id)

    main_bot.send_message(chat_id, "Спасибо!\nМы оповестим вас, если это время освободится.")
    keyboard = types.ReplyKeyboardMarkup(resize_keyboard=True)
    keyboard.add(types.KeyboardButton("Оповестить про другое время"))
    keyboard.add(types.KeyboardButton("Вернуться на главную"))
    main_bot.send_message(chat_id, "Продолжить?", reply_markup=keyboard)
    reset_user_state(chat_id, user_states)

@main_bot.message_handler(func=lambda msg: msg.text == "Оповестить про другое время")
def book_another_time(message):
    reset_user_state(message.chat.id, user_states)
    subscribe_to_free_slots(message)

def show_free_days(message):
    free_days = get_free_days()
    now = datetime.now()
    current_hour = now.hour
    if not free_days:
        main_bot.send_message(message.chat.id, "Все дни заняты.")
        return
    filtered_days = []
    for day in free_days:
        day_dt = datetime.strptime(day, "%Y-%m-%d")
        if day_dt.date() == now.date():
            schedule = get_schedule_for_day(day)
            available_times = [t for t, b, _ in schedule if not b]
            future_times = [t for t in available_times if int(t.split(':')[0]) > current_hour]
            if future_times:
                filtered_days.append(day)
        else:
            filtered_days.append(day)
    if not filtered_days:
        main_bot.send_message(message.chat.id, "Нет доступных дней.")
        return
    keyboard = types.ReplyKeyboardMarkup(resize_keyboard=True)
    keyboard.add(*[types.KeyboardButton(format_date(day)) for day in filtered_days])
    main_bot.send_message(message.chat.id, "Свободные дни:", reply_markup=keyboard)
    user_states[message.chat.id] = 'waiting_for_day'

@main_bot.message_handler(func=lambda msg: user_states.get(msg.chat.id) == 'waiting_for_day')
def handle_day_selection(message):
    try:
        selected_day = datetime.strptime(message.text.split()[0], '%d.%m').replace(year=datetime.now().year).strftime('%Y-%m-%d')
    except ValueError:
        main_bot.send_message(message.chat.id, "Неверный формат. Попробуйте снова.")
        show_free_days(message)
        return
    if selected_day not in get_free_days():
        main_bot.send_message(message.chat.id, "День недоступен. Попробуйте другой.")
        show_free_days(message)
        return
    schedule = get_schedule_for_day(selected_day, message.chat.id)
    filtered_schedule = [(t, b, g) for t, b, g in schedule if 11 <= int(t.split(':')[0]) < 24]
    text = "\n".join([f"{t} - *{g}*" if g else f"{t} -" for t, _, g in filtered_schedule])
    main_bot.send_message(message.chat.id, f"Расписание на {format_date(selected_day)}:\n{text}", parse_mode='Markdown')
    keyboard = types.ReplyKeyboardMarkup(resize_keyboard=True, row_width=3)
    keyboard.add(*[types.KeyboardButton(t) for t, b, _ in filtered_schedule if not b])
    keyboard.add(types.KeyboardButton("Выбрать другой день"))
    main_bot.send_message(message.chat.id, "Выберите время:", reply_markup=keyboard)
    user_states[message.chat.id] = 'waiting_for_time'
    user_states[f"{message.chat.id}_selected_day"] = selected_day

@main_bot.message_handler(func=lambda msg: user_states.get(msg.chat.id) == 'waiting_for_time')
def handle_time_selection(message):
    chat_id = message.chat.id
    selected_day = user_states.get(f"{chat_id}_selected_day")
    if message.text == "Выбрать другой день":
        reset_user_state(chat_id, user_states)
        show_free_days(message)
        return
    selected_time = message.text.strip()
    schedule = get_schedule_for_day(selected_day)
    available_times = [t for t, b, _ in schedule if not b]
    if selected_time not in available_times:
        main_bot.send_message(chat_id, "Время занято или недоступно. Попробуйте снова.")
        return
    main_bot.send_message(chat_id, "Сколько часов будет занято?\nУкажите числом.", reply_markup=types.ReplyKeyboardRemove())
    user_states[chat_id] = 'waiting_for_hours'
    user_states[f"{chat_id}_selected_time"] = selected_time

@main_bot.message_handler(func=lambda msg: user_states.get(msg.chat.id) == 'waiting_for_hours')
def handle_hours_input(message):
    chat_id = message.chat.id
    try:
        hours = int(message.text.strip())
        if hours <= 0:
            raise ValueError
    except ValueError:
        main_bot.send_message(chat_id, "Введите корректное количество часов.")
        return
    selected_day = user_states.get(f"{chat_id}_selected_day")
    selected_time = user_states.get(f"{chat_id}_selected_time")
    start_hour = int(selected_time.split(":")[0])
    if hours > 8:
        main_bot.send_message(chat_id, "Максимум можно забронировать 8 часов.")
        return
    schedule = get_schedule_for_day(selected_day)
    schedule_dict = {t: b for t, b, _ in schedule}
    conflict = False
    for i in range(hours):
        current_hour = start_hour + i
        days_passed = current_hour // 24
        hour_in_day = current_hour % 24
        current_date = datetime.strptime(selected_day, "%Y-%m-%d") + timedelta(days=days_passed)
        current_date_str = current_date.strftime("%Y-%m-%d")
        full_schedule = get_schedule_for_day(current_date_str)
        schedule_dict = {t: b for t, b, _ in full_schedule}
        time_str = f"{hour_in_day:02d}:00"
        if schedule_dict.get(time_str, 0) != 0:
            conflict = True
            break
    if conflict:
        main_bot.send_message(chat_id, "Этот временной интервал уже занят. Выберите другое время.")
        show_free_days(message)
        return
    main_bot.send_message(chat_id, "Введите название группы:", reply_markup=types.ReplyKeyboardRemove())
    user_states[chat_id] = 'waiting_for_group_name'
    user_states[f"{chat_id}_hours"] = hours

@main_bot.message_handler(func=lambda msg: user_states.get(msg.chat.id) == 'waiting_for_group_name')
def handle_group_name_input(message):
    chat_id = message.chat.id
    group_name = message.text.strip()
    if not group_name or re.search(r"[;'\"]|^\s*/", group_name):
        main_bot.send_message(chat_id, "Некорректное название. Попробуйте снова.")
        return
    user_states[chat_id] = 'waiting_for_contact'
    user_states[f"{chat_id}_group_name"] = group_name
    main_bot.send_message(chat_id, "Введите ваш номер телефона, тег в телеграмме или укажите другой способ связаться с вами.\n\nМы сообщим о непредвиденных изменениях графика работы репетиционной базы.")

@main_bot.message_handler(func=lambda msg: user_states.get(msg.chat.id) == 'waiting_for_contact')
def handle_contact_input(message):
    chat_id = message.chat.id
    contact_info = message.text.strip()
    if not contact_info:
        main_bot.send_message(chat_id, "Контактная информация обязательна. Попробуйте снова.")
        return
    user_states[f"{chat_id}_contact_info"] = contact_info
    user_states[chat_id] = 'waiting_for_booking_type'
    keyboard = types.ReplyKeyboardMarkup(resize_keyboard=True)
    keyboard.add("Репетиция", "Запись", "Другое")
    main_bot.send_message(chat_id, "Тип брони.\n\nКак планируете использовать пространство репетиционной базы в бронируемое время?", reply_markup=keyboard)

@main_bot.message_handler(func=lambda msg: user_states.get(msg.chat.id) == 'waiting_for_booking_type')
def handle_booking_type_selection(message):
    chat_id = message.chat.id
    if message.text == "Другое":
        main_bot.send_message(chat_id, "Чем планируете заниматься?", reply_markup=types.ReplyKeyboardRemove())
        user_states[chat_id] = 'waiting_for_custom_booking_type'
    else:
        user_states[f"{chat_id}_booking_type"] = message.text.strip()
        user_states[chat_id] = 'waiting_for_comment'
        show_comment_prompt(chat_id)

@main_bot.message_handler(func=lambda msg: user_states.get(msg.chat.id) == 'waiting_for_custom_booking_type')
def handle_custom_booking_type(message):
    chat_id = message.chat.id
    user_states[f"{chat_id}_booking_type"] = message.text.strip()
    user_states[chat_id] = 'waiting_for_comment'
    show_comment_prompt(chat_id)

def show_comment_prompt(chat_id):
    keyboard = types.ReplyKeyboardMarkup(resize_keyboard=True)
    keyboard.add("Ок")
    main_bot.send_message(chat_id, "Если вам необходимы какие-либо дополнительные услуги из нашего прайса, пожалуйста, укажите их в комментарии.\n\nЕсли доп.услуги не требуются, нажмите 'Ок'.", reply_markup=keyboard)

@main_bot.message_handler(func=lambda msg: user_states.get(msg.chat.id) == 'waiting_for_comment')
def handle_comment_input(message):
    chat_id = message.chat.id
    if message.text == "Ок":
        comment = ""
    else:
        comment = message.text.strip()
    selected_day = user_states.get(f"{chat_id}_selected_day")
    selected_time = user_states.get(f"{chat_id}_selected_time")
    hours = user_states.get(f"{chat_id}_hours")
    start_hour = int(selected_time.split(":")[0])
    date_obj = datetime.strptime(selected_day, "%Y-%m-%d")
    conn = sqlite3.connect('bookings.db')
    cursor = conn.cursor()
    conflict = False
    for i in range(hours):
        current_hour = start_hour + i
        days_passed = current_hour // 24
        hour_in_day = current_hour % 24
        slot_date = (date_obj + timedelta(days=days_passed)).strftime("%Y-%m-%d")
        slot_time = f"{hour_in_day:02d}:00"
        cursor.execute("SELECT status FROM slots WHERE date = ? AND time = ?", (slot_date, slot_time))
        result = cursor.fetchone()
        if result and result[0] != 0:
            conflict = True
            break
    conn.close()
    if conflict:
        main_bot.send_message(chat_id, "Это время уже занято другим пользователем. Пожалуйста, выберите другое время.")
        reset_user_state(chat_id, user_states)
        show_menu(message)
        return
    group_name = user_states.get(f"{chat_id}_group_name")
    booking_type = user_states.get(f"{chat_id}_booking_type")
    contact_info = user_states.get(f"{chat_id}_contact_info")
    start_datetime = datetime.combine(date_obj.date(), datetime.min.time()).replace(hour=start_hour, minute=0)
    end_datetime = start_datetime + timedelta(hours=hours)
    end_time = f"{end_datetime.hour}:00"
    book_slots(selected_day, selected_time, hours, chat_id, group_name, booking_type, comment, contact_info)
    conn = sqlite3.connect('bookings.db')
    cursor = conn.cursor()
    booking_ids = []
    current_date = datetime.strptime(selected_day, "%Y-%m-%d")
    for i in range(hours):
        current_hour = start_hour + i
        days_passed = current_hour // 24
        hour_in_day = current_hour % 24
        slot_date = (current_date + timedelta(days=days_passed)).strftime("%Y-%m-%d")
        slot_time = f"{hour_in_day:02d}:00"
        cursor.execute("SELECT id FROM slots WHERE date = ? AND time = ? AND user_id = ?", (slot_date, slot_time, chat_id))
        row = cursor.fetchone()
        if row:
            booking_ids.append(row[0])
    conn.close()
    try:
        formatted_date = datetime.strptime(selected_day, "%Y-%m-%d").strftime("%d.%m.%Y")
    except ValueError:
        formatted_date = selected_day
    main_bot.send_message(chat_id, f"Спасибо! 👍\nВы забронировали {hours} {get_hour_word(hours)} с {selected_time} по {end_time} {formatted_date}\nГруппа: {group_name}\nПожалуйста, ожидайте подтверждения брони администратором.", parse_mode='Markdown')
    mention = f"[{message.from_user.first_name}](tg://user?id={message.from_user.id})"
    note = f"🔔 Новая бронь!\nДата: {selected_day}\nВремя: {selected_time}-{end_time}\nГруппа: {group_name}\nТип: {booking_type}\nКомментарий: {comment}\nКонтакт: {contact_info}\nСоздатель: {mention}"
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
    conn = sqlite3.connect('bookings.db')
    cursor = conn.cursor()
    cursor.execute('''
        SELECT DISTINCT date FROM slots 
        WHERE status IN (1, 2) AND created_by = ? AND date >= ?
        ORDER BY date
    ''', (chat_id, today))
    all_dates = [row[0] for row in cursor.fetchall()]
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
    user_states[chat_id] = {
        "step": "choose_date_for_cancellation",
        "valid_dates": valid_dates
    }
    send_date_selection_keyboard(chat_id, valid_dates, main_bot)

@main_bot.message_handler(func=lambda msg: user_states.get(msg.chat.id, {}).get("step") == "choose_date_for_cancellation")
def handle_date_chosen_for_cancellation(message):
    chat_id = message.chat.id

    if message.text == "На главную":
        reset_user_state(chat_id, user_states)
        show_menu(message)
        return

    selected_date_formatted = message.text
    try:
        selected_date = datetime.strptime(selected_date_formatted, "%Y-%m-%d").strftime("%Y-%m-%d")
    except ValueError:
        try:
            selected_date = format_date_to_db(selected_date_formatted)
        except Exception as e:
            main_bot.send_message(chat_id, "Неверный формат даты. Попробуйте снова.")
            send_date_selection_keyboard(chat_id, user_states[chat_id]["valid_dates"], main_bot)
            return

    valid_dates = [datetime.strptime(d, "%Y-%m-%d").strftime("%Y-%m-%d") for d in user_states[chat_id]["valid_dates"]]
    if selected_date not in valid_dates:
        main_bot.send_message(chat_id, "Выберите одну из предложенных дат.")
        return

    bookings = get_grouped_bookings_for_cancellation(selected_date, chat_id)
    if not bookings:
        main_bot.send_message(chat_id, "На эту дату у вас нет броней.")
        return

    user_states[chat_id].update({
        "step": "choose_booking_for_cancellation",
        "selected_date": selected_date,
        "bookings": bookings
    })
    send_cancellation_options(chat_id, bookings)

def send_cancellation_options(chat_id, bookings):
    markup = types.ReplyKeyboardMarkup(resize_keyboard=True)
    for booking in bookings:
        start_time = booking['start_time'].strftime("%H:%M")
        end_time = booking['end_time'].strftime("%H:%M")
        group_name = booking['group_name']
        markup.add(types.KeyboardButton(f"{start_time}–{end_time}, {group_name}"))
    markup.add(types.KeyboardButton("На главную"))
    main_bot.send_message(chat_id, "Выберите бронь для отмены:", reply_markup=markup)

@main_bot.message_handler(func=lambda msg: user_states.get(msg.chat.id, {}).get("step") == "choose_booking_for_cancellation")
def handle_user_choose_booking_for_cancellation(message):
    chat_id = message.chat.id
    if message.text == "На главную":
        reset_user_state(chat_id, user_states)
        show_menu(message)
        return
    selected_text = message.text.strip()
    bookings = user_states[chat_id].get("bookings", [])
    found = False
    for index, booking in enumerate(bookings):
        start_time = booking['start_time'].strftime("%H:%M")
        end_time = booking['end_time'].strftime("%H:%M")
        group_name = booking['group_name']
        button_text = f"{start_time}–{end_time}, {group_name}"
        if selected_text == button_text:
            found = True
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
        formatted_date = datetime.strptime(date_str, "%Y-%m-%d").strftime("%d.%m.%Y")
    except ValueError:
        formatted_date = date_str
    mention = f"[{message.from_user.first_name}](tg://user?id={chat_id})"
    note = (
        f"🔔 Запрос на отмену брони!\n"
        f"Дата: {date_str}\n"
        f"Время: {start_time}–{end_time}\n"
        f"Группа: {group_name}\n"
        f"Создатель: {mention}"
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
    main_bot.send_message(chat_id, "Запрос на отмену брони отправлен администратору. Ожидайте подтверждения.")
    show_menu(message)


if __name__ == "__main__":
    init_db()
    main_bot.polling(none_stop=True)