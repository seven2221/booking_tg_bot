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
    ids = group.get("ids") or []
    if not ids:
        print("[Error] Пустой список ID в group['ids'].")
        return

    try:
        results = _fetch_slots_by_ids(ids)
    except Exception as e:
        print(f"[Error] DB fetch failed: {e}")
        return

    if not results:
        print("[Error] Нет данных для указанных ID.")
        return
    dates = set()
    for row in results:
        dt_val = row[0]
        if isinstance(dt_val, datetime):
            dates.add(dt_val.date().isoformat())
        elif hasattr(dt_val, "isoformat"):  # date
            dates.add(dt_val.isoformat())
        else:
            dates.add(str(dt_val))

    selected_date = list(dates)[0] if dates else "неизвестная дата"

    users_to_notify = {}
    for _, time_val, subs_str in results:
        if not subs_str:
            continue
        for user_id in str(subs_str).split(","):
            user_id = user_id.strip()
            if not user_id:
                continue
            users_to_notify.setdefault(user_id, []).append(str(time_val))

    formatted_date = selected_date
    try:
        formatted_date = datetime.strptime(str(selected_date), "%Y-%m-%d").strftime("%d.%m.%Y")
    except Exception:
        pass

    for user_id, times in users_to_notify.items():
        try:
            time_list = "\n".join(sorted(set(times)))
            message = f"🔔 У нас освободилось время!\n{formatted_date}:\n{time_list}"
            bot.send_message(int(user_id), message)
        except Exception as e:
            print(f"[Error] Can't notify user {user_id}: {e}")


def notify_booking_cancelled(user_id, bot, group_name=None, start_time=None, end_time=None, date_formatted=None):
    try:
        safe_group = escape_markdown(group_name or "")
        message = (
            f"❌ К сожалению, мы были вынуждены отменить вашу бронь для группы \n*{safe_group}*\n"
            f"{date_formatted} с {start_time} по {end_time}\nпо техническим причинам.\n"
            f"Приносим свои извинения за доставленные неудобства.\nСвязь с админом: @cyberocalypse"
        )
        bot.send_message(int(user_id), message.strip(), parse_mode="Markdown")
    except Exception as e:
        print(f"[Error] Не удалось отправить уведомление пользователю {user_id}: {e}")
