import os
import time
from datetime import datetime, timedelta

import telebot
from dotenv import load_dotenv
from telebot import types

from lib.db_init import get_connection
from lib.utils import (
    is_admin,
    reset_user_state,
    confirm_booking,
    reject_booking,
    format_booking_info,
    format_date,
    format_date_to_db,
    validate_input,
)
from lib.schedule_tasks import (
    get_grouped_bookings_for_cancellation,
    clear_booking_slots,
    get_grouped_unconfirmed_bookings,
)
from lib.schedule_generator import create_schedule_grid_image, create_daily_schedule_image
from lib.keyboards import send_booking_selection_keyboard, send_date_selection_keyboard
from lib.notifiers import notify_subscribers_for_cancellation, notify_booking_cancelled

load_dotenv()

user_states = {}

ADMIN_BOT_TOKEN = os.getenv("ADMIN_BOT_TOKEN")
MAIN_BOT_TOKEN = os.getenv("MAIN_BOT_TOKEN")
ADMIN_IDS = [int(x) for x in os.getenv("ADMIN_IDS", "").split(",") if x.strip().isdigit()]

admin_bot = telebot.TeleBot(ADMIN_BOT_TOKEN)
main_bot = telebot.TeleBot(MAIN_BOT_TOKEN)


def show_menu(message):
    markup = types.ReplyKeyboardMarkup(resize_keyboard=True)
    markup.add(types.KeyboardButton("Просмотреть неподтвержденные брони"))
    markup.add(types.KeyboardButton("Посмотреть расписание"))
    markup.add(types.KeyboardButton("Отменить бронь"))
    admin_bot.send_message(message.chat.id, "Выберите действие:", reply_markup=markup)
    reset_user_state(message.chat.id, user_states)


@admin_bot.message_handler(commands=["start"])
def handle_start(message):
    if not is_admin(message.from_user.id):
        admin_bot.send_message(message.chat.id, "❌ У вас нет прав для использования этого бота.")
        return
    admin_bot.set_my_commands([telebot.types.BotCommand("/start", "Главное меню")])
    show_menu(message)


@admin_bot.message_handler(func=lambda msg: msg.text == "Посмотреть расписание")
def view_schedule(message):
    markup = types.ReplyKeyboardMarkup(resize_keyboard=True)
    markup.row(types.KeyboardButton("Расписание на 28 дней"), types.KeyboardButton("Расписание на сегодня"))
    admin_bot.send_message(message.chat.id, "Выберите тип расписания:", reply_markup=markup)
    reset_user_state(message.chat.id, user_states)


@admin_bot.message_handler(func=lambda msg: msg.text == "Расписание на 28 дней")
def view_28_days_schedule(message):
    path = create_schedule_grid_image(message.chat.id, days_to_show=28)
    if path:
        with open(path, "rb") as img:
            admin_bot.send_photo(message.chat.id, img, caption="Расписание на ближайшие 28 дней:")
        try:
            os.remove(path)
        except Exception:
            pass
    else:
        admin_bot.send_message(message.chat.id, "Нет данных для отображения расписания.")
    reset_user_state(message.chat.id, user_states)
    show_menu(message)


@admin_bot.message_handler(func=lambda msg: msg.text == "Расписание на сегодня")
def view_today_schedule(message):
    markup = types.ReplyKeyboardMarkup(resize_keyboard=True)
    markup.row(types.KeyboardButton("Картинкой"), types.KeyboardButton("Списком"))
    admin_bot.send_message(message.chat.id, "Выберите формат расписания:", reply_markup=markup)


@admin_bot.message_handler(func=lambda msg: msg.text == "Картинкой")
def send_schedule_image(message):
    path = create_daily_schedule_image(message.chat.id)
    if path:
        with open(path, "rb") as img:
            admin_bot.send_photo(message.chat.id, img, caption="Расписание на сегодня:")
        try:
            os.remove(path)
        except Exception:
            pass
    else:
        admin_bot.send_message(message.chat.id, "Нет данных для отображения расписания на сегодня.")
    reset_user_state(message.chat.id, user_states)
    show_menu(message)


@admin_bot.message_handler(func=lambda msg: msg.text == "Списком")
def send_schedule_list(message):
    chat_id = message.chat.id
    today = datetime.now().strftime("%Y-%m-%d")
    tomorrow = (datetime.strptime(today, "%Y-%m-%d") + timedelta(days=1)).strftime("%Y-%m-%d")

    conn = get_connection()
    try:
        cur = conn.cursor()
        cur.execute(
            "SELECT date, time, group_name, contact_info, booking_type, comment "
            "FROM slots WHERE date IN (%s, %s) AND status != 0 ORDER BY date, time",
            (today, tomorrow),
        )
        rows = cur.fetchall()
    finally:
        conn.close()

    if not rows:
        admin_bot.send_message(chat_id, "На сегодня нет записей в расписании.")
        show_menu(message)
        return

    def is_consecutive(prev_date, prev_time, curr_date, curr_time):
        if not prev_time:
            return False
        prev_dt = datetime.strptime(f"{prev_date} {prev_time}", "%Y-%m-%d %H:%M")
        curr_dt = datetime.strptime(f"{curr_date} {curr_time}", "%Y-%m-%d %H:%M")
        return (curr_dt - prev_dt) == timedelta(hours=1)

    output_groups = []
    current_group = None
    start_time = None
    start_date = None
    prev_time = None
    prev_date = None

    for row in rows:
        date_str, time_str, group_name, contact_info, booking_type, comment = row
        # пропускаем полностью пустые записи
        if group_name is None and contact_info is None and booking_type is None and comment is None:
            continue
        group_data = (group_name or "", contact_info or "", booking_type or "", comment or "")
        if current_group is None:
            current_group = group_data
            start_time = time_str
            start_date = date_str
        elif group_data != current_group or not is_consecutive(prev_date, prev_time, date_str, time_str):
            end_dt = datetime.strptime(f"{prev_date} {prev_time}", "%Y-%m-%d %H:%M") + timedelta(hours=1)
            output_groups.append((start_date, start_time, end_dt, current_group))
            current_group = group_data
            start_time = time_str
            start_date = date_str
        prev_time = time_str
        prev_date = date_str

    if current_group and prev_time and prev_date:
        end_dt = datetime.strptime(f"{prev_date} {prev_time}", "%Y-%m-%d %H:%M") + timedelta(hours=1)
        output_groups.append((start_date, start_time, end_dt, current_group))

    now = datetime.now()
    for start_date, start_time, end_dt, group_data in output_groups:
        start_dt = datetime.strptime(f"{start_date} {start_time}", "%Y-%m-%d %H:%M")
        if end_dt <= now or start_date != today:
            continue
        send_schedule_list_notification(chat_id, start_dt.strftime("%H:%M"), end_dt.strftime("%H:%M"), group_data)

    reset_user_state(chat_id, user_states)
    show_menu(message)


def send_schedule_list_notification(chat_id, start_time, end_time, group_data):
    group_name, contact_info, booking_type, comment = group_data

    # Нормализуем контакт для отображения
    if contact_info:
        contact_info = contact_info.strip()
        if contact_info.startswith("+7") or contact_info.startswith("8"):
            contact = f"[{contact_info}]"
        elif contact_info.startswith("@"):
            contact = contact_info
        elif "@" in contact_info:
            contact = contact_info
        else:
            contact = contact_info
    else:
        contact = "не указан"

    note = (
        f"_Время:_ *{start_time}–{end_time}*\n"
        f"_Группа:_ *{group_name}*\n"
        f"_Тип:_ *{booking_type}*\n"
        f"_Комментарий:_ {comment}\n"
        f"_Контакт:_ {contact}"
    )

    try:
        admin_bot.send_message(chat_id, note, parse_mode="Markdown")
    except Exception as e:
        print(f"[Error] Can't send notification to admin: {e}")


@admin_bot.message_handler(func=lambda msg: msg.text == "Отменить бронь")
def handle_cancel_booking(message):
    admin_id = message.from_user.id
    if not is_admin(admin_id):
        admin_bot.send_message(message.chat.id, "❌ У вас нет прав для выполнения этой операции.")
        return

    today = datetime.now().strftime("%Y-%m-%d")
    conn = get_connection()
    try:
        cur = conn.cursor()
        cur.execute(
            "SELECT DISTINCT date FROM slots WHERE status IN (1, 2) AND date >= %s ORDER BY date",
            (today,),
        )
        all_dates = [row[0] for row in cur.fetchall()]
    finally:
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


@admin_bot.message_handler(
    func=lambda msg: msg.text not in ["Назад", "На главную"]
    and user_states.get(msg.from_user.id, {}).get("step") == "choose_date_for_cancellation"
)
def handle_choose_date_for_cancellation(message):
    admin_id = message.from_user.id
    # поддержка как "YYYY-MM-DD", так и "DD.MM ДД"
    try:
        selected_date = datetime.strptime(message.text, "%Y-%m-%d").strftime("%Y-%m-%d")
    except ValueError:
        try:
            selected_date = format_date_to_db(message.text)
        except Exception:
            admin_bot.send_message(message.chat.id, "Выберите корректный день из предложенных.")
            return

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

    user_states[admin_id].update(
        {
            "step": "choose_booking_for_cancellation",
            "selected_date": selected_date,
            "bookings": bookings,
        }
    )
    send_booking_selection_keyboard(message.chat.id, bookings, admin_bot)


@admin_bot.message_handler(
    func=lambda msg: msg.text not in ["⬅️ Выбрать другой день", "🏠 На главную"]
    and user_states.get(msg.from_user.id, {}).get("step") == "choose_booking_for_cancellation"
)
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

    user_states[admin_id].update({"step": "ask_notify_subscribers", "selected_group": selected_group})
    markup = types.ReplyKeyboardMarkup(resize_keyboard=True)
    markup.row(types.KeyboardButton("✅ Да"), types.KeyboardButton("❌ Нет"))
    admin_bot.send_message(message.chat.id, "Уведомить подписавшихся?", reply_markup=markup)


@admin_bot.message_handler(
    func=lambda msg: msg.text in ["✅ Да", "❌ Нет"]
    and user_states.get(msg.from_user.id, {}).get("step") == "ask_notify_subscribers"
)
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
            date_formatted=formatted_date,
        )

    reset_user_state(admin_id, user_states)
    show_menu(message)


@admin_bot.message_handler(func=lambda msg: msg.text == "Выбрать другой день" and user_states.get(msg.from_user.id, {}).get("step") == "choose_booking_for_cancellation")
def handle_back_from_booking_selection(message):
    admin_id = message.from_user.id
    valid_dates = user_states[admin_id]["valid_dates"]
    send_date_selection_keyboard(message.chat.id, valid_dates, admin_bot)


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

    user_states[admin_id] = "awaiting_confirmation_action"
    for group in groups:
        info = format_booking_info(group)
        ids = group["ids"]
        user_id = group["user_id"]
        markup = types.InlineKeyboardMarkup()
        confirm_btn = types.InlineKeyboardButton("✅ Подтвердить", callback_data=f"confirm:{','.join(map(str, ids))}:{user_id}")
        reject_btn = types.InlineKeyboardButton("❌ Отклонить", callback_data=f"reject:{','.join(map(str, ids))}:{user_id}")
        markup.add(confirm_btn, reject_btn)
        admin_bot.send_message(message.chat.id, info, reply_markup=markup)

    show_menu(message)


@admin_bot.callback_query_handler(func=lambda call: ":" in call.data)
def handle_callback_query(call):
    try:
        action, booking_ids_str, user_id_str = call.data.split(":")
        booking_ids = list(map(int, booking_ids_str.split(",")))
        user_id = int(user_id_str)
    except ValueError:
        admin_bot.answer_callback_query(call.id, "❌ Ошибка при обработке запроса.")
        return

    group_name = None
    try:
        # Получаем детали слотов
        placeholders = ",".join(["%s"] * len(booking_ids))
        conn = get_connection()
        try:
            cur = conn.cursor()
            cur.execute(
                f"SELECT date, time, group_name FROM slots WHERE id IN ({placeholders}) ORDER BY date, time",
                tuple(booking_ids),
            )
            rows = cur.fetchall()
            if not rows:
                raise Exception("Не найдено данных о слотах")

            cur.execute(
                f"SELECT id, status FROM slots WHERE id IN ({placeholders})",
                tuple(booking_ids),
            )
            status_rows = cur.fetchall()
        finally:
            conn.close()

        dates = {row[0] for row in rows}
        times = [row[1] for row in rows]
        group_name = rows[0][2] if rows[0][2] else None

        date_str = next(iter(dates)) if dates else "неизвестная дата"
        if len(dates) > 1:
            date_str = f"{date_str} и другие даты"
        start_time = times[0] if times else "?"
        end_time = times[-1] if times else "?"

        try:
            formatted_date = datetime.strptime(date_str, "%Y-%m-%d").strftime("%d.%m.%Y")
        except ValueError:
            formatted_date = "неизвестная дата"

        confirmation_message = (
            f"✅ Ваша бронь для группы «{group_name}» подтверждена!\n"
            f"Ожидаем вас {formatted_date} в {start_time} по адресу проспект Труда, 111А.\n"
            f"Связь с админом: @cyberocalypse"
        )
        decline_message = (
            f"❌ К сожалению, по техническим причинам мы вынуждены отклонить вашу бронь "
            f"для группы «{group_name or 'неизвестная группа'}» {formatted_date} в {start_time}.\n"
            f"Приносим извинения за неудобства. 😔\nПожалуйста, выберите другое время.\n"
            f"Связь с админом: @cyberocalypse"
        )
        cancellation_message = (
            f"🚫 Ваша бронь для группы «{group_name or 'неизвестная группа'}» {formatted_date} в {start_time} "
            f"была отменена администратором по вашей заявке."
        )

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
                reply_markup=None,
            )
        except Exception as e:
            print(f"[Error] Не удалось удалить клавиатуру: {e}")


if __name__ == "__main__":
    admin_bot.polling(none_stop=True)
