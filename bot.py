import os
import re
import sqlite3
from datetime import datetime, timedelta
from dotenv import load_dotenv
import telebot
from telebot import types
from telebot.types import InlineKeyboardMarkup, InlineKeyboardButton
import logging
from schedule_generator import create_schedule_grid_image
from utils import is_admin, format_date, get_schedule_for_day

logging.basicConfig(level=logging.INFO)
load_dotenv()

MAIN_BOT_TOKEN = os.getenv("MAIN_BOT_TOKEN")
ADMIN_BOT_TOKEN = os.getenv("ADMIN_BOT_TOKEN")
ADMIN_IDS = list(map(int, os.getenv("ADMIN_IDS", "").split(",")))

main_bot = telebot.TeleBot(MAIN_BOT_TOKEN)
admin_bot = telebot.TeleBot(ADMIN_BOT_TOKEN)

user_states = {}

def init_db():
    conn = sqlite3.connect('bookings.db', check_same_thread=False)
    cursor = conn.cursor()
    cursor.execute('''
        CREATE TABLE IF NOT EXISTS slots (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            date TEXT NOT NULL,
            time TEXT NOT NULL
        )
    ''')
    
    def add_column_if_not_exists(table_name, column_name, column_definition):
        cursor.execute(f"PRAGMA table_info({table_name})")
        columns = [column[1] for column in cursor.fetchall()]
        if column_name not in columns:
            cursor.execute(f"ALTER TABLE {table_name} ADD COLUMN {column_name} {column_definition}")

    add_column_if_not_exists("slots", "user_id", "INTEGER DEFAULT NULL")
    add_column_if_not_exists("slots", "group_name", "TEXT DEFAULT NULL")
    add_column_if_not_exists("slots", "created_by", "INTEGER DEFAULT NULL")
    add_column_if_not_exists("slots", "subscribed_users", "TEXT DEFAULT NULL")
    add_column_if_not_exists("slots", "booking_type", "TEXT DEFAULT NULL")
    add_column_if_not_exists("slots", "comment", "TEXT DEFAULT NULL")
    add_column_if_not_exists("slots", "contact_info", "TEXT DEFAULT NULL")
    add_column_if_not_exists("slots", "status", "INTEGER DEFAULT 0")

    cursor.execute('SELECT COUNT(*) FROM slots')
    if cursor.fetchone()[0] == 0:
        times = [f"{hour}:00" for hour in range(11, 24)]
        today = datetime.now()
        for i in range(30):
            date = (today + timedelta(days=i)).strftime('%Y-%m-%d')
            for time in times:
                cursor.execute('INSERT INTO slots (date, time, status) VALUES (?, ?, ?)', (date, time, 0))
    
    conn.commit()
    conn.close()

def update_booking_status(date, time, status):
    conn = sqlite3.connect('bookings.db', check_same_thread=False)
    cursor = conn.cursor()
    cursor.execute('''
        UPDATE slots 
        SET status = ?
        WHERE date = ? AND time = ?
    ''', (status, date, time))
    conn.commit()
    conn.close()

def get_free_days():
    conn = sqlite3.connect('bookings.db', check_same_thread=False)
    cursor = conn.cursor()
    cursor.execute('SELECT DISTINCT date FROM slots WHERE status = 0 ORDER BY date')
    free_days = [row[0] for row in cursor.fetchall()]
    conn.close()
    return free_days

def book_slots(date, start_time, hours, user_id, group_name, booking_type, comment, contact_info):
    conn = sqlite3.connect('bookings.db', check_same_thread=False)
    cursor = conn.cursor()
    start_hour = int(start_time.split(":")[0])
    for i in range(hours):
        current_hour = start_hour + i
        if current_hour >= 24:
            break
        time = f"{current_hour}:00"
        cursor.execute('''
            UPDATE slots 
            SET user_id = ?, group_name = ?, created_by = ?, booking_type = ?, comment = ?, contact_info = ?, status = 1
            WHERE date = ? AND time = ?''',
            (user_id, group_name, user_id, booking_type, comment, contact_info, date, time))
    conn.commit()
    conn.close()

def reset_user_state(chat_id):
    keys = list(user_states.keys())
    for key in keys:
        if str(chat_id) in str(key):
            user_states.pop(key, None)

def create_confirmation_keyboard(selected_day, selected_time):
    conn = sqlite3.connect('bookings.db')
    cursor = conn.cursor()
    cursor.execute('''
        SELECT id, created_by, time 
        FROM slots 
        WHERE date = ? AND time >= ?
        ORDER BY time
    ''', (selected_day, selected_time))
    rows = cursor.fetchall()
    conn.close()
    if not rows:
        return None
    bookings = []
    for row in rows:
        bid, user_id, time_str = row
        try:
            dt = datetime.strptime(f"{selected_day} {time_str}", "%Y-%m-%d %H:%M")
        except ValueError:
            continue
        bookings.append({'id': bid, 'datetime': dt, 'time': time_str, 'user_id': user_id})
    grouped = []
    current_group = None
    for booking in bookings:
        if not current_group:
            current_group = {
                'start_time': booking['datetime'],
                'end_time': booking['datetime'] + timedelta(hours=1),
                'ids': [booking['id']],
                'user_id': booking['user_id']
            }
        else:
            if booking['user_id'] == current_group['user_id'] and booking['datetime'] == current_group['end_time']:
                current_group['end_time'] += timedelta(hours=1)
                current_group['ids'].append(booking['id'])
            else:
                grouped.append(current_group)
                current_group = {
                    'start_time': booking['datetime'],
                    'end_time': booking['datetime'] + timedelta(hours=1),
                    'ids': [booking['id']],
                    'user_id': booking['user_id']
                }
    if current_group:
        grouped.append(current_group)
    if not grouped:
        return None
    group = grouped[0]
    booking_ids = group['ids']
    user_id = group['user_id']
    keyboard = InlineKeyboardMarkup()
    keyboard.row(
        InlineKeyboardButton("✅ Подтвердить", callback_data=f"confirm:{','.join(map(str, booking_ids))}:{user_id}"),
        InlineKeyboardButton("❌ Отклонить", callback_data=f"reject:{','.join(map(str, booking_ids))}:{user_id}")
    )
    return keyboard

@main_bot.message_handler(func=lambda msg: msg.text == "Посмотреть прайс")
def show_price_list(message):
    try:
        with open('price.txt', 'r', encoding='utf-8') as file:
            price_list = file.read().strip()
    except FileNotFoundError:
        price_list = "Информация о прайсе временно недоступна."
    main_bot.send_message(message.chat.id, price_list)
    reset_user_state(message.chat.id)
    show_main_menu(message)

def show_main_menu(message):
    keyboard = types.ReplyKeyboardMarkup(resize_keyboard=True)
    keyboard.add(types.KeyboardButton("Забронировать время"))
    keyboard.add(types.KeyboardButton("Посмотреть расписание"))
    keyboard.add(types.KeyboardButton("Отменить бронь"))
    keyboard.add(types.KeyboardButton("Посмотреть прайс"))
    main_bot.send_message(message.chat.id, "Выберите действие:", reply_markup=keyboard)
    reset_user_state(message.chat.id)

@main_bot.message_handler(commands=['start'])
def start(message):
    main_bot.set_my_commands([telebot.types.BotCommand("/start", "Главное меню")])
    show_main_menu(message)

@main_bot.message_handler(func=lambda msg: msg.text == "Забронировать время")
def book_time(message):
    reset_user_state(message.chat.id)
    show_free_days(message)

@main_bot.message_handler(func=lambda msg: msg.text == "Посмотреть расписание")
def view_schedule(message):
    path = create_schedule_grid_image(message.chat.id)
    with open(path, "rb") as img:
        main_bot.send_photo(message.chat.id, img, caption="Расписание на ближайшие дни:")
    os.remove(path)
    reset_user_state(message.chat.id)
    show_main_menu(message)

@main_bot.message_handler(func=lambda msg: msg.text == "Отменить бронь")
def cancel_booking(message):
    main_bot.send_message(message.chat.id, "Функция отмены временно недоступна. Обратитесь к @admin_nora")
    reset_user_state(message.chat.id)
    show_main_menu(message)

def show_free_days(message):
    free_days = get_free_days()
    if not free_days:
        main_bot.send_message(message.chat.id, "Все дни заняты.")
        return
    keyboard = types.ReplyKeyboardMarkup(resize_keyboard=True)
    keyboard.add(*[types.KeyboardButton(format_date(day)) for day in free_days])
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
    text = "\n".join([f"{t} - *{g}*" if g else f"{t} -" for t, _, g in schedule])
    main_bot.send_message(message.chat.id, f"Расписание на {format_date(selected_day)}:\n{text}", parse_mode='Markdown')
    keyboard = types.ReplyKeyboardMarkup(resize_keyboard=True, row_width=3)
    keyboard.add(*[types.KeyboardButton(t) for t, b, _ in schedule if not b])
    keyboard.add(types.KeyboardButton("Выбрать другой день"))
    main_bot.send_message(message.chat.id, "Выберите время:", reply_markup=keyboard)
    user_states[message.chat.id] = 'waiting_for_time'
    user_states[f"{message.chat.id}_selected_day"] = selected_day

@main_bot.message_handler(func=lambda msg: user_states.get(msg.chat.id) == 'waiting_for_time')
def handle_time_selection(message):
    chat_id = message.chat.id
    selected_day = user_states.get(f"{chat_id}_selected_day")
    if message.text == "Выбрать другой день":
        reset_user_state(chat_id)
        show_free_days(message)
        return
    selected_time = message.text.strip()
    schedule = get_schedule_for_day(selected_day)
    available_times = [t for t, b, _ in schedule if not b]
    if selected_time not in available_times:
        main_bot.send_message(chat_id, "Время занято или недоступно. Попробуйте снова.")
        return
    main_bot.send_message(chat_id, "Сколько часов будет занято?", reply_markup=types.ReplyKeyboardRemove())
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
    end_hour = start_hour + hours
    if end_hour > 24 or (start_hour == 23 and hours > 2):
        main_bot.send_message(chat_id, "Недопустимое количество часов для выбранного времени.")
        return
    schedule = get_schedule_for_day(selected_day)
    schedule_dict = {t: b for t, b, _ in schedule}
    conflict = any(schedule_dict.get(f"{start_hour + i}:00", 0) != 0 for i in range(hours))
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
    main_bot.send_message(chat_id, "Введите ваш номер телефона или другой контакт:")

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
    main_bot.send_message(chat_id, "Выберите тип брони:", reply_markup=keyboard)

@main_bot.message_handler(func=lambda msg: user_states.get(msg.chat.id) == 'waiting_for_booking_type')
def handle_booking_type_selection(message):
    chat_id = message.chat.id
    if message.text == "Другое":
        main_bot.send_message(chat_id, "Введите тип брони:", reply_markup=types.ReplyKeyboardRemove())
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
    main_bot.send_message(chat_id, "Введите комментарий или нажмите 'Ок' для пропуска:", reply_markup=keyboard)

@main_bot.message_handler(func=lambda msg: user_states.get(msg.chat.id) == 'waiting_for_comment')
def handle_comment_input(message):
    chat_id = message.chat.id
    comment = "" if message.text == "Ок" else message.text.strip()
    selected_day = user_states.get(f"{chat_id}_selected_day")
    selected_time = user_states.get(f"{chat_id}_selected_time")
    hours = user_states.get(f"{chat_id}_hours")
    group_name = user_states.get(f"{chat_id}_group_name")
    booking_type = user_states.get(f"{chat_id}_booking_type")
    contact_info = user_states.get(f"{chat_id}_contact_info")
    start_hour = int(selected_time.split(":")[0])
    end_hour = start_hour + hours
    end_time = f"{end_hour}:00"
    book_slots(selected_day, selected_time, hours, chat_id, group_name, booking_type, comment, contact_info)
    main_bot.send_message(chat_id, f"Вы забронировали: {selected_day} {selected_time}-{end_time} - '{group_name}'", parse_mode='Markdown')
    mention = f"[{message.from_user.first_name}](tg://user?id={message.from_user.id})"
    note = f"🔔 Новая бронь!\nДата: {selected_day}\nВремя: {selected_time}-{end_time}\nГруппа: {group_name}\nТип: {booking_type}\nКомментарий: {comment}\nКонтакт: {contact_info}\nСоздатель: {mention}"
    for admin_id in ADMIN_IDS:
        try:
            admin_bot.send_message(admin_id, note, parse_mode='Markdown', reply_markup=create_confirmation_keyboard(selected_day, selected_time))
        except Exception as e:
            print(f"[Error] Can't send message to admin {admin_id}: {e}")
    keyboard = types.ReplyKeyboardMarkup(resize_keyboard=True)
    keyboard.add(types.KeyboardButton("Забронировать другое время"))
    keyboard.add(types.KeyboardButton("Вернуться на главную"))
    main_bot.send_message(chat_id, "Что дальше?", reply_markup=keyboard)
    reset_user_state(chat_id)

@main_bot.message_handler(func=lambda msg: msg.text == "Забронировать другое время")
def book_another_time(message):
    reset_user_state(message.chat.id)
    show_free_days(message)

@main_bot.message_handler(func=lambda msg: msg.text == "Вернуться на главную")
def return_to_main_menu(message):
    reset_user_state(message.chat.id)
    show_main_menu(message)

if __name__ == "__main__":
    init_db()
    main_bot.polling(none_stop=True)