import sqlite3
from datetime import datetime, timedelta
import telebot
from telebot import types
from dotenv import load_dotenv
import os

# Загрузка переменных окружения из .env файла
load_dotenv()

# Чтение токенов и списка администраторов из .env
MAIN_BOT_TOKEN = os.getenv("MAIN_BOT_TOKEN")  # Токен основного бота
NOTIFIER_BOT_TOKEN = os.getenv("NOTIFIER_BOT_TOKEN")  # Токен notifier_bot
ADMIN_IDS = list(map(int, os.getenv("ADMIN_IDS", "").split(",")))  # Список ID администраторов

# Инициализация базы данных
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
    # Генерация слотов на 30 дней вперед (если таблица пустая)
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

# Получение свободных дней
def get_free_days():
    conn = sqlite3.connect('bookings.db')
    cursor = conn.cursor()
    cursor.execute('SELECT DISTINCT date FROM slots WHERE booked = 0 ORDER BY date')
    free_days = [row[0] for row in cursor.fetchall()]
    conn.close()
    return free_days

# Форматирование даты
def format_date(date_str):
    date_obj = datetime.strptime(date_str, '%Y-%m-%d')
    formatted_date = date_obj.strftime('%d.%m (%A)')  # Убран год, оставлен только день и месяц
    return formatted_date

# Получение расписания для выбранного дня
def get_schedule_for_day(date):
    conn = sqlite3.connect('bookings.db')
    cursor = conn.cursor()
    cursor.execute('SELECT time, booked, group_name FROM slots WHERE date = ? ORDER BY time', (date,))
    schedule = cursor.fetchall()
    conn.close()
    return schedule

# Бронирование слота
def book_slot(date, time, user_id, group_name):
    conn = sqlite3.connect('bookings.db')
    cursor = conn.cursor()
    cursor.execute('UPDATE slots SET booked = 1, user_id = ?, group_name = ? WHERE date = ? AND time = ?', 
                   (user_id, group_name, date, time))
    conn.commit()
    conn.close()

# Отмена бронирования
def cancel_booking(date, time):
    conn = sqlite3.connect('bookings.db')
    cursor = conn.cursor()
    cursor.execute('UPDATE slots SET booked = 0, user_id = NULL, group_name = NULL WHERE date = ? AND time = ?', 
                   (date, time))
    conn.commit()
    conn.close()

# Инициализация ботов
main_bot = telebot.TeleBot(MAIN_BOT_TOKEN)
notifier_bot = telebot.TeleBot(NOTIFIER_BOT_TOKEN)

# Словарь для хранения состояний пользователей
user_states = {}

# Команда /start
@main_bot.message_handler(commands=['start'])
def start(message):
    main_bot.send_message(
        message.chat.id,
        "Привет! Я помогу вам забронировать свободное время. "
        "Используйте команду /free, чтобы увидеть доступные дни."
    )

# Команда /free
@main_bot.message_handler(commands=['free'])
def show_free_days(message):
    free_days = get_free_days()
    if free_days:
        keyboard = types.ReplyKeyboardMarkup(resize_keyboard=True, one_time_keyboard=True, row_width=7)
        buttons = []
        for day in free_days:
            formatted_day = format_date(day)
            buttons.append(types.KeyboardButton(formatted_day))
            if len(buttons) == 7:
                keyboard.add(*buttons)
                buttons = []
        if buttons:
            keyboard.add(*buttons)
        main_bot.send_message(message.chat.id, "Свободные дни:", reply_markup=keyboard)
        user_states[message.chat.id] = 'waiting_for_day'
    else:
        main_bot.send_message(message.chat.id, "К сожалению, все дни заняты.")

# Обработка выбора дня
@main_bot.message_handler(func=lambda message: user_states.get(message.chat.id) == 'waiting_for_day')
def handle_day_selection(message):
    selected_day_formatted = message.text.split('(')[0].strip()  # Убираем день недели
    free_days = get_free_days()
    # Преобразуем форматированную дату обратно в YYYY-MM-DD
    try:
        selected_day = datetime.strptime(selected_day_formatted, '%d.%m').replace(year=datetime.now().year).strftime('%Y-%m-%d')
    except ValueError:
        main_bot.send_message(message.chat.id, "Неверный формат даты. Попробуйте снова.")
        return

    if selected_day in free_days:
        schedule = get_schedule_for_day(selected_day)
        schedule_text = "\n".join(
            [f"{time} ({group_name or 'свободно'})" for time, booked, group_name in schedule]
        )
        main_bot.send_message(message.chat.id, f"Расписание на {format_date(selected_day)}:\n{schedule_text}")

        # Создаем клавиатуру с доступными временными слотами
        keyboard = types.ReplyKeyboardMarkup(resize_keyboard=True, one_time_keyboard=True)
        for time, booked, _ in schedule:
            if not booked:
                keyboard.add(types.KeyboardButton(time))  # Добавляем только свободные слоты
        keyboard.add(types.KeyboardButton("Выбрать другой день"))  # Кнопка для возврата к выбору дня
        main_bot.send_message(message.chat.id, "Выберите свободное время или нажмите 'Выбрать другой день':", reply_markup=keyboard)

        user_states[message.chat.id] = 'waiting_for_time'
        user_states[f"{message.chat.id}_selected_day"] = selected_day
    else:
        main_bot.send_message(message.chat.id, "Этот день недоступен. Попробуйте снова.")

# Обработка выбора времени
@main_bot.message_handler(func=lambda message: user_states.get(message.chat.id) == 'waiting_for_time')
def handle_time_selection(message):
    chat_id = message.chat.id
    selected_day = user_states.get(f"{chat_id}_selected_day")

    if message.text == "Выбрать другой день":
        main_bot.send_message(chat_id, "Возвращаемся к выбору дня...")
        user_states[chat_id] = 'waiting_for_day'
        show_free_days(message)
        return

    selected_time = message.text.strip()
    if not selected_day:
        main_bot.send_message(chat_id, "Произошла ошибка. Пожалуйста, начните заново с команды /free.")
        return

    schedule = get_schedule_for_day(selected_day)
    for time, booked, _ in schedule:
        if time == selected_time and not booked:
            main_bot.send_message(chat_id, "Введите название группы:")
            user_states[chat_id] = 'waiting_for_group_name'
            user_states[f"{chat_id}_selected_time"] = selected_time
            return
    main_bot.send_message(chat_id, "Это время уже занято или недоступно. Попробуйте снова.")

# Обработка ввода названия группы
@main_bot.message_handler(func=lambda message: user_states.get(message.chat.id) == 'waiting_for_group_name')
def handle_group_name_input(message):
    chat_id = message.chat.id
    selected_day = user_states.get(f"{chat_id}_selected_day")
    selected_time = user_states.get(f"{chat_id}_selected_time")
    group_name = message.text.strip()

    if not group_name:
        main_bot.send_message(chat_id, "Название группы не может быть пустым. Попробуйте снова.")
        return

    book_slot(selected_day, selected_time, chat_id, group_name)
    main_bot.send_message(chat_id, f"Вы успешно забронировали время: {selected_day} {selected_time} для группы '{group_name}'!")

    # Отправка уведомления администраторам через notifier_bot
    notification_text = f"🔔 Новая бронь!\nДата: {selected_day}\nВремя: {selected_time}\nГруппа: {group_name}"
    for admin_id in ADMIN_IDS:
        notifier_bot.send_message(admin_id, notification_text)

# Команда /cancel (доступна только администраторам)
@main_bot.message_handler(commands=['cancel'])
def cancel_booking_handler(message):
    if message.from_user.id not in ADMIN_IDS:
        main_bot.send_message(message.chat.id, "У вас нет прав для отмены брони.")
        return

    main_bot.send_message(message.chat.id, "Введите дату и время в формате 'DD.MM HH:MM':")
    user_states[message.chat.id] = 'waiting_for_cancel'

# Обработка ввода для отмены брони
@main_bot.message_handler(func=lambda message: user_states.get(message.chat.id) == 'waiting_for_cancel')
def handle_cancel_input(message):
    try:
        date_part, time = message.text.split()
        date = datetime.strptime(date_part, '%d.%m').replace(year=datetime.now().year).strftime('%Y-%m-%d')
        cancel_booking(date, time)
        main_bot.send_message(message.chat.id, f"Бронь на {date_part} {time} успешно отменена.")
    except ValueError:
        main_bot.send_message(message.chat.id, "Неверный формат ввода. Попробуйте снова.")

# Основная функция
if __name__ == "__main__":
    # Инициализация базы данных
    init_db()

    # Запуск основного бота
    main_bot.polling(none_stop=True)