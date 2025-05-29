import sqlite3
from datetime import datetime

def notify_subscribers_for_cancellation(group, bot):
    conn = sqlite3.connect('bookings.db')
    cursor = conn.cursor()
    ids = group["ids"]
    cursor.execute("SELECT date, time, subscribed_users FROM slots WHERE id IN ({})".format(','.join('?' * len(ids))), ids)
    results = cursor.fetchall()
    if not results:
        print("[Error] Нет данных для указанных ID.")
        return
    dates = set(row[0] for row in results)
    selected_date = list(dates)[0] if dates else "неизвестная дата"
    users_to_notify = {}
    for _, time, subs_str in results:
        if not subs_str:
            continue
        for user_id in subs_str.split(','):
            user_id = user_id.strip()
            if not user_id:
                continue
            if user_id not in users_to_notify:
                users_to_notify[user_id] = []
            users_to_notify[user_id].append(time)
    for user_id, times in users_to_notify.items():
        try:
            formatted_date = datetime.strptime(selected_date, "%Y-%m-%d").strftime("%d.%m.%Y")
            time_list = "\n".join(sorted(set(times)))
            message = f"🔔 У нас освободилось время!\n{formatted_date}:\n{time_list}"
            bot.send_message(int(user_id), message)
        except Exception as e:
            print(f"[Error] Can't notify user {user_id}: {e}")
    conn.close()
    
def notify_booking_cancelled(user_id, bot, group_name=None, start_time=None, end_time=None, date_formatted=None):
    try:
        message = (f"❌ К сожалению, мы были вынуждены отменить вашу бронь для группы \n*{group_name}*\n{date_formatted} с {start_time} по {end_time}\nпо техническим причинам.\nПриносим свои извинения за доставленные неудобства.\nСвязь с админом: @cyberocalypse")
        bot.send_message(int(user_id), message.strip(), parse_mode='Markdown')
    except Exception as e:
        print(f"[Error] Не удалось отправить уведомление пользователю {user_id}: {e}")