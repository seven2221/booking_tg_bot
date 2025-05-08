import os
import sqlite3
from datetime import datetime, timedelta
import telebot
from dotenv import load_dotenv
from telebot import types
import threading

load_dotenv()

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
            if (
                booking['group_name'] == current_group['group_name'] and
                booking['user_id'] == current_group['user_id'] and
                booking['date_str'] == current_group['date_str'] and
                booking['datetime'] == current_group['end_time']
            ):
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
        query = 'DELETE FROM slots WHERE id IN ({})'.format(','.join('?' * len(booking_ids)))
        cursor.execute(query, booking_ids)
        conn.commit()

def format_booking_info(group):
    start_time = group['start_time'].strftime("%H:%M")
    end_time = group['end_time'].strftime("%H:%M")
    return f"ID: {','.join(map(str, group['ids']))} | {group['date_str']} | {start_time}-{end_time} | {group['group_name']}"

def show_admin_menu(chat_id):
    markup = types.ReplyKeyboardMarkup(resize_keyboard=True)
    markup.add(types.KeyboardButton("Просмотреть неподтвержденные брони"))
    admin_bot.send_message(chat_id, "Админ-меню:", reply_markup=markup)

@admin_bot.message_handler(commands=['start'])
def handle_start(message):
    if message.from_user.id not in ADMIN_IDS:
        admin_bot.send_message(message.chat.id, "❌ У вас нет прав для использования этого бота.")
        return
    admin_bot.set_my_commands([telebot.types.BotCommand("/start", "Главное меню")])
    show_admin_menu(message.chat.id)

@admin_bot.message_handler(func=lambda msg: msg.text == "Просмотреть неподтвержденные брони")
def handle_view_unconfirmed(message):
    if message.from_user.id not in ADMIN_IDS:
        admin_bot.send_message(message.chat.id, "❌ У вас нет прав для выполнения этой операции.")
        return

    groups = get_grouped_unconfirmed_bookings()
    if not groups:
        admin_bot.send_message(message.chat.id, "Нет неподтвержденных броней.")
        show_admin_menu(message.chat.id)
        return

    for group in groups:
        info = format_booking_info(group)
        ids = group['ids']
        user_id = group['user_id']

        markup = types.InlineKeyboardMarkup()
        confirm_btn = types.InlineKeyboardButton("✅ Подтвердить", callback_data=f"confirm:{','.join(map(str, ids))}:{user_id}")
        reject_btn = types.InlineKeyboardButton("❌ Отклонить", callback_data=f"reject:{','.join(map(str, ids))}:{user_id}")
        markup.add(confirm_btn, reject_btn)
        admin_bot.send_message(message.chat.id, info, reply_markup=markup)

    markup_back = types.ReplyKeyboardMarkup(resize_keyboard=True)
    markup_back.add(types.KeyboardButton("Вернуться в меню"))
    admin_bot.send_message(message.chat.id, "Выберите действие:", reply_markup=markup_back)

@admin_bot.message_handler(func=lambda msg: msg.text == "Вернуться в меню")
def handle_back_to_menu(message):
    show_admin_menu(message.chat.id)

@admin_bot.callback_query_handler(func=lambda call: True)
def handle_callback_query(call):
    try:
        action, booking_ids_str, user_id_str = call.data.split(":")
        booking_ids = list(map(int, booking_ids_str.split(',')))
        user_id = int(user_id_str)
    except ValueError:
        admin_bot.answer_callback_query(call.id, "Ошибка обработки запроса.")
        return

    if action == "confirm":
        confirm_booking(booking_ids)
        try:
            first_booking_id = booking_ids[0]
            date_str = get_booking_date(first_booking_id)
            time_str = get_booking_start_time(first_booking_id)

            if date_str != "неизвестная дата" and time_str != "неизвестное время":
                date_formatted = datetime.strptime(date_str, "%Y-%m-%d").strftime("%d.%m.%Y")
                main_bot.send_message(
                    user_id,
                    f"Ваша бронь подтверждена, ожидаем вас на репетиционной базе {date_formatted} с {time_str}."
                )
            else:
                main_bot.send_message(
                    user_id,
                    "Ваша бронь подтверждена. Скоро мы свяжемся с вами для уточнения деталей."
                )
        except:
            pass
        admin_bot.answer_callback_query(call.id, "Бронь подтверждена.")

    if action == "reject":
        reject_booking(booking_ids)
        try:
            main_bot.send_message(
                user_id,
                "Ваша бронь отклонена. Приносим извинения за неудобства. Предлагаем выбрать другое время."
            )
        except:
            pass
        admin_bot.answer_callback_query(call.id, "Бронь отклонена.")

    admin_bot.edit_message_reply_markup(call.message.chat.id, call.message.message_id, reply_markup=None)
    admin_bot.send_message(call.message.chat.id, "Продолжить работу?", reply_markup=get_continue_markup())
    
def get_continue_markup():
    markup = types.ReplyKeyboardMarkup(resize_keyboard=True)
    markup.add(types.KeyboardButton("Просмотреть неподтвержденные брони"))
    markup.add(types.KeyboardButton("Вернуться в меню"))
    return markup

def get_booking_date(booking_id):
    with sqlite3.connect('bookings.db') as conn:
        cursor = conn.cursor()
        cursor.execute("SELECT date FROM slots WHERE id = ?", (booking_id,))
        result = cursor.fetchone()
    return result[0] if result and result[0] else "неизвестная дата"


def get_booking_start_time(booking_id):
    with sqlite3.connect('bookings.db') as conn:
        cursor = conn.cursor()
        cursor.execute("SELECT time FROM slots WHERE id = ?", (booking_id,))
        result = cursor.fetchone()
    return result[0] if result and result[0] else "неизвестное время"

if __name__ == "__main__":
    admin_bot.polling(none_stop=True)
