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
    reject_booking,
    format_date,
)
from lib.schedule_tasks import (
    get_grouped_bookings_for_cancellation,
    clear_booking_slots,
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
    contact = "не указан"
    if contact_info:
        contact_info = contact_info.strip()
        contact = contact_info
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
        clear_booking_slots(booking_ids)
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
    clear_booking_slots(booking_ids)
    admin_bot.send_message(message.chat.id, "Бронь отменена ✅")
    show_menu(message)

@admin_bot.callback_query_handler(func=lambda call: ':' in call.data)
def handle_callback_query(call):
    try:
        action, booking_ids_str, user_id_str = call.data.split(":")
        booking_ids = list(map(int, booking_ids_str.split(',')))
        user_id = int(user_id_str)
    except ValueError:
        admin_bot.answer_callback_query(call.id, "❌ Ошибка при разборе данных.")
        return
    group_name = None
    date_str = None
    start_time = None
    end_time = None
    try:
        conn = get_connection()
        try:
            cur = conn.cursor()
            format_ids = ",".join(["%s"] * len(booking_ids))
            cur.execute(
                f"SELECT date, time, group_name FROM slots "
                f"WHERE id IN ({format_ids}) ORDER BY time",
                booking_ids
            )
            rows = cur.fetchall()
            if not rows:
                raise Exception("Не найдено данных о слотах")
            dates = sorted(set(r[0] for r in rows))
            times = [r[1] for r in rows]
            group_name = rows[0][2]
            if dates:
                date_str = dates[0].strftime("%Y-%m-%d") if hasattr(dates[0], "strftime") else str(dates[0])
            start_time = times[0].strftime("%H:%M") if hasattr(times[0], "strftime") else str(times[0])
            end_time = times[-1].strftime("%H:%M") if hasattr(times[-1], "strftime") else str(times[-1])
        finally:
            conn.close()
        try:
            formatted_date = datetime.strptime(date_str, "%Y-%m-%d").strftime("%d.%m.%Y")
        except Exception:
            formatted_date = date_str or "неизвестная дата"
        confirmation_message = (
            f"✅ Ваша бронь для группы «{group_name}» подтверждена!\n"
            f"Ожидаем вас {formatted_date} в {start_time}.\n"
            f"Связь с админом: @cyberocalypse"
        )
        decline_message = (
            f"❌ К сожалению, ваша бронь для группы «{group_name or 'неизвестная'}» "
            f"{formatted_date} в {start_time} отклонена.\n"
            f"Связь с админом: @cyberocalypse"
        )
        cancellation_message = (
            f"🚫 Ваша бронь для группы «{group_name or 'неизвестная'}» "
            f"{formatted_date} в {start_time} была отменена."
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
            reject_booking(booking_ids)
            try:
                main_bot.send_message(user_id, cancellation_message)
            except Exception as e:
                print(f"[Error] Не удалось уведомить пользователя {user_id}: {e}")
            admin_bot.answer_callback_query(call.id, "🚫 Бронь отменена.")
    except Exception as e:
        print(f"[Error] Не удалось обработать callback: {e}")
        admin_bot.answer_callback_query(call.id, "❌ Ошибка при обработке.")
    finally:
        try:
            admin_bot.edit_message_reply_markup(
                chat_id=call.message.chat.id,
                message_id=call.message.message_id,
                reply_markup=None
            )
        except Exception as e:
            print(f"[Error] Не удалось удалить клавиатуру: {e}")


@admin_bot.callback_query_handler(func=lambda c: c.data.startswith("confirm:"))
def cb_confirm(c):
    booking_id = int(c.data.split(":")[1])
    confirm_booking(booking_id)
    user_id = get_user_id_by_booking(booking_id)
    if user_id:
        main_bot.send_message(user_id, "Ваша бронь подтверждена ✅")
    admin_bot.answer_callback_query(c.id, "Подтверждено")
    admin_bot.edit_message_reply_markup(c.message.chat.id, c.message.message_id, reply_markup=None)


@admin_bot.callback_query_handler(func=lambda c: c.data.startswith("reject:"))
def cb_reject(c):
    booking_id = int(c.data.split(":")[1])
    reject_booking(booking_id)
    user_id = get_user_id_by_booking(booking_id)
    if user_id:
        main_bot.send_message(user_id, "К сожалению, ваша бронь отклонена ❌")
    admin_bot.answer_callback_query(c.id, "Отклонено")
    admin_bot.edit_message_reply_markup(c.message.chat.id, c.message.message_id, reply_markup=None)


@admin_bot.callback_query_handler(func=lambda c: c.data.startswith("cancel:"))
def cb_cancel(c):
    booking_id = int(c.data.split(":")[1])
    clear_booking_slots(booking_id)
    user_id = get_user_id_by_booking(booking_id)
    if user_id:
        main_bot.send_message(user_id, "Ваша бронь отменена ❌")
    admin_bot.answer_callback_query(c.id, "Отменено")
    admin_bot.edit_message_reply_markup(c.message.chat.id, c.message.message_id, reply_markup=None)


def main():
    admin_bot.polling(none_stop=True)


if __name__ == "__main__":
    main()
