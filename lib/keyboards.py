from telebot import types
from telebot.types import InlineKeyboardMarkup, InlineKeyboardButton
from datetime import datetime

from lib.db_init import get_connection
from lib.utils import format_date


def send_booking_selection_keyboard(chat_id, bookings, bot):
    markup = types.ReplyKeyboardMarkup(resize_keyboard=True)
    for group in bookings:
        start_time = group['start_time'].strftime("%H:%M")
        end_time = group['end_time'].strftime("%H:%M")
        group_name = group.get('group_name', 'Без названия')
        btn_text = f"{start_time}–{end_time}, {group_name}"
        markup.add(types.KeyboardButton(btn_text))
    markup.row(types.KeyboardButton("Выбрать другой день"))
    markup.row(types.KeyboardButton("На главную"))
    bot.send_message(chat_id, "Выберите бронь для отмены:", reply_markup=markup)


def send_date_selection_keyboard(chat_id, dates, bot):
    markup = types.ReplyKeyboardMarkup(resize_keyboard=True)
    buttons = [types.KeyboardButton(format_date(d)) for d in dates]
    for i in range(0, len(buttons), 3):
        markup.row(*buttons[i:i+3])
    markup.row(types.KeyboardButton("На главную"))
    bot.send_message(chat_id, "Выберите день для отмены брони:", reply_markup=markup)


def _load_creator_id_for_slot(date_str: str, time_str: str):
    conn = get_connection()
    try:
        cur = conn.cursor()
        cur.execute(
            "SELECT created_by FROM slots WHERE date = %s AND time = %s",
            (date_str, time_str),
        )
        row = cur.fetchone()
        return row[0] if row else None
    finally:
        conn.close()


def _load_all_slots_by_creator(creator_id: int):
    conn = get_connection()
    try:
        cur = conn.cursor()
        cur.execute(
            "SELECT id, created_by, date, time FROM slots "
            "WHERE created_by = %s ORDER BY date, time",
            (creator_id,),
        )
        return cur.fetchall()
    finally:
        conn.close()


def _group_booking_ids(rows):
    bookings = []
    for row in rows:
        bid, _user_id, date_str, time_str = row
        try:
            _ = datetime.strptime(f"{date_str} {time_str}", "%Y-%m-%d %H:%M")
        except ValueError:
            continue
        bookings.append({'id': bid})
    grouped = []
    current_group = None
    for booking in bookings:
        if not current_group:
            current_group = {'ids': [booking['id']]}
        else:
            current_group['ids'].append(booking['id'])
    if current_group:
        grouped.append(current_group)
    return grouped


def create_confirmation_keyboard(selected_day: str, selected_time: str, booking_ids):
    if isinstance(booking_ids, (list, tuple)):
        if not booking_ids:
            raise ValueError("booking_ids is empty")
        booking_id = booking_ids
    else:
        if booking_ids is None:
            raise ValueError("booking_id is None")
        booking_id = booking_ids
    markup = InlineKeyboardMarkup()
    markup.add(
        InlineKeyboardButton("✅ Подтвердить", callback_data=f"confirm:{booking_id}"),
        InlineKeyboardButton("❌ Отклонить", callback_data=f"reject:{booking_id}")
    )
    return markup


def create_cancellation_keyboard(date_str: str, start_time: str, booking_ids):
    markup = InlineKeyboardMarkup()
    markup.add(
        InlineKeyboardButton(
            "🚫 Подтвердить отмену", callback_data=f"cancel:{date_str}:{start_time}"
        )
    )
    return markup