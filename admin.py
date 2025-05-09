import os
import time
import sqlite3
from datetime import datetime, timedelta
import telebot
from dotenv import load_dotenv
from telebot import types

from utils import is_admin, reset_user_state, get_grouped_unconfirmed_bookings, confirm_booking, reject_booking, format_booking_info
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
    # admin_bot.send_message(chat_id, "Админ-меню:", reply_markup=markup)

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
        
        confirmation_message = f"✅ Ваша бронь для группы {group_name} подтверждена!\nОжидаем вас {formatted_date} в {start_time} по адресу проспект Труда, 111А."
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