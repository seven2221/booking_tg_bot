from datetime import datetime, timedelta

from lib.db_init import get_connection


def update_slots(days_ahead=28):

    today = datetime.now().date()
    seven_days_ago = today - timedelta(days=7)

    hours = [f"{h:02d}:00" for h in range(24)]

    conn = get_connection()
    try:
        cur = conn.cursor()

        cur.execute("DELETE FROM slots WHERE date < %s", (seven_days_ago.strftime("%Y-%m-%d"),))

        cur.execute("SELECT MAX(date) FROM slots")
        (last_date_val,) = cur.fetchone() or (None,)

        if last_date_val is None:
            last_date = today - timedelta(days=1)
        else:
            if isinstance(last_date_val, str):
                last_date = datetime.strptime(last_date_val, "%Y-%m-%d").date()
            else:
                last_date = last_date_val

        target_last_date = today + timedelta(days=days_ahead)

        insert_rows = []
        d = last_date + timedelta(days=1)
        while d <= target_last_date:
            date_str = d.strftime("%Y-%m-%d")
            for t in hours:
                insert_rows.append((date_str, t, 0))
            d += timedelta(days=1)

        if insert_rows:
            cur.executemany(
                "INSERT INTO slots (`date`, `time`, `status`) VALUES (%s, %s, %s)",
                insert_rows,
            )

        check_date = today
        while check_date <= target_last_date:
            date_str = check_date.strftime("%Y-%m-%d")
            cur.execute(
                "SELECT time FROM slots WHERE date = %s",
                (date_str,),
            )
            existing = {row[0] for row in cur.fetchall()}
            missing = [(date_str, h, 0) for h in hours if h not in existing]
            if missing:
                cur.executemany(
                    "INSERT INTO slots (`date`, `time`, `status`) VALUES (%s, %s, %s)",
                    missing,
                )
            check_date += timedelta(days=1)

        conn.commit()
        print("Slots updated successfully.")
    except Exception as e:
        try:
            conn.rollback()
        except Exception:
            pass
        raise
    finally:
        conn.close()


if __name__ == "__main__":
    update_slots()
