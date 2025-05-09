import os
import sqlite3

from dotenv import load_dotenv
from datetime import datetime

load_dotenv()

ADMIN_IDS = list(map(int, os.getenv("ADMIN_IDS", "").split(",")))

def is_admin(user_id):
    return user_id in ADMIN_IDS

def format_date(date_str):
    date_obj = datetime.strptime(date_str, '%Y-%m-%d')
    weekdays = ["ПН", "ВТ", "СР", "ЧТ", "ПТ", "СБ", "ВС"]
    return f"{date_obj.strftime('%d.%m')} {weekdays[date_obj.weekday()]}"

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