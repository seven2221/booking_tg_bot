import os
import sqlite3
import telebot
from dotenv import load_dotenv
from datetime import datetime, timedelta

load_dotenv()

BOT_TOKEN = os.getenv("MAIN_BOT_TOKEN")
bot = telebot.TeleBot(BOT_TOKEN)

def send_reminders():
    try:
        conn = sqlite3.connect('db/bookings.db')
        cursor = conn.cursor()
        now = datetime.now()
        notification_times = [
            now + timedelta(hours=2),
            now + timedelta(hours=24)
        ]
        for notification_time in notification_times:
            target_datetime = notification_time.strftime("%Y-%m-%d %H:%M")
            target_date = notification_time.strftime("%Y-%m-%d")
            target_time = notification_time.strftime("%H:%M")
            cursor.execute('''
                SELECT date, time, created_by, group_name 
                FROM slots 
                WHERE status = 2 AND date = ? AND time = ?
            ''', (target_date, target_time))
            reminders_to_send = cursor.fetchall()
            for reminder in reminders_to_send:
                date, time, created_by, group_name = reminder
                prev_time = (datetime.strptime(time, "%H:%M") - timedelta(hours=1)).strftime("%H:%M")
                if prev_time == "23:00":
                    prev_date = (datetime.strptime(date, "%Y-%m-%d") - timedelta(days=1)).strftime("%Y-%m-%d")
                else:
                    prev_date = date
                cursor.execute('''
                    SELECT group_name, created_by 
                    FROM slots 
                    WHERE date = ? AND time = ?
                ''', (prev_date, prev_time))
                prev_slot = cursor.fetchone()
                if prev_slot:
                    prev_group, prev_created = prev_slot
                    if (prev_group == group_name and prev_created == created_by):
                        continue
                message = (
                    f"🔔 *Напоминаем о забронированном времени:*\n"
                    f"_Дата:_ *{date}*\n"
                    f"_Время:_ *{time} - {get_end_time(date, time, group_name, created_by, cursor)}*\n"
                    f"_Группа:_ *{group_name}*"
                )
                try:
                    bot.send_message(created_by, message, parse_mode='Markdown')
                except Exception as e:
                    print(f"[ERROR] Failed to send reminder to user {created_by}: {e}")
    except Exception as e:
        print(f"[ERROR] Error while processing reminders: {e}")
    finally:
        if 'conn' in locals():
            conn.close()

def get_end_time(date, start_time, group_name, created_by, cursor):
    start_datetime = datetime.strptime(f"{date} {start_time}", "%Y-%m-%d %H:%M")
    current = start_datetime
    while True:
        next_hour = (current + timedelta(hours=1)).strftime("%H:%M")
        next_date = current.strftime("%Y-%m-%d")
        if next_hour == "00:00":
            next_date = (current + timedelta(days=1)).strftime("%Y-%m-%d")
        cursor.execute('''
            SELECT group_name, created_by 
            FROM slots 
            WHERE date = ? AND time = ?
        ''', (next_date, next_hour))
        next_slot = cursor.fetchone()
        if not next_slot:
            break
        next_group, next_created = next_slot
        if (next_group != group_name or next_created != created_by):
            break
        current += timedelta(hours=1)
    end_time = (current + timedelta(hours=1)).strftime("%H:%M")
    return end_time

if __name__ == "__main__":
    send_reminders()