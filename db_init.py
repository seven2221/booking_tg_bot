import sqlite3
from datetime import datetime, timedelta

def init_db():
    conn = sqlite3.connect('bookings.db', check_same_thread=False)
    cursor = conn.cursor()
    cursor.execute('''
        CREATE TABLE IF NOT EXISTS slots (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            date TEXT NOT NULL,
            time TEXT NOT NULL
        )
    ''')

    def add_column_if_not_exists(table_name, column_name, column_definition):
        cursor.execute(f"PRAGMA table_info({table_name})")
        columns = [column[1] for column in cursor.fetchall()]
        if column_name not in columns:
            cursor.execute(f"ALTER TABLE {table_name} ADD COLUMN {column_name} {column_definition}")

    add_column_if_not_exists("slots", "user_id", "INTEGER DEFAULT NULL")
    add_column_if_not_exists("slots", "group_name", "TEXT DEFAULT NULL")
    add_column_if_not_exists("slots", "created_by", "INTEGER DEFAULT NULL")
    add_column_if_not_exists("slots", "subscribed_users", "TEXT DEFAULT NULL")
    add_column_if_not_exists("slots", "booking_type", "TEXT DEFAULT NULL")
    add_column_if_not_exists("slots", "comment", "TEXT DEFAULT NULL")
    add_column_if_not_exists("slots", "contact_info", "TEXT DEFAULT NULL")
    add_column_if_not_exists("slots", "status", "INTEGER DEFAULT 0")

    cursor.execute('SELECT COUNT(*) FROM slots')
    if cursor.fetchone()[0] == 0:
        times = [f"{hour:02d}:00" for hour in range(0, 24)]
        today = datetime.now().date()

        for i in range(28):
            date = (today + timedelta(days=i)).strftime('%Y-%m-%d')
            for time in times:
                cursor.execute(
                    'INSERT INTO slots (date, time, status) VALUES (?, ?, ?)', 
                    (date, time, 0)
                )

    conn.commit()
    conn.close()