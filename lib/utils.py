import os
import re
import sqlite3
from dotenv import load_dotenv
from datetime import datetime, timedelta

load_dotenv()

ADMIN_IDS = list(map(int, os.getenv("ADMIN_IDS", "").split(",")))

def is_admin(user_id):
    return user_id in ADMIN_IDS

def reset_user_state(chat_id, user_states):
    chat_id_str = str(chat_id)
    keys_to_delete = [
        key for key in user_states
        if key == chat_id or str(key).startswith(f"{chat_id}_")
    ]
    for key in keys_to_delete:
        user_states.pop(key, None)

def format_date(date_str):
    date_obj = datetime.strptime(date_str, '%Y-%m-%d')
    weekdays = ["ПН", "ВТ", "СР", "ЧТ", "ПТ", "СБ", "ВС"]
    return f"{date_obj.strftime('%d.%m')} {weekdays[date_obj.weekday()]}"

def format_date_to_db(date_str):
    day_month, _ = date_str.split()
    year = datetime.now().year
    date_obj = datetime.strptime(f"{day_month}.{year}", "%d.%m.%Y")
    return date_obj.strftime("%Y-%m-%d")

def validate_input(value, max_length=100):
    if not value:
        return False
    if len(value) > max_length:
        return False
    if re.search(r"[;'\"\\/*]|^\s*/", value):
        return False
    return True

def get_hour_word(hours):
    if 11 <= hours % 100 <= 14:
        return "часов"
    elif hours % 10 == 1:
        return "час"
    elif 2 <= hours % 10 <= 4:
        return "часа"
    else:
        return "часов"

def get_user_id_from_booking_ids(booking_ids):
    with sqlite3.connect('bookings.db') as conn:
        cursor = conn.cursor()
        query = 'SELECT created_by FROM slots WHERE id IN ({})'.format(','.join('?' * len(booking_ids)))
        cursor.execute(query, booking_ids)
        result = cursor.fetchone()
        return result[0] if result else None

def confirm_booking(booking_ids):
    with sqlite3.connect('bookings.db') as conn:
        cursor = conn.cursor()
        placeholders = ','.join('?' * len(booking_ids))
        query = f'UPDATE slots SET status = 2 WHERE id IN ({placeholders})'
        cursor.execute(query, booking_ids)
        conn.commit()

def reject_booking(booking_ids):
    with sqlite3.connect('bookings.db') as conn:
        cursor = conn.cursor()
        placeholders = ','.join('?' * len(booking_ids))
        query = f'''UPDATE slots SET 
                    user_id = NULL, 
                    group_name = NULL, 
                    created_by = NULL, 
                    booking_type = NULL, 
                    comment = NULL, 
                    contact_info = NULL, 
                    subscribed_users = NULL,
                    status = 0 
                  WHERE id IN ({placeholders})'''
        cursor.execute(query, booking_ids)
        conn.commit()

def format_booking_info(group):
    start_time = group['start_time'].strftime("%H:%M")
    end_time = group['end_time'].strftime("%H:%M")
    date_str = datetime.strptime(group['date_str'], "%Y-%m-%d").strftime("%d.%m.%Y")
    return f"Дата: {date_str}\n"\
           f"Время: {start_time}–{end_time}\n"\
           f"Группа: {group['group_name']}\n"\
           f"Контакт: @{group['user_id']}"
           
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

def book_slots(date, start_time, hours, user_id, group_name, booking_type, comment, contact_info):
    conn = sqlite3.connect('bookings.db', check_same_thread=False)
    cursor = conn.cursor()
    start_hour = int(start_time.split(":")[0])
    date_obj = datetime.strptime(date, "%Y-%m-%d")
    for i in range(hours):
        current_hour = start_hour + i
        days_passed = current_hour // 24
        hour_in_day = current_hour % 24
        current_date = date_obj + timedelta(days=days_passed)
        time = f"{hour_in_day:02d}:00"
        current_date_str = current_date.strftime("%Y-%m-%d")
        cursor.execute('''
            UPDATE slots 
            SET user_id = ?, group_name = ?, created_by = ?, booking_type = ?, comment = ?, contact_info = ?, status = 1
            WHERE date = ? AND time = ?''',
            (user_id, group_name, user_id, booking_type, comment, contact_info, current_date_str, time))
    conn.commit()
    conn.close()
