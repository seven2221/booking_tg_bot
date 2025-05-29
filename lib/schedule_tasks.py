import sqlite3
from datetime import datetime, timedelta
from lib.utils import is_admin

def get_booked_days_filtered():
    conn = sqlite3.connect('bookings.db')
    cursor = conn.cursor()
    current_date = datetime.now().strftime("%Y-%m-%d")
    cursor.execute("SELECT DISTINCT date FROM slots WHERE time >= '11:00' AND status IN (1, 2)  AND date >= ?", (current_date,))
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

def clear_booking_slots(slot_ids, bot):
    conn = sqlite3.connect('bookings.db')
    cursor = conn.cursor()
    update_query = "UPDATE slots SET user_id = NULL, group_name = NULL, created_by = NULL, booking_type = NULL, comment = NULL, contact_info = NULL, status = 0, subscribed_users = NULL WHERE id IN ({})".format(','.join('?' * len(slot_ids)))
    cursor.execute(update_query, slot_ids)
    conn.commit()
    conn.close()

def get_schedule_for_day(date, user_id=None):
    conn = sqlite3.connect('bookings.db', check_same_thread=False)
    cursor = conn.cursor()
    cursor.execute("SELECT time, status, group_name FROM slots WHERE date = ? ORDER BY time", (date,))
    schedule = []
    for row in cursor.fetchall():
        time, status, group_name = row
        if status > 0 and not is_admin(user_id):
            schedule.append((time, True, "Занято"))
        else:
            schedule.append((time, status > 0, group_name))
    conn.close()
    return schedule

def get_free_days():
    conn = sqlite3.connect('bookings.db')
    cursor = conn.cursor()
    today = datetime.now().date()
    now_time = datetime.now()
    date_list = [(today + timedelta(days=i)).strftime("%Y-%m-%d") for i in range(28)]
    free_days = []
    for date_str in date_list:
        is_today = date_str == today.strftime("%Y-%m-%d")
        if is_today:
            current_hour = now_time.hour
            cursor.execute("SELECT COUNT(*) FROM slots WHERE date = ? AND status != 0 AND time >= ?", (date_str, f"{current_hour:02d}:00"))
        else:
            cursor.execute("SELECT COUNT(*) FROM slots WHERE date = ? AND status != 0", (date_str,))
        result = cursor.fetchone()
        if result[0] == 0 or result[0] < 13:
            free_days.append(date_str)
    conn.close()
    return free_days

def get_daily_schedule_from_db(date):
    conn = sqlite3.connect('bookings.db')
    cursor = conn.cursor()
    cursor.execute("SELECT time, status, group_name, booking_type, comment FROM slots WHERE date = ? ORDER BY time", (date,))
    rows = cursor.fetchall()
    conn.close()
    schedule = []
    for row in rows:
        time, status, group_name, booking_type, comment = row
        schedule.append({
            "time": time,
            "status": status,
            "group_name": group_name if status > 0 else "",
            "booking_type": booking_type if status > 0 else "",
            "comment": comment if status > 0 else ""
        })
    return schedule

def prepare_daily_schedule_data(date):
    raw_slots = get_daily_schedule_from_db(date)
    grouped_slots = []
    current_group = None
    for slot in raw_slots:
        if slot["group_name"]:
            if not current_group:
                current_group = {
                    "start_time": slot["time"],
                    "end_time": slot["time"],
                    "group_name": slot["group_name"],
                    "booking_type": slot["booking_type"],
                    "comment": slot["comment"]
                }
            elif (current_group["group_name"] == slot["group_name"] and
                  current_group["booking_type"] == slot["booking_type"] and
                  current_group["comment"] == slot["comment"]):
                current_group["end_time"] = slot["time"]
            else:
                grouped_slots.append(current_group)
                current_group = {
                    "start_time": slot["time"],
                    "end_time": slot["time"],
                    "group_name": slot["group_name"],
                    "booking_type": slot["booking_type"],
                    "comment": slot["comment"]
                }
        else:
            if current_group:
                grouped_slots.append(current_group)
                current_group = None
            grouped_slots.append(slot)
    if current_group:
        grouped_slots.append(current_group)
    final_schedule = []
    for slot in grouped_slots:
        if "time" not in slot:
            slot["time"] = slot.get("start_time", "") or slot.get("end_time", "")
        final_schedule.append(slot)
    return final_schedule

def get_grouped_daily_bookings(date):
    conn = sqlite3.connect('bookings.db')
    cursor = conn.cursor()
    prev_day = (datetime.strptime(date, "%Y-%m-%d") - timedelta(days=1)).strftime("%Y-%m-%d")
    next_day = (datetime.strptime(date, "%Y-%m-%d") + timedelta(days=1)).strftime("%Y-%m-%d")
    query = "SELECT id, date, time, group_name, created_by, booking_type, comment FROM slots WHERE date IN (?, ?, ?) AND status IN (1, 2) ORDER BY date, time"
    params = [prev_day, date, next_day]
    cursor.execute(query, params)
    rows = cursor.fetchall()
    conn.close()
    bookings = []
    for row in rows:
        bid, date_str, time_str, group_name, user_id, booking_type, comment = row
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
            'user_id': user_id,
            'booking_type': booking_type,
            'comment': comment
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
                'booking_type': booking['booking_type'],
                'comment': booking['comment'],
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
                    'booking_type': booking['booking_type'],
                    'comment': booking['comment'],
                    'date_str': booking['date_str']
                }
    if current_group:
        grouped.append(current_group)
    filtered_grouped = [
        g for g in grouped
        if g['start_time'].strftime("%Y-%m-%d") == date
    ]
    return filtered_grouped

def get_grouped_unconfirmed_bookings():
    with sqlite3.connect('bookings.db') as conn:
        cursor = conn.cursor()
        cursor.execute("SELECT id, date, time, group_name, created_by FROM slots WHERE status = 1 ORDER BY date, time")
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

def get_grouped_bookings_for_cancellation(date, created_by=None):
    conn = sqlite3.connect('bookings.db')
    cursor = conn.cursor()
    prev_day = (datetime.strptime(date, "%Y-%m-%d") - timedelta(days=1)).strftime("%Y-%m-%d")
    next_day = (datetime.strptime(date, "%Y-%m-%d") + timedelta(days=1)).strftime("%Y-%m-%d")
    query = "SELECT id, date, time, group_name, created_by FROM slots WHERE date IN (?, ?, ?) AND status IN (1, 2)"
    params = [prev_day, date, next_day]
    if created_by is not None:
        query += " AND created_by = ?"
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