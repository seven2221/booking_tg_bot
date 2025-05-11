import os
import sqlite3

from dotenv import load_dotenv
from datetime import datetime, timedelta
from telebot.types import InlineKeyboardMarkup, InlineKeyboardButton

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

def get_booked_days_filtered():
    conn = sqlite3.connect('bookings.db')
    cursor = conn.cursor()
    cursor.execute('''
        SELECT DISTINCT date FROM slots 
        WHERE time >= '11:00' AND status IN (1, 2)
    ''')
    days = [row[0] for row in cursor.fetchall()]
    conn.close()
    return days

def add_subscriber_to_slot(date, time, user_id):
    conn = sqlite3.connect('bookings.db')
    cursor = conn.cursor()
    cursor.execute('''
        SELECT subscribed_users FROM slots
        WHERE date = ? AND time = ?
    ''', (date, time))
    result = cursor.fetchone()

    current_subs = set(result[0].split(',') if result[0] else [])
    if str(user_id) not in current_subs:
        current_subs.add(str(user_id))
        updated_subs = ','.join(current_subs)
        cursor.execute('''
            UPDATE slots SET subscribed_users = ?
            WHERE date = ? AND time = ?
        ''', (updated_subs, date, time))
        conn.commit()
    conn.close()

# def notify_subscribers(date, time, freed_by_admin=False):
#     conn = sqlite3.connect('bookings.db')
#     cursor = conn.cursor()
#     cursor.execute('''
#         SELECT subscribed_users FROM schedule
#         WHERE date = ? AND time = ?
#     ''', (date, time))
#     result = cursor.fetchone()
#     conn.close()

#     if not result or not result[0]:
#         return

#     subscribers = result[0].split(',')
#     for user_id in subscribers:
#         try:
#             main_bot.send_message(int(user_id), f"🔔 Внимание! Освободилось время:\nДата: {date}\nВремя: {time}")
#         except Exception as e:
#             print(f"[Error] Can't notify user {user_id}: {e}")

def get_schedule_for_day(date, user_id=None):
    conn = sqlite3.connect('bookings.db', check_same_thread=False)
    cursor = conn.cursor()
    cursor.execute('SELECT time, status, group_name FROM slots WHERE date = ? ORDER BY time', (date,))
    schedule = []
    for row in cursor.fetchall():
        time, status, group_name = row
        if status > 0 and not is_admin(user_id):
            schedule.append((time, True, "Занято"))
        else:
            schedule.append((time, status > 0, group_name))
    conn.close()
    return schedule

def get_hour_word(hours):
    if 11 <= hours % 100 <= 14:
        return "часов"
    elif hours % 10 == 1:
        return "час"
    elif 2 <= hours % 10 <= 4:
        return "часа"
    else:
        return "часов"

def get_grouped_unconfirmed_bookings():
    with sqlite3.connect('bookings.db') as conn:
        cursor = conn.cursor()
        cursor.execute('''
            SELECT id, date, time, group_name, created_by FROM slots
            WHERE status = 1
            ORDER BY date, time
        ''')
        rows = cursor.fetchall()

    bookings = []
    for row in rows:
        bid, date_str, time_str, group_name, user_id = row
        try:
            dt = datetime.strptime(f"{date_str} {time_str}", "%Y-%m-%d %H:%M")
        except ValueError:
            continue
        bookings.append({
            'id': bid,
            'datetime': dt,
            'date_str': date_str,
            'time_str': time_str,
            'group_name': group_name,
            'user_id': user_id
        })
    grouped = []
    current_group = None
    for booking in bookings:
        if not current_group:
            current_group = {
                'start_time': booking['datetime'],
                'end_time': booking['datetime'] + timedelta(hours=1),
                'ids': [booking['id']],
                'group_name': booking['group_name'],
                'user_id': booking['user_id'],
                'date_str': booking['date_str']
            }
        else:
            if (booking['group_name'] == current_group['group_name'] and
                booking['user_id'] == current_group['user_id'] and
                booking['datetime'] == current_group['end_time']):
                current_group['end_time'] += timedelta(hours=1)
                current_group['ids'].append(booking['id'])
            else:
                grouped.append(current_group)
                current_group = {
                    'start_time': booking['datetime'],
                    'end_time': booking['datetime'] + timedelta(hours=1),
                    'ids': [booking['id']],
                    'group_name': booking['group_name'],
                    'user_id': booking['user_id'],
                    'date_str': booking['date_str']
                }
    if current_group:
        grouped.append(current_group)
    return grouped

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

def get_user_id_from_booking_ids(booking_ids):
    with sqlite3.connect('bookings.db') as conn:
        cursor = conn.cursor()
        query = 'SELECT created_by FROM slots WHERE id IN ({})'.format(','.join('?' * len(booking_ids)))
        cursor.execute(query, booking_ids)
        result = cursor.fetchone()
        return result[0] if result else None

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