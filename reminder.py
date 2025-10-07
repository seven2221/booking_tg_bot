import os
from datetime import datetime, timedelta
import telebot
from dotenv import load_dotenv
from lib.db_init import get_connection
from lib.utils import get_slot_ids_by_booking, escape_markdown

load_dotenv()

BOT_TOKEN = os.getenv("MAIN_BOT_TOKEN") or os.getenv("BOTTOKEN") or os.getenv("MAINBOTTOKEN")
if not BOT_TOKEN:
    raise RuntimeError("MAIN_BOT_TOKEN (или MAINBOTTOKEN) отсутствует в переменных окружения")

bot = telebot.TeleBot(BOT_TOKEN)


def _to_iso_date(v) -> str:
    if hasattr(v, "strftime"):
        return v.strftime("%Y-%m-%d")
    return str(v)


def _to_hm(v) -> str:
    s = str(v)
    return s[:5]


def _fmt_date_human(iso_date: str) -> str:
    dt = datetime.strptime(iso_date, "%Y-%m-%d")
    return dt.strftime("%d.%m.%Y")


def _select_start_slots_for_moment(cur, date_iso: str, time_hm: str):
    cur.execute(
        """
        SELECT id, booking_id, user_id, group_name
        FROM slots
        WHERE date=%s AND time=%s AND status=2 AND booking_id IS NOT NULL
        """,
        (date_iso, time_hm),
    )
    rows = cur.fetchall()
    if not rows:
        return []
    start_slots = []
    prev_dt = datetime.strptime(f"{date_iso} {time_hm}", "%Y-%m-%d %H:%M") - timedelta(hours=1)
    prev_date = prev_dt.strftime("%Y-%m-%d")
    prev_time = prev_dt.strftime("%H:%M")
    for slot_id, booking_id, user_id, group_name in rows:
        cur.execute(
            """
            SELECT 1 FROM slots
            WHERE date=%s AND time=%s AND booking_id=%s
            LIMIT 1
            """,
            (prev_date, prev_time, booking_id),
        )
        if cur.fetchone():
            continue
        start_slots.append((slot_id, booking_id, user_id, group_name))
    return start_slots


def _calc_span_for_booking(cur, date_iso: str, slot_ids: list[int]) -> tuple[str, str]:
    if not slot_ids:
        return None, None
    placeholders = ",".join(["%s"] * len(slot_ids))
    cur.execute(
        f"""
        SELECT MIN(time), MAX(time)
        FROM slots
        WHERE id IN ({placeholders}) AND date=%s
        """,
        (*slot_ids, date_iso),
    )
    row = cur.fetchone()
    if not row:
        return None, None
    tmin, tmax = row
    if not tmin or not tmax:
        return None, None
    start_hm = _to_hm(tmin)
    last_hm = _to_hm(tmax)
    start_dt = datetime.strptime(f"{date_iso} {start_hm}", "%Y-%m-%d %H:%M")
    end_dt = datetime.strptime(f"{date_iso} {last_hm}", "%Y-%m-%d %H:%M") + timedelta(hours=1)
    return start_dt.strftime("%H:%M"), end_dt.strftime("%H:%M")


def _send_reminder(user_id: int, date_iso: str, start_hm: str, end_hm: str, group_name: str | None):
    date_h = _fmt_date_human(date_iso)
    gn = group_name or ""
    text = f"{date_h} {start_hm}-{end_hm}\n{escape_markdown(gn)}".strip()
    bot.send_message(int(user_id), text, parse_mode="Markdown")


def send_reminders():
    conn = None
    try:
        now = datetime.now()
        targets = [now + timedelta(hours=2), now + timedelta(hours=24)]
        conn = get_connection()
        with conn.cursor() as cur:
            for nt in targets:
                date_iso = nt.strftime("%Y-%m-%d")
                time_hm = nt.strftime("%H:%M")
                start_slots = _select_start_slots_for_moment(cur, date_iso, time_hm)
                if not start_slots:
                    continue
                for _, booking_id, user_id, group_name in start_slots:
                    slot_ids = get_slot_ids_by_booking(int(booking_id)) or []
                    start_hm, end_hm = _calc_span_for_booking(cur, date_iso, slot_ids)
                    if not start_hm or not end_hm:
                        start_hm = time_hm
                        end_hm = (datetime.strptime(f"{date_iso} {time_hm}", "%Y-%m-%d %H:%M") + timedelta(hours=1)).strftime("%H:%M")
                    try:
                        _send_reminder(user_id, date_iso, start_hm, end_hm, group_name)
                    except Exception as e:
                        print(f"Reminder error for booking {booking_id}, user {user_id}: {e}")
    except Exception as e:
        print(f"Reminder run failed: {e}")
    finally:
        try:
            if conn:
                conn.close()
        except Exception:
            pass


if __name__ == "__main__":
    send_reminders()
