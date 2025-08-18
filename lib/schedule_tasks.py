from datetime import datetime, timedelta

from lib.db_init import get_connection
from lib.utils import is_admin


def get_booked_days_filtered():
    conn = get_connection()
    try:
        cur = conn.cursor()
        current_date = datetime.now().strftime("%Y-%m-%d")
        cur.execute(
            "SELECT DISTINCT date "
            "FROM slots "
            "WHERE time >= '11:00' AND status IN (1, 2) AND date >= %s",
            (current_date,),
        )
        return [row[0] for row in cur.fetchall()]
    finally:
        conn.close()


def get_busy_times_for_day(date_iso: str) -> list[str]:
    conn = get_connection()
    try:
        cur = conn.cursor()
        cur.execute(
            """
            SELECT LEFT(time,5) AS t
            FROM slots
            WHERE date = %s
              AND status IN (1, 2)
              AND LEFT(time,5) >= '11:00'
              AND LEFT(time,5) <= '23:00'
            ORDER BY t
            """,
            (date_iso,),
        )
        return [row[0] for row in cur.fetchall()]
    finally:
        conn.close()


def add_subscriber_to_slot(date: str, time: str, user_id: int):
    conn = get_connection()
    cur = None
    try:
        cur = conn.cursor()
        cur.execute(
            "SELECT subscribed_users FROM slots WHERE date = %s AND LEFT(time,5) = %s LIMIT 1 FOR UPDATE",
            (date, time),
        )
        row = cur.fetchone()
        existing_raw = row[0] if row else ""
        existing = [s.strip() for s in str(existing_raw or "").split(",") if s and s.strip()]
        subs = set(existing)
        subs.add(str(user_id))
        updated = ",".join(sorted(subs))
        cur.execute(
            "UPDATE slots SET subscribed_users = %s WHERE date = %s AND LEFT(time,5) = %s",
            (updated, date, time),
        )
        conn.commit()
    except Exception:
        try:
            conn.rollback()
        except Exception:
            pass
        raise
    finally:
        try:
            if cur is not None:
                cur.close()
        except Exception:
            pass
        try:
            conn.close()
        except Exception:
            pass


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
                booking_id = NULL,
                mention = NULL
            WHERE booking_id = %s
        """, (booking_id,))
        conn.commit()
    finally:
        conn.close()


def get_schedule_for_day(date: str, user_id=None):
    conn = get_connection()
    try:
        cur = conn.cursor()
        cur.execute(
            "SELECT time, status, booking_id, group_name FROM slots WHERE date = %s ORDER BY time",
            (date,),
        )
        schedule = []
        admin = is_admin(user_id)
        for time_str, status, booking_id, group_name in cur.fetchall():
            if (status or 0) > 0 and not admin:
                schedule.append((time_str, status, booking_id, "Занято"))
            else:
                schedule.append((time_str, status, booking_id, group_name))
        return schedule
    finally:
        conn.close()


def get_free_days():
    conn = get_connection()
    try:
        cur = conn.cursor()
        today = datetime.now().date()
        now_time = datetime.now()
        date_list = [(today + timedelta(days=i)).strftime("%Y-%m-%d") for i in range(28)]
        free_days = []
        for date_str in date_list:
            is_today = date_str == today.strftime("%Y-%m-%d")
            if is_today:
                current_hour = now_time.hour
                cur.execute(
                    "SELECT COUNT(*) FROM slots "
                    "WHERE date = %s AND status != 0 AND time >= %s",
                    (date_str, f"{current_hour:02d}:00"),
                )
            else:
                cur.execute(
                    "SELECT COUNT(*) FROM slots WHERE date = %s AND status != 0",
                    (date_str,),
                )
            (count,) = cur.fetchone()
            count = int(count or 0)
            if count == 0 or count < 13:
                free_days.append(date_str)
        return free_days
    finally:
        conn.close()


def get_daily_schedule_from_db(date: str):
    conn = get_connection()
    try:
        cur = conn.cursor()
        cur.execute(
            "SELECT time, status, group_name, booking_type, comment "
            "FROM slots WHERE date = %s ORDER BY time",
            (date,),
        )
        rows = cur.fetchall()
    finally:
        conn.close()
    schedule = []
    for time_str, status, group_name, booking_type, comment in rows:
        st = int(status or 0)
        schedule.append(
            {
                "time": time_str,
                "status": st,
                "group_name": group_name if st > 0 else "",
                "booking_type": booking_type if st > 0 else "",
                "comment": comment if st > 0 else "",
            }
        )
    return schedule


def prepare_daily_schedule_data(date: str):
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
                    "comment": slot["comment"],
                }
            elif (
                current_group["group_name"] == slot["group_name"]
                and current_group["booking_type"] == slot["booking_type"]
                and current_group["comment"] == slot["comment"]
            ):
                current_group["end_time"] = slot["time"]
            else:
                grouped_slots.append(current_group)
                current_group = {
                    "start_time": slot["time"],
                    "end_time": slot["time"],
                    "group_name": slot["group_name"],
                    "booking_type": slot["booking_type"],
                    "comment": slot["comment"],
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


def _fetch_bookings_for_range(dates_list, where_status="status IN (1, 2)"):
    if not dates_list:
        return []
    placeholders = ",".join(["%s"] * len(dates_list))
    conn = get_connection()
    try:
        cur = conn.cursor()
        cur.execute(
            f"SELECT id, date, time, group_name, created_by, booking_type, comment "
            f"FROM slots WHERE date IN ({placeholders}) AND {where_status} "
            f"ORDER BY date, time",
            tuple(dates_list),
        )
        rows = cur.fetchall()
    finally:
        conn.close()
    bookings = []
    for bid, date_str, time_str, group_name, user_id, booking_type, comment in rows:
        try:
            dt = datetime.strptime(f"{date_str} {time_str}", "%Y-%m-%d %H:%M")
        except ValueError:
            continue
        bookings.append(
            {
                "id": bid,
                "datetime": dt,
                "date_str": date_str,
                "time_str": time_str,
                "group_name": group_name,
                "user_id": user_id,
                "booking_type": booking_type,
                "comment": comment,
            }
        )
    return bookings


def _group_contiguous(bookings):
    grouped = []
    current = None
    for b in bookings:
        if not current:
            current = {
                "start_time": b["datetime"],
                "end_time": b["datetime"] + timedelta(hours=1),
                "ids": [b["id"]],
                "group_name": b["group_name"],
                "user_id": b["user_id"],
                "booking_type": b.get("booking_type"),
                "comment": b.get("comment"),
                "date_str": b["date_str"],
            }
        else:
            if (
                b["group_name"] == current["group_name"]
                and b["user_id"] == current["user_id"]
                and b["datetime"] == current["end_time"]
                and b.get("booking_type") == current.get("booking_type")
                and b.get("comment") == current.get("comment")
            ):
                current["end_time"] += timedelta(hours=1)
                current["ids"].append(b["id"])
            else:
                grouped.append(current)
                current = {
                    "start_time": b["datetime"],
                    "end_time": b["datetime"] + timedelta(hours=1),
                    "ids": [b["id"]],
                    "group_name": b["group_name"],
                    "user_id": b["user_id"],
                    "booking_type": b.get("booking_type"),
                    "comment": b.get("comment"),
                    "date_str": b["date_str"],
                }
    if current:
        grouped.append(current)
    return grouped


def get_grouped_daily_bookings(date: str):
    target = datetime.strptime(date, "%Y-%m-%d")
    prev_day = (target - timedelta(days=1)).strftime("%Y-%m-%d")
    next_day = (target + timedelta(days=1)).strftime("%Y-%m-%d")
    bookings = _fetch_bookings_for_range([prev_day, date, next_day])
    grouped = _group_contiguous(bookings)
    filtered = [g for g in grouped if g["start_time"].strftime("%Y-%m-%d") == date]
    return filtered


def get_grouped_unconfirmed_bookings():
    conn = get_connection()
    try:
        cur = conn.cursor(dictionary=True)
        cur.execute("""
            SELECT
                booking_id,
                MIN(date) AS date_str,
                MIN(time) AS start_time,
                MAX(time) AS end_time,
                ANY_VALUE(group_name)   AS group_name,
                ANY_VALUE(booking_type) AS booking_type,
                ANY_VALUE(comment)      AS comment,
                ANY_VALUE(contact_info) AS contact_info
            FROM slots
            WHERE status = 1
            GROUP BY booking_id
            ORDER BY date_str, start_time
        """)
        return cur.fetchall()
    finally:
        conn.close()


def get_grouped_bookings_for_cancellation(date_str: str):
    conn = get_connection()
    try:
        cur = conn.cursor(dictionary=True)
        cur.execute("""
            SELECT
                booking_id,
                MIN(time) AS start_time,
                MAX(time) AS end_time,
                ANY_VALUE(group_name) AS group_name
            FROM slots
            WHERE date = %s AND status IN (1, 2)
            GROUP BY booking_id
            ORDER BY start_time
        """, (date_str,))
        return cur.fetchall()
    finally:
        conn.close()