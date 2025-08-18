import os
from datetime import datetime

import mysql.connector
from mysql.connector import pooling


def _get_pool():
    global _MYSQL_POOL
    try:
        return _MYSQL_POOL
    except NameError:
        pass
    config = {
        "host": os.getenv("MYSQL_HOST", "localhost"),
        "database": os.getenv("MYSQL_DATABASE"),
        "user": os.getenv("MYSQL_USER"),
        "password": os.getenv("MYSQL_PASSWORD"),
        "port": int(os.getenv("MYSQL_PORT", "3306")),
        "charset": "utf8mb4",
        "use_pure": True,
    }
    missing = [k for k in ("database", "user", "password") if not config.get(k)]
    if missing:
        raise RuntimeError(f"Missing DB config keys in env: {missing}")
    _MYSQL_POOL = pooling.MySQLConnectionPool(
        pool_name="notify_pool",
        pool_size=5,
        pool_reset_session=True,
        **config,
    )
    return _MYSQL_POOL


def _fetch_slots_by_ids(ids):
    if not ids:
        return []
    pool = _get_pool()
    cnx = pool.get_connection()
    try:
        placeholders = ", ".join(["%s"] * len(ids))
        query = f"""
            SELECT date, time, subscribed_users
            FROM slots
            WHERE id IN ({placeholders})
        """
        with cnx.cursor() as cur:
            cur.execute(query, tuple(ids))
            rows = cur.fetchall()
        return rows
    finally:
        cnx.close()


def notify_subscribers_for_cancellation(group, bot):
    from datetime import datetime as _dt
    from lib.db_init import get_connection
    ids = group.get("ids") or []
    if not ids:
        return
    conn = get_connection()
    try:
        placeholders = ", ".join(["%s"] * len(ids))
        query = f"""
            SELECT date, time, subscribed_users
            FROM slots
            WHERE id IN ({placeholders})
        """
        with conn.cursor() as cur:
            cur.execute(query, tuple(ids))
            results = cur.fetchall()
    finally:
        try:
            conn.close()
        except Exception:
            pass
    if not results:
        return
    dates = set()
    users_to_notify = {}
    for row in results:
        dt_val = row[0]
        time_val = row[1]
        subs_str = row[2]
        date_iso = None
        if isinstance(dt_val, _dt):
            date_iso = dt_val.date().isoformat()
        elif hasattr(dt_val, "isoformat"):
            try:
                date_iso = dt_val.isoformat()
            except Exception:
                date_iso = str(dt_val)
        else:
            date_iso = str(dt_val)
        if date_iso:
            dates.add(date_iso)
        if not subs_str:
            continue
        time_text = str(time_val)
        for uid_raw in str(subs_str).split(","):
            uid_raw = (uid_raw or "").strip()
            if not uid_raw:
                continue
            try:
                uid_int = int(uid_raw)
            except Exception:
                continue
            users_to_notify.setdefault(uid_int, []).append(time_text)
    if dates:
        try:
            date_list = sorted(dates)
            base = date_list[0]
            formatted_date = _dt.strptime(base, "%Y-%m-%d").strftime("%d.%m.%Y")
        except Exception:
            formatted_date = ", ".join(date_list)
    else:
        formatted_date = "неизвестная дата"
    for user_id, times in users_to_notify.items():
        try:
            uniq_times_sorted = sorted(set(times))
            message = "🔔 У нас освободилось время!\n" + f"{formatted_date}:\n" + "\n".join(uniq_times_sorted)
            bot.send_message(int(user_id), message)
        except Exception as e:
            print(e)


def notify_booking_cancelled(user_id, bot, group_name=None, start_time=None, end_time=None, date_formatted=None):
    try:
        from lib.utils import escape_markdown
        safe_group = escape_markdown(group_name or "")
        message = (
            f"❌ К сожалению, мы были вынуждены отменить вашу бронь для группы \n*{safe_group}*\n"
            f"{date_formatted} с {start_time} по {end_time}\nпо техническим причинам.\н"
            f"Приносим свои извинения за доставленные неудобства.\nСвязь с админом: @cyberocalypse"
        )
        bot.send_message(int(user_id), message.strip(), parse_mode="Markdown")
    except Exception as e:
        print(e)