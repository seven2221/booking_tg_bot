import os
import time
import sqlite3
from datetime import datetime, timedelta
import telebot
from dotenv import load_dotenv
from telebot import types

from utils import is_admin, reset_user_state
from schedule_generator import create_schedule_grid_image

load_dotenv()

user_states = {}
admin_states = {}

ADMIN_BOT_TOKEN = os.getenv("ADMIN_BOT_TOKEN")
MAIN_BOT_TOKEN = os.getenv("MAIN_BOT_TOKEN")
ADMIN_IDS = list(map(int, os.getenv("ADMIN_IDS", "").split(",")))

admin_bot = telebot.TeleBot(ADMIN_BOT_TOKEN)
main_bot = telebot.TeleBot(MAIN_BOT_TOKEN)


def get_grouped_unconfirmed_bookings():
    with sqlite3.connect('bookings.db') as conn:
        cursor = conn.cursor()
        cursor.execute('''
            SELECT id, date, time, group_name, created_by FROM slots
            WHERE status = 1
            ORDER BY date, time
        ''')
        rows = cursor.fetchall()

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
                booking['date_str'] == current_group['date_str'] and
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


def confirm_booking(booking_ids):
    with sqlite3.connect('bookings.db') as conn:
        cursor = conn.cursor()
        query = 'UPDATE slots SET status = 2 WHERE id IN ({})'.format(','.join('?' * len(booking_ids)))
        cursor.execute(query, booking_ids)
        conn.commit()


def reject_booking(booking_ids):
    with sqlite3.connect('bookings.db') as conn:
        cursor = conn.cursor()
        query = '''UPDATE slots SET 
                    user_id = NULL, 
                    group_name = NULL, 
                    created_by = NULL, 
                    booking_type = NULL, 
                    comment = NULL, 
                    contact_info = NULL, 
                    status = 0 
                  WHERE id IN ({})'''.format(','.join('?' * len(booking_ids)))
        cursor.execute(query, booking_ids)
        conn.commit()


def format_booking_info(group):
    start_time = group['start_time'].strftime("%H:%M")
    end_time = group['end_time'].strftime("%H:%M")
    date_str = datetime.strptime(group['date_str'], "%Y-%m-%d").strftime("%d.%m.%Y")

    return f"Дата: {date_str}\n"\
           f"Время: {start_time}–{end_time}\n"\
           f"Группа: {group['group_name']}\n"\
           f"Контакт: @{group['user_id']}"


def show_menu(chat_id):
    markup = types.ReplyKeyboardMarkup(resize_keyboard=True)
    markup.add(types.KeyboardButton("Просмотреть неподтвержденные брони"))
    markup.add(types.KeyboardButton("Посмотреть расписание"))
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

    try:
        with sqlite3.connect('bookings.db') as conn:
            cursor = conn.cursor()
            query = 'SELECT date, time FROM slots WHERE id IN ({}) ORDER BY time'.format(
                ','.join('?' * len(booking_ids)))
            cursor.execute(query, booking_ids)
            rows = cursor.fetchall()

        if not rows:
            raise Exception("Не найдено данных о слотах")

        dates = set(row[0] for row in rows)
        times = [row[1] for row in rows]
        date_str = dates.pop() if dates else "неизвестная дата"
        if len(dates) > 1:
            date_str = f"{date_str} и другие даты"

        start_time = times[0]
        end_time = times[-1]

        try:
            formatted_date = datetime.strptime(date_str, "%Y-%m-%d").strftime("%d.%m.%Y")
        except ValueError:
            formatted_date = "неизвестная дата"

    except Exception as e:
        print(f"[Error] Не удалось получить информацию о брони: {e}")
        confirmation_message = "✅ Ваша бронь подтверждена. Скоро мы свяжемся с вами для уточнения деталей."
    else:
        confirmation_message = f"✅ Ваша бронь подтверждена!\nОжидаем вас  {formatted_date} с {start_time} до {end_time} по адресу проспект Труда, 111А."

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
            main_bot.send_message(user_id,
                                  "❌ Ваша бронь отклонена. Приносим извинения за неудобства. Предлагаем выбрать другое время.")
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