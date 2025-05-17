import os
import time
import sqlite3
from datetime import datetime, timedelta
import telebot
from dotenv import load_dotenv
from telebot import types

from utils import is_admin, reset_user_state, get_grouped_unconfirmed_bookings, confirm_booking, reject_booking, format_booking_info, format_date, format_date_to_db, send_date_selection_keyboard, get_grouped_bookings_for_cancellation, send_booking_selection_keyboard, notify_subscribers_for_cancellation, clear_booking_slots, notify_booking_cancelled
from schedule_generator import create_schedule_grid_image

load_dotenv()

user_states = {}

ADMIN_BOT_TOKEN = os.getenv("ADMIN_BOT_TOKEN")
MAIN_BOT_TOKEN = os.getenv("MAIN_BOT_TOKEN")
ADMIN_IDS = list(map(int, os.getenv("ADMIN_IDS", "").split(",")))

admin_bot = telebot.TeleBot(ADMIN_BOT_TOKEN)
main_bot = telebot.TeleBot(MAIN_BOT_TOKEN)

def show_menu(message):
    markup = types.ReplyKeyboardMarkup(resize_keyboard=True)
    markup.add(types.KeyboardButton("Просмотреть неподтвержденные брони"))
    markup.add(types.KeyboardButton("Посмотреть расписание"))
    markup.add(types.KeyboardButton("Отменить бронь"))
    admin_bot.send_message(message.chat.id, "Выберите действие:", reply_markup=markup)
    reset_user_state(message.chat.id, user_states)

@admin_bot.message_handler(commands=['start'])
def handle_start(message):
    if not is_admin(message.from_user.id):
        admin_bot.send_message(message.chat.id, "❌ У вас нет прав для использования этого бота.")
        return
    admin_bot.set_my_commands([telebot.types.BotCommand("/start", "Главное меню")])
    show_menu(message)

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
    today = datetime.now().date()
    conn = sqlite3.connect('bookings.db')
    cursor = conn.cursor()
    cursor.execute('''
        SELECT DISTINCT date FROM slots WHERE status IN (1, 2) AND date >= ? ORDER BY date
    ''', (today.strftime("%Y-%m-%d"),))
    all_dates = [row[0] for row in cursor.fetchall()]
    conn.close()
    valid_dates = []
    for date_str in all_dates:
        bookings = get_grouped_bookings_for_cancellation(date_str)
        if bookings:
            valid_dates.append(date_str)
    if not valid_dates:
        admin_bot.send_message(message.chat.id, "Нет доступных дней для отмены броней.")
        show_menu(message)
        return
    user_states[admin_id] = {"step": "choose_date_for_cancellation", "valid_dates": valid_dates}
    send_date_selection_keyboard(message.chat.id, valid_dates, admin_bot)

@admin_bot.message_handler(func=lambda msg: msg.text not in ["Назад", "На главную"] and user_states.get(msg.from_user.id, {}).get("step") == "choose_date_for_cancellation")
def handle_choose_date_for_cancellation(message):
    admin_id = message.from_user.id
    selected_date = format_date_to_db(message.text)
    if selected_date not in user_states[admin_id]["valid_dates"]:
        admin_bot.send_message(message.chat.id, "Выберите корректный день из предложенных.")
        return
    bookings = get_grouped_bookings_for_cancellation(selected_date)
    today = datetime.now().date()
    selected_date_obj = datetime.strptime(selected_date, "%Y-%m-%d").date()
    current_hour = datetime.now().hour
    if selected_date_obj == today:
        bookings = [booking for booking in bookings if booking["start_time"].hour > current_hour]
    if not bookings:
        admin_bot.send_message(message.chat.id, "На этот день нет броней для отмены.")
        reset_user_state(message.chat.id, user_states)
        show_menu(message)
        return
    user_states[admin_id].update({
        "step": "choose_booking_for_cancellation",
        "selected_date": selected_date,
        "bookings": bookings
    })
    send_booking_selection_keyboard(message.chat.id, bookings, admin_bot)

@admin_bot.message_handler(func=lambda msg: msg.text not in ["⬅️ Выбрать другой день", "🏠 На главную"] and user_states.get(msg.from_user.id, {}).get("step") == "choose_booking_for_cancellation")
def handle_choose_booking_for_cancellation(message):
    admin_id = message.from_user.id
    text = message.text.strip()
    if "–" not in text or "," not in text:
        admin_bot.send_message(message.chat.id, "Выберите корректный временной интервал.")
        return
    time_part, group_name = text.split(",", 1)
    time_range = time_part.strip().split("–")
    if len(time_range) != 2:
        admin_bot.send_message(message.chat.id, "Неверный формат временного диапазона.")
        return
    start_time_str = time_range[0].strip()
    end_time_str = time_range[1].strip()
    try:
        start_time = datetime.strptime(start_time_str, "%H:%M").time()
        end_time = datetime.strptime(end_time_str, "%H:%M").time()
    except ValueError:
        admin_bot.send_message(message.chat.id, "Ошибка распознавания времени.")
        return
    bookings = user_states[admin_id]["bookings"]
    selected_group = None
    for group in bookings:
        group_start = group["start_time"].time()
        group_end = group["end_time"].time()
        group_name_stored = group.get("group_name", "")
        if group_start == start_time and group_end == end_time and group_name_stored.strip() == group_name.strip():
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

@admin_bot.message_handler(func=lambda msg: msg.text in ["✅ Да", "❌ Нет"] and user_states.get(msg.from_user.id, {}).get("step") == "ask_notify_subscribers")
def handle_notify_choice(message):
    admin_id = message.from_user.id
    choice = message.text.strip()
    group = user_states[admin_id]["selected_group"]
    if choice == "✅ Да":
        notify_subscribers_for_cancellation(group, main_bot)
    clear_booking_slots(group["ids"], main_bot)
    creator_id = group["user_id"]
    group_name = group["group_name"]
    start_time = group["start_time"].strftime("%H:%M")
    end_time = group["end_time"].strftime("%H:%M")
    date_str = group["start_time"].strftime("%Y-%m-%d")
    formatted_date = datetime.strptime(date_str, "%Y-%m-%d").strftime("%d.%m.%Y")
    notify_booking_cancelled(
        user_id=creator_id,
        bot=main_bot,
        group_name=group_name,
        start_time=start_time,
        end_time=end_time,
        date_formatted=formatted_date
    )
    reset_user_state(admin_id, user_states)
    show_menu(message)

@admin_bot.message_handler(func=lambda msg: msg.text == "Выбрать другой день" and user_states.get(msg.from_user.id, {}).get("step") == "choose_booking_for_cancellation")
def handle_back_from_booking_selection(message):
    admin_id = message.from_user.id
    valid_dates = user_states[admin_id]["valid_dates"]
    send_date_selection_keyboard(message.chat.id, valid_dates)

@admin_bot.message_handler(func=lambda msg: msg.text == "На главную")
def handle_go_home(message):
    reset_user_state(message.chat.id, user_states)
    show_menu(message)

@admin_bot.message_handler(func=lambda msg: msg.text == "Просмотреть неподтвержденные брони")
def handle_view_unconfirmed(message):
    admin_id = message.from_user.id
    if not is_admin(admin_id):
        admin_bot.send_message(message.chat.id, "❌ У вас нет прав для выполнения этой операции.")
        return
    groups = get_grouped_unconfirmed_bookings()
    if not groups:
        admin_bot.send_message(message.chat.id, "Нет неподтвержденных броней.")
        show_menu(message)
        return
    user_states[admin_id] = 'awaiting_confirmation_action'
    for group in groups:
        info = format_booking_info(group)
        ids = group['ids']
        user_id = group['user_id']
        markup = types.InlineKeyboardMarkup()
        confirm_btn = types.InlineKeyboardButton("✅ Подтвердить",callback_data=f"confirm:{','.join(map(str, ids))}:{user_id}")
        reject_btn = types.InlineKeyboardButton("❌ Отклонить",callback_data=f"reject:{','.join(map(str, ids))}:{user_id}")
        markup.add(confirm_btn, reject_btn)
        admin_bot.send_message(message.chat.id, info, reply_markup=markup)
    show_menu(message)

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
            cursor.execute('SELECT id, status FROM slots WHERE id IN ({})'.format(
                ','.join('?' * len(booking_ids))), booking_ids)
            status_rows = cursor.fetchall()
            status_set = set(status for _, status in status_rows)
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
        confirmation_message = f"✅ Ваша бронь для группы «{group_name}» подтверждена!\nОжидаем вас {formatted_date} в {start_time} по адресу проспект Труда, 111А.\nСвязь с админом: @cyberocalypse"
        decline_message = f"❌ Ваша бронь для группы «{group_name or 'неизвестная группа'}» {formatted_date} в {start_time} отклонена по техническим причинам.\nПриносим извинения за неудобства. 😔\nПожалуйста, выберите другое время.\nСвязь с админом: @cyberocalypse"
        cancellation_message = f"🚫 Ваша бронь для группы «{group_name or 'неизвестная группа'}» {formatted_date} в {start_time} была отменена администратором по вашей заявке.\n"
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
        elif action == "cancel":
            notify_subscribers_for_cancellation({"ids": booking_ids}, main_bot)
            reject_booking(booking_ids)
            try:
                main_bot.send_message(user_id, cancellation_message)
            except Exception as e:
                print(f"[Error] Не удалось уведомить пользователя {user_id}: {e}")
            admin_bot.answer_callback_query(call.id, "🚫 Бронь успешно отменена.")
    except Exception as e:
        print(f"[Error] Не удалось обработать callback: {e}")
        admin_bot.answer_callback_query(call.id, "❌ Ошибка при обработке брони.")
    finally:
        try:
            admin_bot.edit_message_reply_markup(
                chat_id=call.message.chat.id,
                message_id=call.message.message_id,
                reply_markup=None
            )
        except Exception as e:
            print(f"[Error] Не удалось удалить клавиатуру: {e}")

if __name__ == "__main__":
    admin_bot.polling(none_stop=True)