import os
import telebot
from datetime import datetime, timedelta
from telebot import types
from dotenv import load_dotenv
from lib.db_init import get_connection
from lib.utils import (
    is_admin,
    reset_user_state,
    confirm_booking,
    format_date,
    get_user_id_by_booking,
    escape_markdown,
    get_booking_info_by_id,
)
from lib.schedule_tasks import (
    get_grouped_bookings_for_cancellation,
    reject_booking,
    get_grouped_unconfirmed_bookings,
)
from lib.schedule_generator import create_schedule_grid_image, create_daily_schedule_image

load_dotenv()

ADMIN_BOT_TOKEN = os.getenv("ADMIN_BOT_TOKEN")
MAIN_BOT_TOKEN = os.getenv("MAIN_BOT_TOKEN")
ADMIN_IDS = [int(x) for x in os.getenv("ADMIN_IDS", "").split(",") if x.strip().isdigit()]

if not ADMIN_BOT_TOKEN:
    raise RuntimeError("ADMIN_BOT_TOKEN не задан в окружении")

admin_bot = telebot.TeleBot(ADMIN_BOT_TOKEN)
main_bot = telebot.TeleBot(MAIN_BOT_TOKEN)
user_states: dict[int, dict] = {}


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
    admin_bot.set_my_commands([types.BotCommand("/start", "Главное меню")])
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
    safe_group = escape_markdown(group_name or "")
    safe_type = escape_markdown(booking_type or "")
    safe_comment = escape_markdown(comment or "")
    safe_contact = escape_markdown(contact_info or "—")
    note = (
        f"_Время:_ *{start_time}–{end_time}*\n"
        f"_Группа:_ *{safe_group}*\n"
        f"_Тип:_ *{safe_type}*\n"
        f"_Комментарий:_ {safe_comment}\n"
        f"_Контакт:_ {safe_contact}"
    )
    try:
        admin_bot.send_message(chat_id, note, parse_mode="Markdown")
    except Exception as e:
        print(f"[Error] Can't send notification: {e}")


@admin_bot.message_handler(func=lambda msg: msg.text == "Просмотреть неподтвержденные брони")
def show_unconfirmed_bookings(message):
    if not is_admin(message.from_user.id):
        admin_bot.send_message(message.chat.id, "❌ Нет прав.")
        return

    groups = get_grouped_unconfirmed_bookings()
    if not groups:
        admin_bot.send_message(message.chat.id, "Нет неподтвержденных броней.")
        return

    state = {"step": "choose_unconfirmed_booking", "groups": groups}
    user_states[message.chat.id] = state
    markup = types.ReplyKeyboardMarkup(resize_keyboard=True)
    for g in groups:
        start_time = g["start_time"].strftime("%H:%M")
        end_time = g["end_time"].strftime("%H:%M")
        date_iso = g["date_str"].strftime("%Y-%m-%d") if hasattr(g["date_str"], "strftime") else str(g["date_str"])
        label = f"{format_date(date_iso)} {start_time}–{end_time} · {g['group_name']}"
        markup.add(types.KeyboardButton(label))
    markup.add(types.KeyboardButton("На главную"))

    admin_bot.send_message(message.chat.id, "Выберите бронь:", reply_markup=markup)


@admin_bot.message_handler(func=lambda msg: user_states.get(msg.chat.id, {}).get("step") == "choose_unconfirmed_booking")
def handle_choose_unconfirmed_booking(message):
    if message.text == "На главную":
        show_menu(message)
        return
    state = user_states.get(message.chat.id, {}) or {}
    groups = state.get("groups", [])
    selected = None
    for g in groups:
        start_time = g["start_time"].strftime("%H:%M")
        end_time = g["end_time"].strftime("%H:%M")
        date_iso = g["date_str"].strftime("%Y-%m-%d") if hasattr(g["date_str"], "strftime") else str(g["date_str"])
        label = f"{format_date(date_iso)} {start_time}–{end_time} · {g['group_name']}"
        if message.text.strip() == label:
            selected = g
            break
    if not selected:
        admin_bot.send_message(message.chat.id, "Выберите бронь из списка.")
        return
    booking_ids = selected.get("ids", [])
    markup = types.ReplyKeyboardMarkup(resize_keyboard=True)
    markup.add("✅ Подтвердить", "❌ Отклонить")
    markup.add("На главную")
    state["selected"] = selected
    state["step"] = "decide_unconfirmed_booking"
    user_states[message.chat.id] = state
    admin_bot.send_message(message.chat.id, f"Выбранная бронь: {label}", reply_markup=markup)


@admin_bot.message_handler(func=lambda msg: user_states.get(msg.chat.id, {}).get("step") == "decide_unconfirmed_booking")
def handle_decide_unconfirmed_booking(message):
    if message.text == "На главную":
        show_menu(message)
        return
    state = user_states.get(message.chat.id, {}) or {}
    selected = state.get("selected")
    booking_ids = selected.get("ids", [])
    if message.text == "✅ Подтвердить":
        confirm_booking(booking_ids)
        admin_bot.send_message(message.chat.id, "Бронь подтверждена ✅")
    elif message.text == "❌ Отклонить":
        reject_booking(booking_ids)
        reject_booking(booking_ids)
        admin_bot.send_message(message.chat.id, "Бронь отклонена ❌")
    else:
        admin_bot.send_message(message.chat.id, "Выберите действие из списка.")
        return
    show_menu(message)


@admin_bot.message_handler(func=lambda msg: msg.text == "Отменить бронь")
def handle_cancel_booking_admin(message):
    if not is_admin(message.from_user.id):
        admin_bot.send_message(message.chat.id, "❌ Нет прав.")
        return
    today = datetime.now().strftime("%Y-%m-%d")
    conn = get_connection()
    try:
        cur = conn.cursor()
        cur.execute("SELECT DISTINCT date FROM slots WHERE status IN (1, 2) AND date >= %s ORDER BY date", (today,))
        dates = [row[0] for row in cur.fetchall()]
    finally:
        conn.close()
    if not dates:
        admin_bot.send_message(message.chat.id, "Нет активных броней для отмены.")
        show_menu(message)
        return
    state = {"step": "choose_date_for_cancellation_admin", "dates": dates}
    user_states[message.chat.id] = state
    markup = types.ReplyKeyboardMarkup(resize_keyboard=True)
    for d in dates:
        date_iso = d.strftime("%Y-%m-%d") if hasattr(d, "strftime") else str(d)
        markup.add(types.KeyboardButton(format_date(date_iso)))
    markup.add(types.KeyboardButton("На главную"))
    admin_bot.send_message(message.chat.id, "Выберите дату:", reply_markup=markup)


@admin_bot.message_handler(func=lambda msg: user_states.get(msg.chat.id, {}).get("step") == "choose_date_for_cancellation_admin")
def handle_date_for_cancellation_admin(message):
    if message.text == "На главную":
        show_menu(message)
        return
    state = user_states.get(message.chat.id, {}) or {}
    selected_date = None
    for d in state.get("dates", []):
        date_iso = d.strftime("%Y-%m-%d") if hasattr(d, "strftime") else str(d)
        if message.text.strip() == format_date(date_iso):
            selected_date = date_iso
            break
    if not selected_date:
        admin_bot.send_message(message.chat.id, "Выберите дату из списка.")
        return
    bookings = get_grouped_bookings_for_cancellation(selected_date)
    if not bookings:
        admin_bot.send_message(message.chat.id, "Нет броней для отмены в этот день.")
        show_menu(message)
        return
    state["step"] = "choose_booking_to_cancel_admin"
    state["bookings"] = bookings
    user_states[message.chat.id] = state
    markup = types.ReplyKeyboardMarkup(resize_keyboard=True)
    for b in bookings:
        start_time = b["start_time"].strftime("%H:%M")
        end_time = b["end_time"].strftime("%H:%M")
        markup.add(types.KeyboardButton(f"{start_time}–{end_time}, {b['group_name']}"))
    markup.add(types.KeyboardButton("На главную"))
    admin_bot.send_message(message.chat.id, "Выберите бронь:", reply_markup=markup)


@admin_bot.message_handler(func=lambda msg: user_states.get(msg.chat.id, {}).get("step") == "choose_booking_to_cancel_admin")
def handle_choose_booking_to_cancel_admin(message):
    if message.text == "На главную":
        show_menu(message)
        return
    state = user_states.get(message.chat.id, {}) or {}
    bookings = state.get("bookings", [])
    selected = None
    for b in bookings:
        start_time = b["start_time"].strftime("%H:%M")
        end_time = b["end_time"].strftime("%H:%M")
        if message.text.strip() == f"{start_time}–{end_time}, {b['group_name']}":
            selected = b
            break
    if not selected:
        admin_bot.send_message(message.chat.id, "Выберите бронь из списка.")
        return
    booking_ids = selected.get("ids", [])
    reject_booking(booking_ids)
    admin_bot.send_message(message.chat.id, "Бронь отменена ✅")
    show_menu(message)


@admin_bot.callback_query_handler(func=lambda callback: callback.data.startswith("confirm:"))
def handle_confirm_booking(callback):
    try:
        booking_id = int(callback.data.split(":")[1])
    except (IndexError, ValueError):
        admin_bot.answer_callback_query(callback.id, "❌ Ошибка данных")
        return
    user_id = get_user_id_by_booking(booking_id)
    booking_info = get_booking_info_by_id(booking_id)
    confirm_booking(booking_id)
    if user_id and booking_info:
        group_name = booking_info.get("group_name") or "неизвестная группа"
        date_str = booking_info["date"]
        start_time_str = booking_info["start_time"]
        try:
            main_bot.send_message(
                user_id,
                f"✅ Ваша бронь для группы «{group_name}» подтверждена!\n"
                f"Ожидаем вас {date_str} в {start_time_str} по адресу проспект Труда, 111А.\n"
                f"Связь с админом: @cyberocalypse"
            )
        except Exception as e:
            print(f"[Error] Не удалось уведомить пользователя {user_id}: {e}")
    admin_bot.answer_callback_query(callback.id, "Бронь подтверждена ✅")
    admin_bot.edit_message_reply_markup(callback.message.chat.id, callback.message.message_id, reply_markup=None)


@admin_bot.callback_query_handler(func=lambda callback: callback.data.startswith("reject:"))
def handle_reject_booking(callback):
    try:
        booking_id = int(callback.data.split(":")[1])
    except (IndexError, ValueError):
        admin_bot.answer_callback_query(callback.id, "❌ Ошибка данных")
        return
    user_id = get_user_id_by_booking(booking_id)
    booking_info = get_booking_info_by_id(booking_id)
    reject_booking(booking_id)
    if user_id and booking_info:
        group_name = booking_info.get("group_name") or "неизвестная группа"
        date_str = booking_info["date"]
        start_time_str = booking_info["start_time"]
        try:
            main_bot.send_message(
                user_id,
                f"❌ К сожалению, по техническим причинам мы вынуждены отклонить "
                f"вашу бронь для группы «{group_name}» {date_str} в {start_time_str}.\n"
                f"Приносим извинения за неудобства. 😔\n"
                f"Пожалуйста, выберите другое время.\n"
                f"Связь с админом: @cyberocalypse"
            )
        except Exception as e:
            print(f"[Error] Не удалось уведомить пользователя {user_id}: {e}")
    admin_bot.answer_callback_query(callback.id, "Бронь отклонена ❌")
    admin_bot.edit_message_reply_markup(callback.message.chat.id, callback.message.message_id, reply_markup=None)


@admin_bot.callback_query_handler(func=lambda c: c.data.startswith("cancel:"))
def cb_cancel(c):
    booking_id = int(c.data.split(":")[1])
    reject_booking(booking_id)
    user_id = get_user_id_by_booking(booking_id)
    if user_id:
        main_bot.send_message(user_id, "Ваша бронь отменена ❌")
    admin_bot.answer_callback_query(c.id, "Отменено")
    admin_bot.edit_message_reply_markup(c.message.chat.id, c.message.message_id, reply_markup=None)


def main():
    admin_bot.polling(none_stop=True)


if __name__ == "__main__":
    main()
