import os
from datetime import datetime, timedelta

import telebot
from dotenv import load_dotenv

from lib.db_init import get_connection

load_dotenv()

BOT_TOKEN = os.getenv("MAIN_BOT_TOKEN")
bot = telebot.TeleBot(BOT_TOKEN)


def send_reminders():
    conn = None
    try:
        now = datetime.now()
        notification_times = [now + timedelta(hours=2), now + timedelta(hours=24)]

        conn = get_connection()
        cur = conn.cursor()

        for notification_time in notification_times:
            target_date = notification_time.strftime("%Y-%m-%d")
            target_time = notification_time.strftime("%H:%M")

            cur.execute(
                "SELECT date, time, created_by, group_name "
                "FROM slots "
                "WHERE status = 2 AND date = %s AND time = %s",
                (target_date, target_time),
            )
            reminders_to_send = cur.fetchall()

            for date, time_str, created_by, group_name in reminders_to_send:
                prev_dt = datetime.strptime(f"{date} {time_str}", "%Y-%m-%d %H:%M") - timedelta(hours=1)
                prev_date = prev_dt.strftime("%Y-%m-%d")
                prev_time = prev_dt.strftime("%H:%M")

                cur.execute(
                    "SELECT group_name, created_by FROM slots WHERE date = %s AND time = %s",
                    (prev_date, prev_time),
                )
                prev_slot = cur.fetchone()
                if prev_slot:
                    prev_group, prev_created = prev_slot
                    if prev_group == group_name and prev_created == created_by:
                        continue

                end_time = get_end_time(date, time_str, group_name, created_by, cur)

                message = (
                    "🔔 *Напоминаем о забронированном времени:*\n"
                    f"_Дата:_ *{date}*\n"
                    f"_Время:_ *{time_str} - {end_time}*\n"
                    f"_Группа:_ *{group_name}*"
                )

                try:
                    bot.send_message(created_by, message, parse_mode="Markdown")
                except Exception as e:
                    print(f"[ERROR] Failed to send reminder to user {created_by}: {e}")

    except Exception as e:
        print(f"[ERROR] Error while processing reminders: {e}")
    finally:
        if conn:
            conn.close()


def get_end_time(date, start_time, group_name, created_by, cursor):
    start_datetime = datetime.strptime(f"{date} {start_time}", "%Y-%m-%d %H:%M")
    current = start_datetime

    while True:
        next_dt = current + timedelta(hours=1)
        next_date = next_dt.strftime("%Y-%m-%d")
        next_time = next_dt.strftime("%H:%M")

        cursor.execute(
            "SELECT group_name, created_by FROM slots WHERE date = %s AND time = %s",
            (next_date, next_time),
        )
        next_slot = cursor.fetchone()
        if not next_slot:
            break

        next_group, next_created = next_slot
        if next_group != group_name or next_created != created_by:
            break

        current = next_dt

    end_time = (current + timedelta(hours=1)).strftime("%H:%M")
    return end_time


if __name__ == "__main__":
    send_reminders()
