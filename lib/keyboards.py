import sqlite3
from telebot import types
from telebot.types import InlineKeyboardMarkup, InlineKeyboardButton
from lib.utils import get_user_id_from_booking_ids, format_date

def send_booking_selection_keyboard(chat_id, bookings, bot):
    markup = types.ReplyKeyboardMarkup(resize_keyboard=True)
    for idx, group in enumerate(bookings):
        start_time = group['start_time'].strftime("%H:%M")
        end_time = group['end_time'].strftime("%H:%M")
        group_name = group.get('group_name', 'Без названия')
        btn_text = f"{start_time}–{end_time}, {group_name}"
        markup.add(types.KeyboardButton(btn_text))
    markup.row(types.KeyboardButton("Выбрать другой день"))
    markup.row(types.KeyboardButton("На главную"))
    bot.send_message(chat_id, "Выберите бронь для отмены:", reply_markup=markup)

def send_date_selection_keyboard(chat_id, dates, bot):
    markup = types.ReplyKeyboardMarkup(resize_keyboard=True)
    buttons = [types.KeyboardButton(format_date(d)) for d in dates]
    for i in range(0, len(buttons), 3):
        markup.row(*buttons[i:i+3])
    markup.row(types.KeyboardButton("На главную"))
    bot.send_message(chat_id, "Выберите день для отмены брони:", reply_markup=markup)

def create_confirmation_keyboard(selected_day, selected_time, booking_ids=None):
    keyboard = InlineKeyboardMarkup()
    if not booking_ids:
        conn = sqlite3.connect('bookings.db')
        cursor = conn.cursor()
        cursor.execute('''
            SELECT created_by FROM slots 
            WHERE date = ? AND time = ?
        ''', (selected_day, selected_time))
        creator_row = cursor.fetchone()
        if not creator_row:
            conn.close()
            return None
        creator_id = creator_row[0]
        cursor.execute('''
            SELECT id, created_by, date, time 
            FROM slots 
            WHERE created_by = ? 
            ORDER BY date, time
        ''', (creator_id,))
        rows = cursor.fetchall()
        conn.close()
        if not rows:
            return None
        bookings = []
        for row in rows:
            bid, user_id, date_str, time_str = row
            try:
                dt = datetime.strptime(f"{date_str} {time_str}", "%Y-%m-%d %H:%M")
            except ValueError:
                continue
            bookings.append({'id': bid})
        grouped = []
        current_group = None
        for booking in bookings:
            if not current_group:
                current_group = {'ids': [booking['id']]}
            else:
                current_group['ids'].append(booking['id'])
        if current_group:
            grouped.append(current_group)
        if not grouped:
            return None
        booking_ids = grouped[0]['ids']
    user_id = get_user_id_from_booking_ids(booking_ids)
    keyboard.row(
        InlineKeyboardButton("✅ Подтвердить", callback_data=f"confirm:{','.join(map(str, booking_ids))}:{user_id}"),
        InlineKeyboardButton("❌ Отклонить", callback_data=f"reject:{','.join(map(str, booking_ids))}:{user_id}")
    )
    return keyboard

def create_cancellation_keyboard(selected_day, selected_time, booking_ids=None):
    keyboard = InlineKeyboardMarkup()
    if not booking_ids:
        conn = sqlite3.connect('bookings.db')
        cursor = conn.cursor()
        cursor.execute('''
            SELECT created_by FROM slots 
            WHERE date = ? AND time = ?
        ''', (selected_day, selected_time))
        creator_row = cursor.fetchone()
        if not creator_row:
            conn.close()
            return None
        creator_id = creator_row[0]
        cursor.execute('''
            SELECT id, created_by, date, time 
            FROM slots 
            WHERE created_by = ? 
            ORDER BY date, time
        ''', (creator_id,))
        rows = cursor.fetchall()
        conn.close()
        if not rows:
            return None
        bookings = []
        for row in rows:
            bid, user_id, date_str, time_str = row
            try:
                dt = datetime.strptime(f"{date_str} {time_str}", "%Y-%m-%d %H:%M")
            except ValueError:
                continue
            bookings.append({'id': bid})
        grouped = []
        current_group = None
        for booking in bookings:
            if not current_group:
                current_group = {'ids': [booking['id']]}
            else:
                current_group['ids'].append(booking['id'])
        if current_group:
            grouped.append(current_group)
        if not grouped:
            return None
        booking_ids = grouped[0]['ids']
    user_id = get_user_id_from_booking_ids(booking_ids)
    keyboard.row(
        InlineKeyboardButton("🚫 Подтвердить отмену", callback_data=f"cancel:{','.join(map(str, booking_ids))}:{user_id}")
    )
    return keyboard