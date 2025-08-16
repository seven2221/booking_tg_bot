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


# def reject_booking(booking_id: int):
#     conn = get_connection()
#     try:
#         cur = conn.cursor()
#         cur.execute("""
#             UPDATE slots
#             SET status = 0,
#                 user_id = NULL,
#                 group_name = NULL,
#                 booking_type = NULL,
#                 comment = NULL,
#                 contact_info = NULL,
#                 booking_id = NULL,
#                 mention = NULL
#             WHERE booking_id = %s
#         """, (booking_id,))
#         conn.commit()
#     finally:
#         conn.close()


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


def book_slots(date_str, start_time, hours, user_id, group_name, booking_type, comment, contact_info, mention=None):
    booking_id = int(time.time())
    date_obj = datetime.strptime(date_str, "%Y-%m-%d")
    start_hour = int(start_time.split(":")[0])
    conn = get_connection()
    try:
        cur = conn.cursor()
        for i in range(hours):
            cur_hour = (start_hour + i) % 24
            cur_day = (date_obj + timedelta(days=(start_hour + i) // 24)).strftime("%Y-%m-%d")
            cur.execute("""
                UPDATE slots
                SET user_id = %s,
                    group_name = %s,
                    booking_type = %s,
                    comment = %s,
                    contact_info = %s,
                    mention = %s,
                    status = 1,
                    booking_id = %s
                WHERE date = %s AND time = %s
                  AND status = 0
            """, (
                user_id,
                group_name,
                booking_type,
                comment,
                contact_info,
                mention,
                booking_id,
                cur_day,
                f"{cur_hour:02d}:00"
            ))
        conn.commit()
    finally:
        conn.close()
    return booking_id


def escape_markdown(text: str) -> str:
    if not isinstance(text, str):
        return text
    return text.replace("_", "\\_").replace("*", "\\*")


def get_booking_info_by_id(booking_id: int):
    conn = get_connection()
    try:
        cur = conn.cursor(dictionary=True)
        cur.execute(
            """
            SELECT *
            FROM slots
            WHERE booking_id = %s
            ORDER BY date, time
            """,
            (booking_id,)
        )
        rows = cur.fetchall()
        if not rows:
            return None
        first_slot = rows[0]
        last_slot = rows[-1]
        start_date = first_slot["date"]
        if isinstance(start_date, str):
            start_date = datetime.strptime(start_date, "%Y-%m-%d").date()
        start_dt = datetime.combine(start_date, datetime.strptime(first_slot["time"], "%H:%M").time())
        end_date = last_slot["date"]
        if isinstance(end_date, str):
            end_date = datetime.strptime(end_date, "%Y-%m-%d").date()
        end_dt = datetime.combine(end_date, datetime.strptime(last_slot["time"], "%H:%M").time()) + timedelta(hours=1)
        booking_info = dict(first_slot)
        booking_info["date"] = start_dt.strftime("%d.%m.%Y")
        booking_info["start_time"] = start_dt.strftime("%H:%M")
        booking_info["end_time"] = end_dt.strftime("%H:%M" if end_dt.date() == start_dt.date() else "%H:%M %d.%m.%Y")
        return booking_info
    finally:
        conn.close()


def get_slot_ids_by_booking(booking_id: int) -> list[int]:
    conn = get_connection()
    try:
        cur = conn.cursor()
        cur.execute("SELECT id FROM slots WHERE booking_id = %s", (booking_id,))
        rows = cur.fetchall()
    finally:
        conn.close()
    ids = []
    for r in rows:
        v = r[0] if isinstance(r, (list, tuple)) else r
        try:
            ids.append(int(v))
        except Exception:
            continue
    return ids