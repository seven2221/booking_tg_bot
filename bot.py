import os
import re
import sqlite3
from datetime import datetime, timedelta
from dotenv import load_dotenv
from PIL import Image, ImageDraw, ImageFont
import textwrap
import telebot
from telebot import types

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
            group_name TEXT DEFAULT NULL,
            created_by INTEGER DEFAULT NULL,
            subscribed_users TEXT DEFAULT NULL
        )
    ''')
    cursor.execute('SELECT COUNT(*) FROM slots')
    if cursor.fetchone()[0] == 0:
        times = [f"{hour}:00" for hour in range(11, 24, 2)]
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
    free_days = [row[0] for row in cursor.fetchall()]
    conn.close()
    return free_days

def format_date(date_str):
    date_obj = datetime.strptime(date_str, '%Y-%m-%d')
    weekdays = ["ПН", "ВТ", "СР", "ЧТ", "ПТ", "СБ", "ВС"]
    return f"{date_obj.strftime('%d.%m')} {weekdays[date_obj.weekday()]}"

def get_schedule_for_day(date):
    conn = sqlite3.connect('bookings.db')
    cursor = conn.cursor()
    cursor.execute('SELECT time, booked, group_name FROM slots WHERE date = ? ORDER BY time', (date,))
    schedule = cursor.fetchall()
    conn.close()
    return schedule

def book_slot(date, time, user_id, group_name):
    conn = sqlite3.connect('bookings.db')
    cursor = conn.cursor()
    cursor.execute('''
        UPDATE slots 
        SET booked = 1, user_id = ?, group_name = ?, created_by = ? 
        WHERE date = ? AND time = ?''',
        (user_id, group_name, user_id, date, time))
    conn.commit()
    conn.close()

def reset_user_state(chat_id):
    keys_to_remove = [chat_id, f"{chat_id}_selected_day", f"{chat_id}_selected_time"]
    for key in keys_to_remove:
        user_states.pop(key, None)

def create_schedule_grid_image():
    conn = sqlite3.connect('bookings.db')
    cursor = conn.cursor()
    cursor.execute('SELECT DISTINCT date FROM slots ORDER BY date LIMIT 14')
    dates = [row[0] for row in cursor.fetchall()]
    schedules = {date: get_schedule_for_day(date) for date in dates}
    conn.close()

    cell_width, cell_height, padding = 450, 60, 10
    time_font_size, group_font_size = 18, 16

    try:
        font_path = "/usr/share/fonts/truetype/dejavu/DejaVuSans.ttf"
        time_font = ImageFont.truetype(font_path, time_font_size)
        group_font = ImageFont.truetype(font_path, group_font_size)
        header_font = ImageFont.truetype(font_path.replace("Sans.ttf", "Sans-Bold.ttf"), group_font_size + 4)
    except OSError:
        time_font = group_font = header_font = ImageFont.load_default()

    num_rows_per_day = len(schedules[dates[0]]) + 2 if schedules else 3
    img_width = 7 * (cell_width + padding) + padding
    img_height = 2 * (num_rows_per_day * (cell_height + padding) + padding)

    img = Image.new("RGB", (img_width, img_height), color="white")
    draw = ImageDraw.Draw(img)

    for row_offset in range(2):
        for col, date in enumerate(dates[row_offset * 7:(row_offset + 1) * 7]):
            x = padding + col * (cell_width + padding)
            y = padding + row_offset * (num_rows_per_day * (cell_height + padding))
            draw.rectangle([x, y, x + cell_width, y + cell_height + padding], fill=(200, 200, 200))
            draw.text((x + padding, y + padding), format_date(date), fill="black", font=header_font)

    for row, (time, _, _) in enumerate(schedules[dates[0]], start=1):
        for row_offset in range(2):
            for col, date in enumerate(dates[row_offset * 7:(row_offset + 1) * 7]):
                schedule = schedules[date]
                time_data = next((t for t in schedule if t[0] == time), (time, 0, None))
                booked, group_name = time_data[1], time_data[2] or "-"
                bg_color = (255, 240, 200) if booked else (200, 255, 200)
                x = padding + col * (cell_width + padding)
                y = padding + row_offset * (num_rows_per_day * (cell_height + padding)) + row * (cell_height + padding)
                draw.rectangle([x, y, x + cell_width, y + cell_height], fill=bg_color, outline="black", width=1)
                draw.text((x + padding, y + (cell_height - time_font_size) // 2), time, fill="black", font=time_font)
                group_lines = textwrap.wrap(group_name, width=30)
                group_y = y + (cell_height - len(group_lines) * (group_font_size + 2)) // 2
                for line in group_lines:
                    draw.text((x + cell_width // 4 + padding, group_y), line, fill="black", font=group_font)
                    group_y += group_font_size + 2

    path = "schedule_grid.png"
    img.save(path, dpi=(300, 300))
    return path

def show_main_menu(message):
    keyboard = types.ReplyKeyboardMarkup(resize_keyboard=True)
    keyboard.add(types.KeyboardButton("Забронировать время"))
    keyboard.add(types.KeyboardButton("Посмотреть расписание"))
    keyboard.add(types.KeyboardButton("Отменить бронь"))
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
    path = create_schedule_grid_image()
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
    schedule = get_schedule_for_day(selected_day)
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
    if any(t == selected_time and not b for t, b, _ in get_schedule_for_day(selected_day)):
        main_bot.send_message(chat_id, "Введите название группы:", reply_markup=types.ReplyKeyboardRemove())
        user_states[chat_id] = 'waiting_for_group_name'
        user_states[f"{chat_id}_selected_time"] = selected_time
    else:
        main_bot.send_message(chat_id, "Время занято или недоступно. Попробуйте снова.")

@main_bot.message_handler(func=lambda msg: user_states.get(msg.chat.id) == 'waiting_for_group_name')
def handle_group_name_input(message):
    chat_id = message.chat.id
    selected_day = user_states.get(f"{chat_id}_selected_day")
    selected_time = user_states.get(f"{chat_id}_selected_time")
    group_name = message.text.strip()
    if not group_name or re.search(r"[;'\"]|^\s*/", group_name):
        main_bot.send_message(chat_id, "Некорректное название. Попробуйте снова.")
        return
    book_slot(selected_day, selected_time, chat_id, group_name)
    main_bot.send_message(chat_id, f"Вы забронировали: {selected_day} {selected_time} - '{group_name}'", parse_mode='Markdown')
    mention = f"[{message.from_user.first_name}](tg://user?id={message.from_user.id})"
    note = f"🔔 Новая бронь!\nДата: {selected_day}\nВремя: {selected_time}\nГруппа: {group_name}\nСоздатель: {mention}"
    for admin_id in ADMIN_IDS:
        try:
            notifier_bot.send_message(admin_id, note, parse_mode='Markdown')
        except:
            pass
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
