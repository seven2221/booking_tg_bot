import os
import sqlite3

from dotenv import load_dotenv
from datetime import datetime, timedelta
from telebot import types
from telebot.types import InlineKeyboardMarkup, InlineKeyboardButton

load_dotenv()

ADMIN_IDS = list(map(int, os.getenv("ADMIN_IDS", "").split(",")))

today = datetime.now().strftime("%Y-%m-%d")

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

def get_booked_days_filtered():
    conn = sqlite3.connect('bookings.db')
    cursor = conn.cursor()
    cursor.execute("SELECT DISTINCT date FROM slots WHERE time >= '11:00' AND status IN (1, 2)  AND date >= ?", (today,))
    days = [row[0] for row in cursor.fetchall()]
    conn.close()
    return days

def add_subscriber_to_slot(date, time, user_id):
    conn = sqlite3.connect('bookings.db')
    cursor = conn.cursor()
    cursor.execute("SELECT subscribed_users FROM slots WHERE date = ? AND time = ?", (date, time))
    result = cursor.fetchone()

    current_subs = set(result[0].split(',') if result[0] else [])
    if str(user_id) not in current_subs:
        current_subs.add(str(user_id))
        updated_subs = ','.join(current_subs)
        cursor.execute("UPDATE slots SET subscribed_users = ? WHERE date = ? AND time = ?", (updated_subs, date, time))
        conn.commit()
    conn.close()

def send_date_selection_keyboard(chat_id, dates, bot):
    markup = types.ReplyKeyboardMarkup(resize_keyboard=True)
    buttons = [types.KeyboardButton(format_date(d)) for d in dates]
    for i in range(0, len(buttons), 3):
        markup.row(*buttons[i:i+3])
    markup.row(types.KeyboardButton("На главную"))
    bot.send_message(chat_id, "Выберите день для отмены брони:", reply_markup=markup)

def get_grouped_bookings_for_cancellation(date, created_by=None):
    conn = sqlite3.connect('bookings.db')
    cursor = conn.cursor()
    prev_day = (datetime.strptime(date, "%Y-%m-%d") - timedelta(days=1)).strftime("%Y-%m-%d")
    next_day = (datetime.strptime(date, "%Y-%m-%d") + timedelta(days=1)).strftime("%Y-%m-%d")
    query = '''
        SELECT id, date, time, group_name, created_by FROM slots
        WHERE date IN (?, ?, ?)
          AND status IN (1, 2)
    '''
    params = [prev_day, date, next_day]
    if created_by is not None:
        query += ' AND created_by = ?'
        params.append(created_by)
    cursor.execute(query, params)
    rows = cursor.fetchall()
    conn.close()
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
            if (
                booking['group_name'] == current_group['group_name'] and
                booking['user_id'] == current_group['user_id'] and
                booking['datetime'] == current_group['end_time']
            ):
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
    filtered_grouped = [
        g for g in grouped
        if g['start_time'].strftime("%Y-%m-%d") == date
    ]
    return filtered_grouped

def send_booking_selection_keyboard(chat_id, bookings, bot):
    markup = types.ReplyKeyboardMarkup(resize_keyboard=True)
    for idx, group in enumerate(bookings):
        start_time = group['start_time'].strftime("%H:%M")
        end_time = group['end_time'].strftime("%H:%M")
        btn_text = f"{start_time}–{end_time}"
        markup.add(types.KeyboardButton(btn_text))
    markup.row(types.KeyboardButton("⬅️ Выбрать другой день"))
    markup.row(types.KeyboardButton("🏠 На главную"))
    bot.send_message(chat_id, "Выберите бронь для отмены:", reply_markup=markup)

def notify_subscribers_for_cancellation(group, bot):
    conn = sqlite3.connect('bookings.db')
    cursor = conn.cursor()
    ids = group["ids"]
    cursor.execute("SELECT date FROM slots WHERE id IN ({}) LIMIT 1".format(','.join('?' * len(ids))), ids)
    result = cursor.fetchone()
    if not result:
        print("[Error] Не найдено записей для указанных ID.")
        return
    date_str = result[0]
    query = 'SELECT time, subscribed_users FROM slots WHERE id IN ({})'.format(','.join('?' * len(ids)))
    cursor.execute(query, ids)
    results = cursor.fetchall()
    users_to_notify = {}
    for time, subs_str in results:
        if not subs_str:
            continue
        for user_id in subs_str.split(','):
            user_id = user_id.strip()
            if not user_id:
                continue
            if user_id not in users_to_notify:
                users_to_notify[user_id] = []
            users_to_notify[user_id].append(time)
    for user_id, times in users_to_notify.items():
        first = min(times)
        last = max(times)
        try:
            bot.send_message(
                int(user_id),
                f"🔔 Слот освободился:\nДата: {date_str}\nВремя: {first}–{last}"
            )
        except Exception as e:
            print(f"[Error] Can't notify user {user_id}: {e}")
    conn.close()

def notify_booking_cancelled(user_id,bot):
    try:
        bot.send_message(int(user_id), "❌ Ваша бронь была отменена по техническим причинам.\nПриносим свои извинения за доставленные неудобства.\nСвязь с админом: @cyberocalypse")
    except Exception as e:
        print(f"[Error] Не удалось отправить уведомление пользователю {user_id}: {e}")

def clear_booking_slots(slot_ids, bot):
    conn = sqlite3.connect('bookings.db')
    cursor = conn.cursor()
    query = 'SELECT DISTINCT created_by FROM slots WHERE id IN ({})'.format(','.join('?' * len(slot_ids)))
    cursor.execute(query, slot_ids)
    creators = [row[0] for row in cursor.fetchall() if row[0] is not None]
    update_query = '''UPDATE slots SET 
                        user_id = NULL, 
                        group_name = NULL, 
                        created_by = NULL, 
                        booking_type = NULL, 
                        comment = NULL, 
                        contact_info = NULL, 
                        status = 0,
                        subscribed_users = NULL
                      WHERE id IN ({})'''.format(','.join('?' * len(slot_ids)))
    cursor.execute(update_query, slot_ids)
    conn.commit()
    conn.close()
    for user_id in creators:
        notify_booking_cancelled(user_id, bot)

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
    cursor.execute("SELECT DISTINCT date FROM slots WHERE status = 0 AND date >= ? ORDER BY date", (today,))
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