import sqlite3
from datetime import datetime, timedelta
import telebot
from telebot import types
from dotenv import load_dotenv
import os
import re

load_dotenv()

MAIN_BOT_TOKEN = os.getenv("MAIN_BOT_TOKEN")
NOTIFIER_BOT_TOKEN = os.getenv("NOTIFIER_BOT_TOKEN")
ADMIN_IDS = list(map(int, os.getenv("ADMIN_IDS", "").split(",")))

main_bot = telebot.TeleBot(MAIN_BOT_TOKEN)
notifier_bot = telebot.TeleBot(NOTIFIER_BOT_TOKEN)

user_states = {}

def init_db():
    conn = sqlite3.connect('bookings.db')
    cursor = conn.cursor()
    cursor.execute('''
        CREATE TABLE IF NOT EXISTS slots (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            date TEXT NOT NULL,
            time TEXT NOT NULL,
            booked INTEGER DEFAULT 0,
            user_id INTEGER DEFAULT NULL,
            group_name TEXT DEFAULT NULL
        )
    ''')
    cursor.execute('SELECT COUNT(*) FROM slots')
    if cursor.fetchone()[0] == 0:
        times = ["10:00", "11:00", "12:00", "13:00", "14:00", "15:00", "16:00", "17:00"]
        today = datetime.now()
        for i in range(30):
            date = (today + timedelta(days=i)).strftime('%Y-%m-%d')
            for time in times:
                cursor.execute('INSERT INTO slots (date, time, booked) VALUES (?, ?, ?)', (date, time, 0))
    conn.commit()
    conn.close()

def get_free_days():
    conn = sqlite3.connect('bookings.db')
    cursor = conn.cursor()
    cursor.execute('SELECT DISTINCT date FROM slots WHERE booked = 0 ORDER BY date')
    return [row[0] for row in cursor.fetchall()]

def format_date(date_str):
    date_obj = datetime.strptime(date_str, '%Y-%m-%d')
    day_of_week = {0: "ПН", 1: "ВТ", 2: "СР", 3: "ЧТ", 4: "ПТ", 5: "СБ", 6: "ВС"}
    return f"{date_obj.strftime('%d.%m')} {day_of_week[date_obj.weekday()]}"

def get_schedule_for_day(date):
    conn = sqlite3.connect('bookings.db')
    cursor = conn.cursor()
    cursor.execute('SELECT time, booked, group_name FROM slots WHERE date = ? ORDER BY time', (date,))
    return cursor.fetchall()

def book_slot(date, time, user_id, group_name):
    conn = sqlite3.connect('bookings.db')
    cursor = conn.cursor()
    cursor.execute('UPDATE slots SET booked = 1, user_id = ?, group_name = ? WHERE date = ? AND time = ?', 
                   (user_id, group_name, date, time))
    conn.commit()
    conn.close()

def reset_user_state(chat_id):
    user_states.pop(chat_id, None)
    user_states.pop(f"{chat_id}_selected_day", None)
    user_states.pop(f"{chat_id}_selected_time", None)

def show_main_menu(message):
    keyboard = types.ReplyKeyboardMarkup(resize_keyboard=True)
    keyboard.add(types.KeyboardButton("Забронировать время"))
    main_bot.send_message(message.chat.id, "Выберите действие:", reply_markup=keyboard)
    reset_user_state(message.chat.id)

@main_bot.message_handler(commands=['start'])
def start(message):
    show_main_menu(message)

@main_bot.message_handler(func=lambda message: message.text == "Забронировать время")
def book_time(message):
    reset_user_state(message.chat.id)
    show_free_days(message)

def show_free_days(message):
    free_days = get_free_days()
    if not free_days:
        main_bot.send_message(message.chat.id, "К сожалению, все дни заняты.")
        return

    keyboard = types.ReplyKeyboardMarkup(resize_keyboard=True)
    buttons = [types.KeyboardButton(format_date(day)) for day in free_days]
    keyboard.add(*buttons)
    main_bot.send_message(message.chat.id, "Свободные дни:", reply_markup=keyboard)
    user_states[message.chat.id] = 'waiting_for_day'

@main_bot.message_handler(func=lambda message: user_states.get(message.chat.id) == 'waiting_for_day')
def handle_day_selection(message):
    selected_day_formatted = message.text.split()[0].strip()
    free_days = get_free_days()

    try:
        selected_day = datetime.strptime(selected_day_formatted, '%d.%m').replace(year=datetime.now().year).strftime('%Y-%m-%d')
    except ValueError:
        main_bot.send_message(message.chat.id, "Неверный формат даты. Попробуйте снова.")
        return

    if selected_day not in free_days:
        main_bot.send_message(message.chat.id, "Этот день недоступен. Попробуйте снова.")
        return

    schedule = get_schedule_for_day(selected_day)
    schedule_text = "\n".join([f"{time} ({group_name or 'свободно'})" for time, booked, group_name in schedule])
    main_bot.send_message(message.chat.id, f"Расписание на {format_date(selected_day)}:\n{schedule_text}")

    keyboard = types.ReplyKeyboardMarkup(resize_keyboard=True)
    buttons = [types.KeyboardButton(time) for time, booked, _ in schedule if not booked]
    keyboard.add(*buttons)
    keyboard.add(types.KeyboardButton("Выбрать другой день"))
    main_bot.send_message(message.chat.id, "Выберите свободное время или нажмите 'Выбрать другой день':", reply_markup=keyboard)

    user_states[message.chat.id] = 'waiting_for_time'
    user_states[f"{message.chat.id}_selected_day"] = selected_day

@main_bot.message_handler(func=lambda message: user_states.get(message.chat.id) == 'waiting_for_time')
def handle_time_selection(message):
    chat_id = message.chat.id
    selected_day = user_states.get(f"{chat_id}_selected_day")

    if message.text == "Выбрать другой день":
        main_bot.send_message(chat_id, "Возвращаемся к выбору дня...")
        reset_user_state(chat_id)
        show_free_days(message)
        return

    selected_time = message.text.strip()
    schedule = get_schedule_for_day(selected_day)

    for time, booked, _ in schedule:
        if time == selected_time and not booked:
            main_bot.send_message(chat_id, "Введите название группы:")
            user_states[chat_id] = 'waiting_for_group_name'
            user_states[f"{chat_id}_selected_time"] = selected_time
            return

    main_bot.send_message(chat_id, "Это время уже занято или недоступно. Попробуйте снова.")

@main_bot.message_handler(func=lambda message: user_states.get(message.chat.id) == 'waiting_for_group_name')
def handle_group_name_input(message):
    chat_id = message.chat.id
    selected_day = user_states.get(f"{chat_id}_selected_day")
    selected_time = user_states.get(f"{chat_id}_selected_time")

    if message.text == "Выбрать другой день":
        main_bot.send_message(chat_id, "Возвращаемся к выбору дня...")
        reset_user_state(chat_id)
        show_free_days(message)
        return

    group_name = message.text.strip()

    if not group_name:
        main_bot.send_message(chat_id, "Название группы не может быть пустым. Попробуйте снова.")
        return
    if re.search(r"[;'\"]|(--)|(\bOR\b)|(\bAND\b)", group_name, re.IGNORECASE):
        main_bot.send_message(chat_id, "Название группы содержит недопустимые символы. Попробуйте снова.")
        return

    book_slot(selected_day, selected_time, chat_id, group_name)
    main_bot.send_message(chat_id, f"Вы успешно забронировали время: {selected_day} {selected_time} для группы '{group_name}'!")

    notification_text = f"🔔 Новая бронь!\nДата: {selected_day}\nВремя: {selected_time}\nГруппа: {group_name}"
    for admin_id in ADMIN_IDS:
        try:
            notifier_bot.send_message(admin_id, notification_text)
        except Exception:
            pass

    keyboard = types.ReplyKeyboardMarkup(resize_keyboard=True)
    keyboard.add(types.KeyboardButton("Забронировать другое время"))
    keyboard.add(types.KeyboardButton("Вернуться на главную"))
    main_bot.send_message(chat_id, "Что дальше?", reply_markup=keyboard)

@main_bot.message_handler(func=lambda message: message.text == "Забронировать другое время")
def book_another_time(message):
    reset_user_state(message.chat.id)
    show_free_days(message)

@main_bot.message_handler(func=lambda message: message.text == "Вернуться на главную")
def return_to_main_menu(message):
    reset_user_state(message.chat.id)
    show_main_menu(message)

if __name__ == "__main__":
    init_db()
    main_bot.polling(none_stop=True)
