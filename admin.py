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


@admin_bot.message_handler(func=lambda msg: msg.text == "Просмотреть неподтвержденные брони")
def show_unconfirmed_bookings(message):
    if not is_admin(message.from_user.id):
        admin_bot.send_message(message.chat.id, "❌ Нет прав.")
        return
    conn = get_connection()
    try:
        cur = conn.cursor()
        cur.execute("""
            SELECT DISTINCT booking_id
            FROM slots
            WHERE status = 1 AND booking_id IS NOT NULL
            ORDER BY booking_id
        """)
        rows = cur.fetchall()
    finally:
        try:
            conn.close()
        except Exception:
            pass
    booking_ids = []
    for r in rows:
        v = r[0] if isinstance(r, (list, tuple)) else r
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
        admin_bot.send_message(message.chat.id, "Нет неподтвержденных броней.")
        return
    items = []
    for bid in booking_ids:
        try:
            info = get_booking_info_by_id(bid)
        except Exception:
            info = None
        if not info:
            continue
        date_str_human = (info.get("date") or "").strip()
        start_human = (info.get("start_time") or "").strip()
        end_human = (info.get("end_time") or "").strip()
        group_name = (info.get("group_name") or "—").strip()
        label = f"{date_str_human} {start_human}–{end_human} · {group_name}"
        items.append({
            "booking_id": bid,
            "label": label,
            "info": info,
        })
    if not items:
        admin_bot.send_message(message.chat.id, "Нет неподтвержденных броней.")
        return
    state = {
        "step": "choose_unconfirmed_booking",
        "items": items
    }
    user_states[message.chat.id] = state
    markup = types.ReplyKeyboardMarkup(resize_keyboard=True)
    for it in items:
        markup.add(types.KeyboardButton(it["label"]))
    markup.add(types.KeyboardButton("На главную"))
    admin_bot.send_message(message.chat.id, "Выберите бронь:", reply_markup=markup)


@admin_bot.message_handler(func=lambda msg: user_states.get(msg.chat.id, {}).get("step") == "choose_unconfirmed_booking")
def handle_choose_unconfirmed_booking(message):
    if message.text == "На главную":
        show_menu(message)
        return
    state = user_states.get(message.chat.id, {}) or {}
    items = state.get("items", [])
    selected = None
    incoming = (message.text or "").strip()
    for it in items:
        if it["label"] == incoming:
            selected = it
            break
    if not selected:
        admin_bot.send_message(message.chat.id, "Выберите бронь из списка.")
        return
    markup = types.ReplyKeyboardMarkup(resize_keyboard=True)
    markup.add("✅ Подтвердить", "❌ Отклонить")
    markup.add("На главную")
    state["selected_booking_id"] = selected["booking_id"]
    state["selected_label"] = selected["label"]
    state["step"] = "decide_unconfirmed_booking"
    user_states[message.chat.id] = state
    admin_bot.send_message(message.chat.id, f"Выбранная бронь: {selected['label']}", reply_markup=markup)


@admin_bot.message_handler(func=lambda msg: user_states.get(msg.chat.id, {}).get("step") == "decide_unconfirmed_booking")
def handle_decide_unconfirmed_booking(message):
    if message.text == "На главную":
        show_menu(message)
        return
    state = user_states.get(message.chat.id, {}) or {}
    booking_id = state.get("selected_booking_id")
    if not booking_id:
        admin_bot.send_message(message.chat.id, "Сессия устарела. Начните заново.")
        show_menu(message)
        return
    if message.text == "✅ Подтвердить":
        try:
            confirm_booking(booking_id)
            admin_bot.send_message(message.chat.id, "Бронь подтверждена ✅")
        except Exception as e:
            admin_bot.send_message(message.chat.id, f"Ошибка подтверждения: {e}")
            return
    elif message.text == "❌ Отклонить":
        try:
            reject_booking(booking_id)
            admin_bot.send_message(message.chat.id, "Бронь отклонена ❌")
        except Exception as e:
            admin_bot.send_message(message.chat.id, f"Ошибка отклонения: {e}")
            return
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
