import os
import re
import time
from datetime import datetime, timedelta
from dotenv import load_dotenv
from lib.db_init import get_connection

load_dotenv()
ADMIN_IDS = list(map(int, os.getenv("ADMIN_IDS", "").split(","))) if os.getenv("ADMIN_IDS") else []


def is_admin(user_id):
    return user_id in ADMIN_IDS


def reset_user_state(chat_id, user_states):
    chat_id_str = str(chat_id)
    keys_to_delete = [key for key in user_states if key == chat_id or str(key).startswith(f"{chat_id}_")]
    for key in keys_to_delete:
        user_states.pop(key, None)


def format_date(date_str):
    date_obj = datetime.strptime(date_str, "%Y-%m-%d")
    weekdays = ["ПН", "ВТ", "СР", "ЧТ", "ПТ", "СБ", "ВС"]
    return f"{date_obj.strftime('%d.%m')} {weekdays[date_obj.weekday()]}"


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


def get_user_id_by_booking(booking_id: int):
    conn = get_connection()
    try:
        cur = conn.cursor()
        cur.execute("SELECT DISTINCT user_id FROM slots WHERE booking_id = %s", (booking_id,))
        row = cur.fetchone()
        return row[0] if row else None
    finally:
        conn.close()


def confirm_booking(booking_id: int):
    conn = get_connection()
    try:
        cur = conn.cursor()
        cur.execute("UPDATE slots SET status = 2 WHERE booking_id = %s", (booking_id,))
        conn.commit()
    finally:
        conn.close()


def reject_booking(booking_id: int):
    conn = get_connection()
    try:
        cur = conn.cursor()
        cur.execute("""
            UPDATE slots
            SET status = 0,
                user_id = NULL,
                group_name = NULL,
                booking_type = NULL,
                comment = NULL,
                contact_info = NULL,
                booking_id = NULL
            WHERE booking_id = %s
        """, (booking_id,))
        conn.commit()
    finally:
        conn.close()


def clear_booking_slots(booking_id: int):
    reject_booking(booking_id)


def format_booking_info(group):
    start_time = group["start_time"].strftime("%H:%M") if hasattr(group["start_time"], "strftime") else str(group["start_time"])
    end_time = group["end_time"].strftime("%H:%M") if hasattr(group["end_time"], "strftime") else str(group["end_time"])
    date_str = datetime.strptime(group["date_str"], "%Y-%m-%d").strftime("%d.%m.%Y") if isinstance(group["date_str"], str) else group["date_str"].strftime("%d.%m.%Y")
    user_val = group.get("user_id", "")
    user_label = f"@{user_val}" if isinstance(user_val, str) and user_val else (str(user_val) if user_val else "—")
    return (
        f"Дата: {date_str}\n"
        f"Время: {start_time}–{end_time}\n"
        f"Группа: {group.get('group_name') or '—'}\n"
        f"Контакт: {user_label}"
    )


def update_booking_status(date, time, status):
    conn = get_connection()
    try:
        cur = conn.cursor()
        cur.execute(
            "UPDATE slots SET status = %s WHERE date = %s AND time = %s",
            (int(status), date, time),
        )
        conn.commit()
    finally:
        conn.close()


def book_slots(date_str, start_time, hours, user_id, group_name, booking_type, comment, contact_info):
    booking_id = int(time.time())
    date_obj = datetime.strptime(date_str, "%Y-%m-%d")
    start_hour = int(start_time.split(":")[0])
    conn = get_connection()
    try:
        cur = conn.cursor()
        for i in range(hours):
            cur_hour = (start_hour + i) % 24
            cur_day = (date_obj + timedelta(days=(start_hour + i) // 24)).strftime("%Y-%m-%d")
            cur.execute("INSERT INTO slots (date, time, user_id, group_name, booking_type, comment, contact_info, status, booking_id) VALUES (%s, %s, %s, %s, %s, %s, %s, 1, %s)", (
                cur_day,
                f"{cur_hour:02d}:00",
                user_id,
                group_name,
                booking_type,
                comment,
                contact_info,
                booking_id
            ))
        conn.commit()
    finally:
        conn.close()
    return booking_id

