import os
import time
import sqlite3
from datetime import datetime, timedelta
import telebot
from dotenv import load_dotenv
from telebot import types

from utils import is_admin, reset_user_state, get_grouped_unconfirmed_bookings, confirm_booking, reject_booking, format_booking_info, format_date
from schedule_generator import create_schedule_grid_image

load_dotenv()

user_states = {}
admin_states = {}

ADMIN_BOT_TOKEN = os.getenv("ADMIN_BOT_TOKEN")
MAIN_BOT_TOKEN = os.getenv("MAIN_BOT_TOKEN")
ADMIN_IDS = list(map(int, os.getenv("ADMIN_IDS", "").split(",")))

admin_bot = telebot.TeleBot(ADMIN_BOT_TOKEN)
main_bot = telebot.TeleBot(MAIN_BOT_TOKEN)

def show_menu(chat_id):
    markup = types.ReplyKeyboardMarkup(resize_keyboard=True)
    markup.add(types.KeyboardButton("Просмотреть неподтвержденные брони"))
    markup.add(types.KeyboardButton("Посмотреть расписание"))
    markup.add(types.KeyboardButton("Отменить бронь"))
    admin_bot.send_message(chat_id, "Админ-меню:", reply_markup=markup)

@admin_bot.message_handler(commands=['start'])
def handle_start(message):
    if not is_admin(message.from_user.id):
        admin_bot.send_message(message.chat.id, "❌ У вас нет прав для использования этого бота.")
        return
    admin_bot.set_my_commands([telebot.types.BotCommand("/start", "Главное меню")])
    show_menu(message.chat.id)

@admin_bot.message_handler(func=lambda msg: msg.text == "Посмотреть расписание")
def view_schedule(message):
    path = create_schedule_grid_image(message.chat.id)
    with open(path, "rb") as img:
        admin_bot.send_photo(message.chat.id, img, caption="Расписание на ближайшие дни:")
    os.remove(path)
    reset_user_state(message.chat.id, user_states)
    show_menu(message)

@admin_bot.message_handler(func=lambda msg: msg.text == "Отменить бронь")
def handle_cancel_booking(message):
    admin_id = message.from_user.id
    if not is_admin(admin_id):
        admin_bot.send_message(message.chat.id, "❌ У вас нет прав для выполнения этой операции.")
        return
    conn = sqlite3.connect('bookings.db')
    cursor = conn.cursor()
    cursor.execute('''
        SELECT DISTINCT date FROM slots WHERE status IN (1, 2) ORDER BY date
    ''')
    all_dates = [row[0] for row in cursor.fetchall()]
    valid_dates = []
    for date_str in all_dates:
        date = datetime.strptime(date_str, "%Y-%m-%d")
        next_day = (date + timedelta(days=1)).strftime("%Y-%m-%d")
        prev_day = (date - timedelta(days=1)).strftime("%Y-%m-%d")
        def get_slot_data(date_val, time_val):
            cursor.execute('''
                SELECT created_by, group_name, status FROM slots 
                WHERE date = ? AND time = ?
            ''', (date_val, time_val))
            result = cursor.fetchone()
            if result and result[2] != 0:
                return result[:2]
            return None
        slot_00 = get_slot_data(date_str, '00:00')
        slot_23_prev = get_slot_data(prev_day, '23:00')
        if slot_00 and slot_23_prev and slot_00 == slot_23_prev:
            continue
        cursor.execute('''
            SELECT COUNT(*) FROM slots 
            WHERE date = ? AND time != '00:00' AND status IN (1, 2)
        ''', (date_str,))
        other_bookings_count = cursor.fetchone()[0]
        if other_bookings_count > 0 or not slot_00:
            valid_dates.append(date_str)
    conn.close()
    if not valid_dates:
        admin_bot.send_message(message.chat.id, "Нет доступных дней для отмены броней.")
        show_menu(message.chat.id)
        return
    user_states[admin_id] = {"step": "choose_date_for_cancellation", "valid_dates": valid_dates}
    send_date_selection_keyboard(message.chat.id, valid_dates)

def send_date_selection_keyboard(chat_id, dates):
    markup = types.ReplyKeyboardMarkup(resize_keyboard=True)
    buttons = [types.KeyboardButton(format_date(d)) for d in dates]
    for i in range(0, len(buttons), 3):
        markup.row(*buttons[i:i+3])
    markup.row(types.KeyboardButton("⬅️ Назад"))
    markup.row(types.KeyboardButton("🏠 На главную"))
    admin_bot.send_message(chat_id, "Выберите день для отмены брони:", reply_markup=markup)

@admin_bot.message_handler(func=lambda msg: msg.text not in ["⬅️ Назад", "🏠 На главную"] and user_states.get(msg.from_user.id, {}).get("step") == "choose_date_for_cancellation")
def handle_choose_date_for_cancellation(message):
    admin_id = message.from_user.id
    selected_date = format_date_to_db(message.text)
    if selected_date not in user_states[admin_id]["valid_dates"]:
        admin_bot.send_message(message.chat.id, "Выберите корректный день из предложенных.")
        return
    bookings = get_grouped_bookings_for_cancellation(selected_date, admin_id)
    if not bookings:
        admin_bot.send_message(message.chat.id, "На этот день нет броней для отмены.")
        reset_user_state(message.chat.id, user_states)
        show_menu(message.chat.id)
        return
    user_states[admin_id].update({
        "step": "choose_booking_for_cancellation",
        "selected_date": selected_date,
        "bookings": bookings
    })
    send_booking_selection_keyboard(message.chat.id, bookings)

def get_grouped_bookings_for_cancellation(date, admin_id):
    conn = sqlite3.connect('bookings.db')
    cursor = conn.cursor()
    cursor.execute('''
        SELECT id, date, time, group_name, created_by FROM slots
        WHERE date = ?
          AND status IN (1, 2)
        ORDER BY time
    ''', (date,))
    rows = cursor.fetchall()
    conn.close()
    bookings = []
    for row in rows:
        bid, date_str, time_str, group_name, user_id = row
        try:
            dt = datetime.strptime(f"{date_str} {time_str}", "%Y-%m-%d %H:%M")
        except ValueError:
            continue
        bookings.append({
            'id': bid,
            'datetime': dt,
            'date_str': date_str,
            'time_str': time_str,
            'group_name': group_name,
            'user_id': user_id
        })
    grouped = []
    current_group = None
    for booking in bookings:
        if not current_group:
            current_group = {
                'start_time': booking['datetime'],
                'end_time': booking['datetime'] + timedelta(hours=1),
                'ids': [booking['id']],
                'group_name': booking['group_name'],
                'user_id': booking['user_id'],
                'date_str': booking['date_str']
            }
        else:
            if (booking['group_name'] == current_group['group_name'] and
                booking['user_id'] == current_group['user_id'] and
                booking['datetime'] == current_group['end_time']):
                current_group['end_time'] += timedelta(hours=1)
                current_group['ids'].append(booking['id'])
            else:
                grouped.append(current_group)
                current_group = {
                    'start_time': booking['datetime'],
                    'end_time': booking['datetime'] + timedelta(hours=1),
                    'ids': [booking['id']],
                    'group_name': booking['group_name'],
                    'user_id': booking['user_id'],
                    'date_str': booking['date_str']
                }
    if current_group:
        grouped.append(current_group)
    return grouped

def send_booking_selection_keyboard(chat_id, bookings):
    markup = types.ReplyKeyboardMarkup(resize_keyboard=True)
    for idx, group in enumerate(bookings):
        start_time = group['start_time'].strftime("%H:%M")
        end_time = group['end_time'].strftime("%H:%M")
        btn_text = f"{start_time}–{end_time}"
        markup.add(types.KeyboardButton(btn_text))
    markup.row(types.KeyboardButton("⬅️ Выбрать другой день"))
    markup.row(types.KeyboardButton("🏠 На главную"))
    admin_bot.send_message(chat_id, "Выберите бронь для отмены:", reply_markup=markup)

@admin_bot.message_handler(func=lambda msg: msg.text not in ["⬅️ Выбрать другой день", "🏠 На главную"] and user_states.get(msg.from_user.id, {}).get("step") == "choose_booking_for_cancellation")
def handle_choose_booking_for_cancellation(message):
    admin_id = message.from_user.id
    time_range = message.text.strip().split("–")
    if len(time_range) != 2:
        admin_bot.send_message(message.chat.id, "Выберите корректный временной интервал.")
        return
    start_time = time_range[0].strip()
    end_time = time_range[1].strip()
    bookings = user_states[admin_id]["bookings"]
    selected_group = None
    for group in bookings:
        group_start = group["start_time"].strftime("%H:%M")
        group_end = group["end_time"].strftime("%H:%M")
        if group_start == start_time and group_end == end_time:
            selected_group = group
            break
    if not selected_group:
        admin_bot.send_message(message.chat.id, "Бронь не найдена.")
        return
    user_states[admin_id].update({
        "step": "ask_notify_subscribers",
        "selected_group": selected_group
    })
    markup = types.ReplyKeyboardMarkup(resize_keyboard=True)
    markup.row(types.KeyboardButton("✅ Да"), types.KeyboardButton("❌ Нет"))
    admin_bot.send_message(message.chat.id, "Уведомить подписавшихся?", reply_markup=markup)

def notify_subscribers_for_cancellation(group):
    conn = sqlite3.connect('bookings.db')
    cursor = conn.cursor()
    ids = group["ids"]
    query = 'SELECT time, subscribed_users FROM slots WHERE id IN ({})'.format(','.join('?' * len(ids)))
    cursor.execute(query, ids)
    results = cursor.fetchall()
    users_to_notify = {}
    for time, subs_str in results:
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
        first = min(times)
        last = max(times)
        try:
            main_bot.send_message(
                int(user_id),
                f"🔔 Слот освободился:\nДата: {group['date_str']}\nВремя: {first}–{last}"
            )
        except Exception as e:
            print(f"[Error] Can't notify user {user_id}: {e}")
    conn.close()

def clear_booking_slots(slot_ids):
    conn = sqlite3.connect('bookings.db')
    cursor = conn.cursor()
    query = '''UPDATE slots SET 
                user_id = NULL, 
                group_name = NULL, 
                created_by = NULL, 
                booking_type = NULL, 
                comment = NULL, 
                contact_info = NULL, 
                status = 0,
                subscribed_users = NULL
              WHERE id IN ({})'''.format(','.join('?' * len(slot_ids)))
    cursor.execute(query, slot_ids)
    conn.commit()
    conn.close()

@admin_bot.message_handler(func=lambda msg: msg.text in ["✅ Да", "❌ Нет"] and user_states.get(msg.from_user.id, {}).get("step") == "ask_notify_subscribers")
def handle_notify_choice(message):
    admin_id = message.from_user.id
    choice = message.text.strip()
    group = user_states[admin_id]["selected_group"]
    if choice == "✅ Да":
        notify_subscribers_for_cancellation(group)
    clear_booking_slots(group["ids"])
    reset_user_state(admin_id, user_states)
    show_menu(message.chat.id)

@admin_bot.message_handler(func=lambda msg: msg.text == "⬅️ Назад" and user_states.get(msg.from_user.id, {}).get("step") == "choose_date_for_cancellation")
def handle_back_from_date_selection(message):
    show_menu(message.chat.id)

@admin_bot.message_handler(func=lambda msg: msg.text == "⬅️ Выбрать другой день" and user_states.get(msg.from_user.id, {}).get("step") == "choose_booking_for_cancellation")
def handle_back_from_booking_selection(message):
    admin_id = message.from_user.id
    valid_dates = user_states[admin_id]["valid_dates"]
    send_date_selection_keyboard(message.chat.id, valid_dates)

@admin_bot.message_handler(func=lambda msg: msg.text == "🏠 На главную")
def handle_go_home(message):
    reset_user_state(message.chat.id, user_states)
    show_menu(message.chat.id)

def format_date_to_db(date_str):
    day_month, _ = date_str.split()
    year = datetime.now().year
    date_obj = datetime.strptime(f"{day_month}.{year}", "%d.%m.%Y")
    return date_obj.strftime("%Y-%m-%d")

@admin_bot.message_handler(func=lambda msg: msg.text == "Просмотреть неподтвержденные брони")
def handle_view_unconfirmed(message):
    admin_id = message.from_user.id
    if not is_admin(admin_id):
        admin_bot.send_message(message.chat.id, "❌ У вас нет прав для выполнения этой операции.")
        return

    groups = get_grouped_unconfirmed_bookings()
    if not groups:
        admin_bot.send_message(message.chat.id, "Нет неподтвержденных броней.")
        show_menu(message.chat.id)
        return

    user_states[admin_id] = 'awaiting_confirmation_action'

    for group in groups:
        info = format_booking_info(group)
        ids = group['ids']
        user_id = group['user_id']

        markup = types.InlineKeyboardMarkup()
        confirm_btn = types.InlineKeyboardButton("✅ Подтвердить",
                                                 callback_data=f"confirm:{','.join(map(str, ids))}:{user_id}")
        reject_btn = types.InlineKeyboardButton("❌ Отклонить",
                                                callback_data=f"reject:{','.join(map(str, ids))}:{user_id}")
        markup.add(confirm_btn, reject_btn)

        admin_bot.send_message(message.chat.id, info, reply_markup=markup)

    show_menu(message.chat.id)

@admin_bot.callback_query_handler(func=lambda call: ':' in call.data)
def handle_callback_query(call):
    try:
        action, booking_ids_str, user_id_str = call.data.split(":")
        booking_ids = list(map(int, booking_ids_str.split(',')))
        user_id = int(user_id_str)
    except ValueError:
        admin_bot.answer_callback_query(call.id, "❌ Ошибка при обработке запроса.")
        return
    group_name = None

    try:
        with sqlite3.connect('bookings.db') as conn:
            cursor = conn.cursor()
            query_slots = 'SELECT date, time, group_name FROM slots WHERE id IN ({}) ORDER BY time'.format(
                ','.join('?' * len(booking_ids)))
            cursor.execute(query_slots, booking_ids)
            rows = cursor.fetchall()
        if not rows:
            raise Exception("Не найдено данных о слотах")
        dates = set(row[0] for row in rows)
        times = [row[1] for row in rows]
        group_name = rows[0][2]
        date_str = dates.pop() if dates else "неизвестная дата"
        if len(dates) > 1:
            date_str = f"{date_str} и другие даты"
        start_time = times[0]
        end_time = times[-1]
        try:
            formatted_date = datetime.strptime(date_str, "%Y-%m-%d").strftime("%d.%m.%Y")
        except ValueError:
            formatted_date = "неизвестная дата"
        
        confirmation_message = f"✅ Ваша бронь для группы {group_name} подтверждена!\nОжидаем вас {formatted_date} в {start_time} по адресу проспект Труда, 111А.\nСвязь с админом: @cyberocalypse"
        decline_message = f"❌ Ваша бронь для группы {group_name or 'неизвестная группа'} {formatted_date} в {start_time} отклонена.\nПриносим извинения за неудобства. 😔\nПредлагаем выбрать другое время."
    
    except Exception as e:
        print(f"[Error] Не удалось получить информацию о брони: {e}")
    if action == "confirm":
        confirm_booking(booking_ids)
        try:
            main_bot.send_message(user_id, confirmation_message)
        except Exception as e:
            print(f"[Error] Не удалось отправить сообщение пользователю {user_id}: {e}")
        admin_bot.answer_callback_query(call.id, "✅ Бронь подтверждена.")
    elif action == "reject":
        reject_booking(booking_ids)
        try:
            main_bot.send_message(user_id, decline_message)
        except Exception as e:
            print(f"[Error] Не удалось отправить сообщение пользователю {user_id}: {e}")
        admin_bot.answer_callback_query(call.id, "❌ Бронь отклонена.")
    try:
        admin_bot.edit_message_reply_markup(chat_id=call.message.chat.id,
                                            message_id=call.message.message_id,
                                            reply_markup=None)
    except Exception as e:
        print(f"[Error] Не удалось удалить клавиатуру: {e}")

if __name__ == "__main__":
    admin_bot.polling(none_stop=True)