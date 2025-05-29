import sqlite3
from datetime import datetime, timedelta

def update_slots():
    conn = sqlite3.connect('bookings.db')
    cursor = conn.cursor()
    seven_days_ago = (datetime.now() - timedelta(days=7)).strftime('%Y-%m-%d')
    cursor.execute('DELETE FROM slots WHERE date < ?', (seven_days_ago,))
    cursor.execute('SELECT MAX(date) FROM slots')
    result = cursor.fetchone()
    last_date_str = result[0]
    if last_date_str:
        last_date = datetime.strptime(last_date_str, '%Y-%m-%d').date()
    else:
        last_date = datetime.now().date()
    days_to_add = 28
    times = [f"{hour:02d}:00" for hour in range(0, 24)]
    for i in range(1, days_to_add + 1):
        current_date = last_date + timedelta(days=i)
        date_str = current_date.strftime('%Y-%m-%d')
        for time in times:
            cursor.execute(
                'INSERT INTO slots (date, time, status) VALUES (?, ?, ?)',
                (date_str, time, 0)
            )
    conn.commit()
    conn.close()
    print("Slots updated successfully.")

if __name__ == '__main__':
    update_slots()