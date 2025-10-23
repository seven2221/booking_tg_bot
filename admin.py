import os
import logging
import telebot
from datetime import datetime, timedelta
from telebot import types
from dotenv import load_dotenv
from lib.db_init import get_connection
from lib.notifiers import (
    notify_subscribers_for_cancellation, 
    notify_booking_cancelled,
)
from lib.utils import (
    is_admin,
    reset_user_state,
    confirm_booking,
    format_date,
    get_user_id_by_booking,
    escape_markdown,
    get_booking_info_by_id,
    get_slot_ids_by_booking,
)
from lib.schedule_tasks import (
    get_grouped_bookings_for_cancellation,
    reject_booking,
    get_grouped_unconfirmed_bookings,
)
from lib.schedule_generator import create_schedule_grid_image, create_daily_schedule_image

logging.basicConfig(level=logging.INFO)
logger = logging.getLogger(__name__)

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


### Главное меню ###

@admin_bot.message_handler(commands=["start"])
def handle_start(message):
    if not is_admin(message.from_user.id):
        admin_bot.send_message(message.chat.id, "❌ У вас нет прав для использования этого бота.")
        return
    admin_bot.set_my_commands([types.BotCommand("/start", "Главное меню")])
    show_menu(message)


### Расписание ###

@admin_bot.message_handler(func=lambda msg: msg.text == "Посмотреть расписание")
def view_schedule(message):
    markup = types.ReplyKeyboardMarkup(resize_keyboard=True)
    markup.row(
        types.KeyboardButton("Расписание на 28 дней"),
        types.KeyboardButton("Расписание на сегодня"),
        types.KeyboardButton("Расписание на конкретный день")
    )
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
    today_iso = datetime.now().strftime("%Y-%m-%d")
    path = create_daily_schedule_image(today_iso, requester_id=message.chat.id)
    if path:
        try:
            with open(path, "rb") as img:
                admin_bot.send_photo(message.chat.id, img, caption="Расписание на сегодня:")
        finally:
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
    try:
        chat_id = message.chat.id
        today_iso = datetime.now().strftime("%Y-%m-%d")
        sql = f'SELECT DISTINCT booking_id FROM slots WHERE `date` = "{today_iso}" AND status != 0 AND booking_id IS NOT NULL ORDER BY booking_id'
        conn = get_connection()
        try:
            cur = conn.cursor()
            cur.execute(sql)
            id_rows = cur.fetchall()
        finally:
            conn.close()
        if id_rows:
            sample = id_rows[:5]
        if not id_rows:
            admin_bot.send_message(chat_id, "На сегодня нет записей в расписании.")
            reset_user_state(chat_id, user_states)
            show_menu(message)
            return
        booking_ids = []
        for idx, row in enumerate(id_rows):
            logger.info("[schedule_list] id_row[%s]=type:%s, value:%s", idx, type(row).__name__, row)
            if not row:
                continue
            v = row[0] if isinstance(row, (list, tuple)) else row
            if v is None:
                continue
            s = str(v).strip()
            if not s:
                continue
            try:
                booking_ids.append(int(s))
            except Exception:
                booking_ids.append(s)
        if not booking_ids:
            admin_bot.send_message(chat_id, "На сегодня нет записей в расписании.")
            reset_user_state(chat_id, user_states)
            show_menu(message)
            return
        seen = set()
        uniq_ids = []
        for b in booking_ids:
            if b in seen:
                continue
            seen.add(b)
            uniq_ids.append(b)
        day_start = datetime.strptime(today_iso + " 00:00", "%Y-%m-%d %H:%M")
        day_end = day_start + timedelta(days=1)
        items = []
        for bid in uniq_ids:
            try:
                info = get_booking_info_by_id(bid)
                if not info:
                    continue
                try:
                    start_dt = datetime.strptime(f"{info['date']} {info['start_time']}", "%d.%m.%Y %H:%M")
                except Exception as e:
                    print(e)
                    continue
                end_str = (info.get("end_time") or "").strip()
                parts = end_str.split()
                if len(parts) == 1 and parts[0]:
                    try:
                        end_dt = datetime.strptime(f"{info['date']} {parts}", "%d.%m.%Y %H:%M")
                        end_human = parts
                    except Exception as e:
                        print(e)
                        end_dt = start_dt + timedelta(hours=1)
                        end_human = (start_dt + timedelta(hours=1)).strftime("%H:%M")
                elif len(parts) == 2:
                    end_time_only, end_date_human = parts
                    try:
                        end_dt = datetime.strptime(f"{end_date_human} {end_time_only}", "%d.%m.%Y %H:%M")
                        end_human = end_time_only
                    except Exception as e:
                        print(e)
                        end_dt = start_dt + timedelta(hours=1)
                        end_human = (start_dt + timedelta(hours=1)).strftime("%H:%M")
                else:
                    end_dt = start_dt + timedelta(hours=1)
                    end_human = (start_dt + timedelta(hours=1)).strftime("%H:%M")
                if end_dt <= day_start or start_dt >= day_end:
                    print("[schedule_list] skip bid=%s due to no overlap with today", bid)
                    continue
                items.append({
                    "start_dt": start_dt,
                    "start_human": info.get("start_time", start_dt.strftime("%H:%M")),
                    "end_human": end_human,
                    "group": info.get("group_name") or "—",
                    "type": info.get("booking_type") or "—",
                    "comment": info.get("comment") or "—",
                    "contact": info.get("contact_info") or "—",
                })
            except Exception as e:
                print(e)
        if not items:
            admin_bot.send_message(chat_id, "На сегодня нет записей в расписании.")
            reset_user_state(chat_id, user_states)
            show_menu(message)
            return
        items.sort(key=lambda x: x["start_dt"])
        lines = [f"Расписание на сегодня ({datetime.now().strftime('%d.%m.%Y')}):"]
        for it in items:
            lines.append(f"{it['start_human']}–{it['end_human']}  ·|·  {it['group']}  ·|·  {it['type']}  ·|·  {it['contact']}  ·|·  {it['comment']}")
        admin_bot.send_message(chat_id, "\n".join(lines))
        reset_user_state(chat_id, user_states)
        show_menu(message)
    except Exception as e:
        print(e)
        try:
            admin_bot.send_message(message.chat.id, "Произошла ошибка при формировании списка расписания.")
        except Exception:
            pass
        try:
            reset_user_state(message.chat.id, user_states)
            show_menu(message)
        except Exception:
            pass


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


@admin_bot.message_handler(func=lambda msg: msg.text == "Расписание на конкретный день")
def view_schedule_specific_day(message):
    if not is_admin(message.from_user.id):
        admin_bot.send_message(message.chat.id, "❌ Нет прав.")
        return
    reset_user_state(message.chat.id, user_states)
    today = datetime.now().strftime("%Y-%m-%d")
    conn = get_connection()
    try:
        cur = conn.cursor()
        cur.execute(
            """
            SELECT DISTINCT date
            FROM slots
            WHERE date >= %s
              AND time BETWEEN '11:00' AND '23:00'
              AND status IN (1, 2)
            ORDER BY date
            """,
            (today,)
        )
        rows = cur.fetchall()
    finally:
        conn.close()
    iso_dates = []
    for (d,) in rows:
        iso = d.strftime("%Y-%m-%d") if hasattr(d, "strftime") else str(d)
        iso_dates.append(iso)
    if not iso_dates:
        admin_bot.send_message(message.chat.id, "Нет занятых слотов в ближайшие дни.")
        show_menu(message)
        return
    btn_map = {format_date(iso): iso for iso in iso_dates}
    markup = types.ReplyKeyboardMarkup(resize_keyboard=True, row_width=3)
    labels = list(btn_map.keys())
    for i in range(0, len(labels), 3):
        row = [types.KeyboardButton(lbl) for lbl in labels[i:i+3]]
        markup.add(*row)
    markup.add(types.KeyboardButton("На главную"))
    user_states[message.chat.id] = {
        "step": "choose_specific_day",
        "date_btn_map": btn_map,
    }
    admin_bot.send_message(message.chat.id, "Выберите день:", reply_markup=markup)


@admin_bot.message_handler(func=lambda msg: user_states.get(msg.chat.id, {}).get("step") == "choose_specific_day")
def handle_choose_specific_day(message):
    if message.text == "На главную":
        show_menu(message)
        return
    state = user_states.get(message.chat.id, {}) or {}
    btn_map = state.get("date_btn_map", {}) or {}
    incoming = " ".join((message.text or "").split())
    selected_iso = btn_map.get(incoming)
    if not selected_iso:
        markup = types.ReplyKeyboardMarkup(resize_keyboard=True, row_width=3)
        labels = list(btn_map.keys())
        for i in range(0, len(labels), 3):
            row = [types.KeyboardButton(lbl) for lbl in labels[i:i+3]]
            markup.add(*row)
        markup.add(types.KeyboardButton("На главную"))
        admin_bot.send_message(message.chat.id, "Пожалуйста, выберите дату из списка:", reply_markup=markup)
        return
    try:
        path = create_daily_schedule_image(selected_iso, requester_id=message.chat.id)
    except Exception as e:
        admin_bot.send_message(message.chat.id, f"Ошибка при построении расписания: {e}")
        return
    if path:
        try:
            with open(path, "rb") as img:
                admin_bot.send_photo(message.chat.id, img, caption=f"Расписание на {format_date(selected_iso)}:")
        finally:
            try:
                os.remove(path)
            except Exception:
                pass
    else:
        admin_bot.send_message(message.chat.id, "Нет данных для отображения на выбранный день.")
    reset_user_state(message.chat.id, user_states)
    show_menu(message)


### Неподтвержденные брони ###

def _load_unconfirmed_booking_ids_mysql() -> list[int]:
    conn = get_connection()
    try:
        cursor = conn.cursor()
        cursor.execute(
            """
            SELECT DISTINCT booking_id
            FROM slots
            WHERE status = 1 AND booking_id IS NOT NULL
            ORDER BY booking_id
            """
        )
        rows = cursor.fetchall()
    finally:
        conn.close()
    booking_ids: list[int] = []
    for row in rows:
        raw_value = row[0] if isinstance(row, (list, tuple)) else row
        if raw_value is None:
            continue
        try:
            booking_ids.append(int(str(raw_value).strip()))
        except Exception:
            continue
    return booking_ids


def _format_unconfirmed_booking_note(booking_info: dict) -> str:
    selected_day_iso_for_human = booking_info.get("date") or ""
    start_time_human = booking_info.get("start_time") or ""
    end_time_human = booking_info.get("end_time") or ""
    group_name = escape_markdown(booking_info.get("group_name") or "")
    booking_type = escape_markdown(booking_info.get("booking_type") or "")
    comment = escape_markdown(booking_info.get("comment") or "—")
    contact_info = escape_markdown(booking_info.get("contact_info") or "—")
    note = (
        f"Дата: {selected_day_iso_for_human}\n"
        f"Время: {start_time_human}–{end_time_human}\n"
        f"Группа: {group_name}\n"
        f"Тип: {booking_type}\n"
        f"Комментарий: {comment}\n"
        f"Контакт: {contact_info}"
    )
    return note


@admin_bot.message_handler(func=lambda message: message.text == "Просмотреть неподтвержденные брони")
def handle_view_unconfirmed(message):
    admin_id = message.from_user.id
    if not is_admin(admin_id):
        admin_bot.send_message(message.chat.id, "❌ У вас нет прав для выполнения этой операции.")
        return
    try:
        booking_ids = _load_unconfirmed_booking_ids_mysql()
    except Exception as error:
        logger.error("Ошибка загрузки неподтвержденных броней: %s", error)
        admin_bot.send_message(message.chat.id, "Произошла ошибка при загрузке неподтвержденных броней.")
        show_menu(message)
        return
    if not booking_ids:
        admin_bot.send_message(message.chat.id, "Нет неподтвержденных броней.")
        show_menu(message)
        return
    for booking_id in booking_ids:
        try:
            booking_info = get_booking_info_by_id(booking_id)
        except Exception as error:
            logger.error("Не удалось получить информацию о брони %s: %s", booking_id, error)
            continue

        if not booking_info:
            continue
        try:
            note_text = _format_unconfirmed_booking_note(booking_info)

            markup = types.InlineKeyboardMarkup()
            confirm_btn = types.InlineKeyboardButton("✅ Подтвердить", callback_data=f"confirm:{booking_id}")
            reject_btn = types.InlineKeyboardButton("❌ Отклонить", callback_data=f"reject:{booking_id}")
            markup.add(confirm_btn, reject_btn)
            admin_bot.send_message(message.chat.id, note_text, reply_markup=markup, parse_mode="Markdown")
        except Exception as error:
            logger.error("Не удалось отправить сообщение по брони %s: %s", booking_id, error)
            continue
    show_menu(message)


### Отмена брони

@admin_bot.message_handler(func=lambda msg: msg.text == "Отменить бронь")
def handle_cancel_booking(message):
    admin_id = message.from_user.id
    if not is_admin(admin_id):
        admin_bot.send_message(message.chat.id, "❌ У вас нет прав.")
        return
    conn = get_connection()
    try:
        cur = conn.cursor()
        today = datetime.now().strftime("%Y-%m-%d")
        cur.execute("SELECT DISTINCT date FROM slots WHERE date >= %s AND status = 2 ORDER BY date", (today,))
        rows = cur.fetchall()
    finally:
        conn.close()
    if not rows:
        admin_bot.send_message(message.chat.id, "Нет доступных дней с подтвержденными бронями.")
        return
    iso_dates = [d.strftime("%Y-%m-%d") if hasattr(d, "strftime") else str(d) for (d,) in rows]
    btn_map = {format_date(d): d for d in iso_dates}
    markup = types.ReplyKeyboardMarkup(resize_keyboard=True, row_width=3)
    labels = list(btn_map.keys())
    for i in range(0, len(labels), 3):
        markup.add(*[types.KeyboardButton(lbl) for lbl in labels[i:i+3]])
    markup.add(types.KeyboardButton("На главную"))
    user_states[admin_id] = {"step": "choose_cancel_day", "date_btn_map": btn_map}
    admin_bot.send_message(message.chat.id, "Выберите день:", reply_markup=markup)


@admin_bot.message_handler(func=lambda msg: user_states.get(msg.from_user.id, {}).get("step") == "choose_cancel_day")
def handle_choose_cancel_day(message):
    admin_id = message.from_user.id
    text = message.text.strip()
    if text == "На главную":
        show_menu(message)
        return
    state = user_states.get(admin_id, {})
    date_iso = state.get("date_btn_map", {}).get(text)
    if not date_iso:
        admin_bot.send_message(message.chat.id, "Выберите корректную дату.")
        return
    conn = get_connection()
    try:
        cur = conn.cursor()
        cur.execute("SELECT DISTINCT booking_id FROM slots WHERE date = %s AND status = 2", (date_iso,))
        rows = cur.fetchall()
    finally:
        conn.close()
    booking_ids = [int(r[0]) for r in rows if r]
    if not booking_ids:
        admin_bot.send_message(message.chat.id, "Нет брони на этот день.")
        return
    items = []
    for bid in booking_ids:
        info = get_booking_info_by_id(bid)
        if not info: 
            continue
        label = f"{info['start_time']}-{info['end_time']} - {info['group_name']}"
        items.append((label, bid))
    markup = types.ReplyKeyboardMarkup(resize_keyboard=True, row_width=3)
    row = []
    for i, (label, _bid) in enumerate(items, start=1):
        row.append(types.KeyboardButton(label))
        if i % 3 == 0:
            markup.add(*row)
            row = []
    if row:
        markup.add(*row)
    markup.add("На главную")

    state.update({
        "step": "choose_booking_for_cancel",
        "bookings": dict(items),
        "date_iso": date_iso
    })
    user_states[admin_id] = state
    admin_bot.send_message(message.chat.id, "Выберите бронь:", reply_markup=markup)


@admin_bot.message_handler(func=lambda msg: user_states.get(msg.from_user.id, {}).get("step") == "choose_booking_for_cancel")
def handle_choose_booking_for_cancel(message):
    admin_id = message.from_user.id
    text = message.text.strip()
    state = user_states.get(admin_id, {})
    if text == "На главную":
        show_menu(message)
        return
    bookings = state.get("bookings", {})
    booking_id = bookings.get(text)
    if not booking_id:
        admin_bot.send_message(message.chat.id, "Выберите бронь из списка.")
        return
    info = get_booking_info_by_id(booking_id)
    if not info:
        admin_bot.send_message(message.chat.id, "Ошибка при получении информации.")
        return
    confirm_kb = types.ReplyKeyboardMarkup(resize_keyboard=True, row_width=3)
    confirm_kb.add("Да", "Отмена", "Выбрать другую бронь")
    state.update({
        "step": "confirm_cancel",
        "pending_booking_id": booking_id
    })
    user_states[admin_id] = state
    admin_bot.send_message(
        message.chat.id,
        f"Вы действительно хотите отменить бронь?\n{info['date']} {info['start_time']}-{info['end_time']} {info['group_name']}",
        reply_markup=confirm_kb
    )


@admin_bot.message_handler(func=lambda msg: user_states.get(msg.from_user.id, {}).get("step") == "confirm_cancel")
def handle_confirm_cancel_choice(message):
    admin_id = message.from_user.id
    text = (message.text or "").strip()
    state = user_states.get(admin_id, {}) or {}
    booking_id = state.get("pending_booking_id")
    if not booking_id:
        admin_bot.send_message(message.chat.id, "Сессия устарела. Начните заново.")
        return show_menu(message)
    if text == "Отмена":
        reset_user_state(admin_id, user_states)
        show_menu(message)
        return
    if text == "Выбрать другую бронь":
        conn = get_connection()
        try:
            cur = conn.cursor()
            today = datetime.now().strftime("%Y-%m-%d")
            cur.execute("SELECT DISTINCT date FROM slots WHERE date >= %s AND status = 2 ORDER BY date", (today,))
            rows = cur.fetchall()
        finally:
            conn.close()
        if not rows:
            reset_user_state(admin_id, user_states)
            admin_bot.send_message(message.chat.id, "Нет доступных дней с подтвержденными брони.")
            return show_menu(message)
        iso_dates = [d.strftime("%Y-%m-%d") if hasattr(d, "strftime") else str(d) for (d,) in rows]
        btn_map = {format_date(d): d for d in iso_dates}
        markup = types.ReplyKeyboardMarkup(resize_keyboard=True, row_width=3)
        labels = list(btn_map.keys())
        for i in range(0, len(labels), 3):
            markup.add(*[types.KeyboardButton(lbl) for lbl in labels[i:i+3]])
        markup.add(types.KeyboardButton("На главную"))
        state = {"step": "choose_cancel_day", "date_btn_map": btn_map}
        user_states[admin_id] = state
        admin_bot.send_message(message.chat.id, "Выберите день:", reply_markup=markup)
        return
    if text == "Да":
        state["step"] = "ask_notify_subscribers_for_cancel"
        user_states[admin_id] = state

        kb = types.ReplyKeyboardMarkup(resize_keyboard=True, row_width=2)
        kb.add(types.KeyboardButton("Да"), types.KeyboardButton("Нет"))
        admin_bot.send_message(message.chat.id, "Уведомить подписавшихся?", reply_markup=kb)
        return
    kb = types.ReplyKeyboardMarkup(resize_keyboard=True, row_width=3)
    kb.add("Да", "Отмена", "Выбрать другую бронь")
    admin_bot.send_message(message.chat.id, "Пожалуйста, выберите действие:", reply_markup=kb)


@admin_bot.message_handler(func=lambda msg: user_states.get(msg.from_user.id, {}).get("step") == "ask_notify_subscribers_for_cancel")
def handle_notify_subscribers_for_cancel(message):
    admin_id = message.from_user.id
    text = (message.text or "").strip()
    state = user_states.get(admin_id, {}) or {}
    booking_id = state.get("pending_booking_id")
    if text not in ("Да", "Нет"):
        kb = types.ReplyKeyboardMarkup(resize_keyboard=True, row_width=2)
        kb.add(types.KeyboardButton("Да"), types.KeyboardButton("Нет"))
        admin_bot.send_message(message.chat.id, "Выберите: Да или Нет.", reply_markup=kb)
        return
    slot_ids = []
    try:
        slot_ids = get_slot_ids_by_booking(booking_id)
    except Exception:
        slot_ids = []
    try:
        reject_booking(booking_id)
    except Exception as e:
        admin_bot.send_message(message.chat.id, f"Ошибка отмены: {e}")
        reset_user_state(admin_id, user_states)
        return show_menu(message)
    if text == "Да" and slot_ids:
        try:
            notify_subscribers_for_cancellation({"ids": slot_ids}, main_bot)
        except Exception as e:
            logger.error(f"Не удалось оповестить подписчиков: {e}")
    try:
        info = get_booking_info_by_id(booking_id)
    except Exception:
        info = None
    creator_id = None
    try:
        creator_id = get_user_id_by_booking(booking_id)
    except Exception:
        creator_id = None
    if creator_id and info:
        try:
            start_time = (info.get("start_time") or "").strip()
            end_time = (info.get("end_time") or "").strip()
            group_name = (info.get("group_name") or "").strip()
            date_str = (info.get("date") or "").strip()
            main_bot.send_message(
                int(creator_id),
                f"🚫 Ваша бронь для группы «{group_name or '-неизвестная группа-'}» {date_str} {start_time}–{end_time} была отменена администратором."
            )
        except Exception as e:
            logger.error(f"Не удалось отправить уведомление пользователю {creator_id}: {e}")
    kb = types.ReplyKeyboardMarkup(resize_keyboard=True, row_width=2)
    kb.add(types.KeyboardButton("Отменить другую бронь"), types.KeyboardButton("На главную"))
    state["step"] = "post_cancel_options"
    user_states[admin_id] = state
    admin_bot.send_message(message.chat.id, "Бронь отменена.", reply_markup=kb)


@admin_bot.message_handler(func=lambda msg: user_states.get(msg.from_user.id, {}).get("step") == "post_cancel_options")
def handle_post_cancel_options(message):
    admin_id = message.from_user.id
    text = (message.text or "").strip()
    if text == "На главную":
        reset_user_state(admin_id, user_states)
        return show_menu(message)
    if text == "Отменить другую бронь":
        conn = get_connection()
        try:
            cur = conn.cursor()
            cur.execute("SELECT DISTINCT date FROM slots WHERE status = 2 ORDER BY date")
            rows = cur.fetchall()
        finally:
            conn.close()
        if not rows:
            reset_user_state(admin_id, user_states)
            admin_bot.send_message(message.chat.id, "Нет доступных дней с подтвержденными брони.")
            return show_menu(message)
        iso_dates = [d.strftime("%Y-%m-%d") if hasattr(d, "strftime") else str(d) for (d,) in rows]
        btn_map = {format_date(d): d for d in iso_dates}
        markup = types.ReplyKeyboardMarkup(resize_keyboard=True, row_width=3)
        labels = list(btn_map.keys())
        for i in range(0, len(labels), 3):
            markup.add(*[types.KeyboardButton(lbl) for lbl in labels[i:i+3]])
        markup.add(types.KeyboardButton("На главную"))
        user_states[admin_id] = {"step": "choose_cancel_day", "date_btn_map": btn_map}
        admin_bot.send_message(message.chat.id, "Выберите день:", reply_markup=markup)
        return
    kb = types.ReplyKeyboardMarkup(resize_keyboard=True, row_width=2)
    kb.add(types.KeyboardButton("Отменить другую бронь"), types.KeyboardButton("На главную"))
    admin_bot.send_message(message.chat.id, "Пожалуйста, выберите действие:", reply_markup=kb)


### inline-хендлеры ###

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


@admin_bot.callback_query_handler(func=lambda c: c.data.startswith("cancel_booking_id:"))
def cb_cancel_by_booking_id(c):
    try:
        booking_id = int(c.data.split(":")[1])
    except Exception:
        admin_bot.answer_callback_query(c.id, "Ошибка: неверный booking_id")
        return
    user_id = get_user_id_by_booking(booking_id)
    info = get_booking_info_by_id(booking_id)
    slot_ids = []
    try:
        slot_ids = get_slot_ids_by_booking(booking_id)
    except Exception:
        slot_ids = []
    if slot_ids:
        try:
            notify_subscribers_for_cancellation({"ids": slot_ids}, main_bot)
        except Exception as e:
            logger.error(f"Не удалось оповестить подписчиков: {e}")
    try:
        reject_booking(booking_id)
    except Exception as e:
        admin_bot.answer_callback_query(c.id, f"Ошибка отмены: {e}")
        return
    if user_id and info:
        group_name = info.get("group_name") or ""
        start_time = info.get("start_time") or ""
        end_time = info.get("end_time") or ""
        date_str = info.get("date") or ""
        try:
            main_bot.send_message(
                int(user_id),
                f"🚫 Ваша бронь для группы «{group_name or '-неизвестная группа-'}» {date_str}  {start_time}–{end_time} была отменена администратором по вашей заявке.",
            )
        except Exception as e:
            logger.error(f"Не удалось отправить уведомление пользователю {user_id}: {e}")
    admin_bot.answer_callback_query(c.id, "Отменено")
    admin_bot.edit_message_reply_markup(c.message.chat.id, c.message.message_id, reply_markup=None)


def main():
    admin_bot.polling(none_stop=True)


if __name__ == "__main__":
    main()
