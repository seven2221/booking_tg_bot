from datetime import datetime, timedelta
from lib.db_init import get_connection

def _to_date(obj):
    if hasattr(obj, "date"):
        try:
            return obj if isinstance(obj, datetime) else datetime.combine(obj, datetime.min.time())
        except Exception:
            pass
    if hasattr(obj, "strftime"):
        return obj
    return datetime.strptime(str(obj), "%Y-%m-%d")

def update_slots(days_ahead: int = 28):
    today = datetime.now().date()
    seven_days_ago = today - timedelta(days=7)
    hours = [f"{h:02d}:00" for h in range(24)]
    conn = get_connection()
    try:
        with conn.cursor() as cur:
            cur.execute("DELETE FROM slots WHERE date=%s", (seven_days_ago.strftime("%Y-%m-%d"),))
            cur.execute("SELECT MAX(date) FROM slots")
            row = cur.fetchone()
            last_date_val = row[0] if row else None
            if last_date_val is None:
                last_date = today - timedelta(days=1)
            else:
                if isinstance(last_date_val, str):
                    last_date = datetime.strptime(last_date_val, "%Y-%m-%d").date()
                else:
                    last_date = getattr(last_date_val, "date", lambda: last_date_val)()
            target_last_date = today + timedelta(days=days_ahead)
            insert_rows = []
            d = last_date + timedelta(days=1)
            while d <= target_last_date:
                datestr = d.strftime("%Y-%m-%d")
                for t in hours:
                    insert_rows.append((datestr, t, 0))
                d += timedelta(days=1)
            if insert_rows:
                cur.executemany(
                    "INSERT INTO slots (date, time, status) VALUES (%s, %s, %s)",
                    insert_rows,
                )
            check_date = today
            while check_date <= target_last_date:
                datestr = check_date.strftime("%Y-%m-%d")
                cur.execute("SELECT time FROM slots WHERE date=%s", (datestr,))
                existing = set()
                for r in cur.fetchall():
                    v = r[0] if isinstance(r, (list, tuple)) else r
                    existing.add(str(v)[:5])
                missing = [(datestr, h, 0) for h in hours if h not in existing]
                if missing:
                    cur.executemany(
                        "INSERT INTO slots (date, time, status) VALUES (%s, %s, %s)",
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
        try:
            conn.close()
        except Exception:
            pass

if __name__ == "__main__":
    update_slots()
