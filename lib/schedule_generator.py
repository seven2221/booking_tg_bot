from datetime import datetime
from PIL import Image, ImageDraw, ImageFont
from lib.db_init import get_connection
from lib.utils import is_admin, format_date
from lib.schedule_tasks import (
    get_schedule_for_day,
    get_daily_schedule_from_db,
)

def _load_upcoming_dates(days_to_show=28):
    today = datetime.now().strftime("%Y-%m-%d")
    conn = get_connection()
    try:
        cur = conn.cursor(buffered=True)
        try:
            cur.execute(
                "SELECT DISTINCT date FROM slots WHERE date >= %s ORDER BY date LIMIT %s",
                (today, days_to_show),
            )
            rows = cur.fetchall()
        finally:
            cur.close()
    finally:
        conn.close()
    return [
        (row[0].strftime("%Y-%m-%d") if hasattr(row[0], "strftime") else str(row[0]))
        for row in rows
    ]


def _load_slot_status(date_str: str, time_str: str) -> int:
    conn = get_connection()
    try:
        cur = conn.cursor(buffered=True)
        try:
            cur.execute(
                "SELECT status FROM slots WHERE date = %s AND time = %s LIMIT 1",
                (date_str, time_str),
            )
            row = cur.fetchone()
            return int(row[0]) if row and row[0] is not None else 0
        finally:
            cur.close()
    finally:
        conn.close()


def _get_fonts():
    try:
        font_path = "/usr/share/fonts/truetype/dejavu/DejaVuSans.ttf"
        bold_font_path = "/usr/share/fonts/truetype/dejavu/DejaVuSans-Bold.ttf"
        time_font = ImageFont.truetype(bold_font_path, 26)
        group_font = ImageFont.truetype(font_path, 24)
        date_font = ImageFont.truetype(bold_font_path, 32)
    except OSError:
        time_font = group_font = date_font = ImageFont.load_default()
    return time_font, group_font, date_font

def create_schedule_grid_image(requester_id=None, days_to_show=28):
    dates = _load_upcoming_dates(days_to_show)
    if not dates:
        return None

    schedules = {
        date: [
            (t, s, g) for t, s, g in get_schedule_for_day(date, requester_id)
            if "11:00" <= t <= "23:00"
        ]
        for date in dates
    }

    max_slots = max((len(slots) for slots in schedules.values()), default=1)
    cell_width, cell_height, padding = 450, 70, 10
    time_font, group_font, date_font = _get_fonts()

    cols = 7
    rows = (len(dates) + cols - 1) // cols
    img_width = cols * (cell_width + padding) + padding
    img_height = rows * ((max_slots + 1) * (cell_height + padding)) + padding

    img = Image.new("RGB", (img_width, img_height), color="white")
    draw = ImageDraw.Draw(img)

    for row_offset in range(rows):
        for col in range(cols):
            index = row_offset * cols + col
            if index >= len(dates):
                break
            date = dates[index]
            x = padding + col * (cell_width + padding)
            y = padding + row_offset * ((max_slots + 1) * (cell_height + padding))
            draw.rectangle([x, y, x + cell_width, y + cell_height], fill=(220, 220, 220))

            formatted_date = format_date(date)
            bbox = draw.textbbox((0, 0), formatted_date, font=date_font)
            tx = x + (cell_width - bbox[2]) // 2
            ty = y + (cell_height - bbox[3]) // 2
            draw.text((tx, ty), formatted_date, fill="black", font=date_font)

    for row_offset in range(rows):
        for row_index in range(max_slots):
            for col in range(cols):
                index = row_offset * cols + col
                if index >= len(dates):
                    break
                date = dates[index]  # строка
                x = padding + col * (cell_width + padding)
                y = (
                    padding
                    + row_offset * ((max_slots + 1) * (cell_height + padding))
                    + (row_index + 1) * (cell_height + padding)
                )
                try:
                    time_str, status, group_name = schedules[date][row_index]
                except IndexError:
                    time_str, status, group_name = "", 0, ""

                if status > 0 and is_admin(requester_id):
                    status = _load_slot_status(date, time_str)
                    if status == 2:
                        bg_color = (255, 180, 180)
                    elif status == 1:
                        bg_color = (255, 200, 150)
                    else:
                        bg_color = (255, 200, 200)
                else:
                    bg_color = (200, 255, 200)

                draw.rectangle([x, y, x + cell_width, y + cell_height], fill=bg_color, outline="black")
                draw.text(
                    (x + padding, y + (cell_height - 26) // 2),
                    time_str,
                    fill="black",
                    font=time_font,
                )

                if status > 0:
                    label = group_name if is_admin(requester_id) else "Занято"
                    fitted_font = group_font
                    try:
                        font_path = "/usr/share/fonts/truetype/dejavu/DejaVuSans.ttf"
                        while True:
                            line_width = draw.textbbox((0, 0), label, font=fitted_font)[2]
                            if line_width <= cell_width * 0.6 or getattr(fitted_font, "size", 28) <= 28:
                                break
                            fitted_font = ImageFont.truetype(
                                font_path, getattr(fitted_font, "size", 28) - 1
                            )
                    except OSError:
                        pass
                    draw.text(
                        (x + cell_width // 4 + padding, y + (cell_height - getattr(fitted_font, "size", 24)) // 2),
                        label,
                        fill="black",
                        font=fitted_font,
                    )

    path = "schedule_grid.png"
    img.save(path, dpi=(300, 300))
    return path

def _text_center(draw, text, x, y, w, h, font):
    bbox = draw.textbbox((0, 0), text, font=font)
    tx = x + (w - bbox[2]) // 2
    ty = y + (h - bbox[3]) // 2
    draw.text((tx, ty), text, font=font, fill="black")

def _multiline_center(draw, text, x, y, w, h, font, cell_padding=10, max_lines=2):
    words = (text or "").split()
    lines = []
    line = ""
    for word in words:
        test = f"{line} {word}".strip()
        if draw.textbbox((0, 0), test, font=font)[2] <= w - 2 * cell_padding:
            line = test
        else:
            lines.append(line)
            line = word
        if len(lines) == max_lines:
            break
    if line and len(lines) < max_lines:
        lines.append(line)
    total_height = sum(draw.textbbox((0, 0), l, font=font)[3] for l in lines) if lines else 0
    ty = y + (h - total_height) // 2
    for l in lines:
        draw.text((x + cell_padding, ty), l, font=font, fill="black")
        ty += draw.textbbox((0, 0), l, font=font)[3]

def create_daily_schedule_image(requester_id=None):
    today = datetime.now().strftime("%Y-%m-%d")
    raw_slots = get_daily_schedule_from_db(today)
    if not raw_slots:
        return None

    cell_padding = 10
    row_height = 60
    column_widths = [100, 200, 150, 250]
    headers = ["Время", "Группа", "Тип", "Комментарий"]

    try:
        font_path = "/usr/share/fonts/truetype/dejavu/DejaVuSans.ttf"
        bold_font_path = "/usr/share/fonts/truetype/dejavu/DejaVuSans-Bold.ttf"
        header_font = ImageFont.truetype(bold_font_path, 20)
        text_font = ImageFont.truetype(font_path, 18)
    except OSError:
        header_font = text_font = ImageFont.load_default()

    merge_map = {}
    i = 0
    while i < len(raw_slots):
        slot = raw_slots[i]
        group = slot.get("group_name")
        btype = slot.get("booking_type")
        comment = slot.get("comment")
        rowspan = 1
        for j in range(i + 1, len(raw_slots)):
            other = raw_slots[j]
            if (
                group
                and other.get("group_name") == group
                and other.get("booking_type") == btype
                and other.get("comment") == comment
            ):
                rowspan += 1
            else:
                break
        merge_map[i] = rowspan
        i += rowspan

    total_rows = len(raw_slots)
    img_height = (total_rows + 1) * (row_height + cell_padding) + cell_padding
    img_width = sum(column_widths) + cell_padding * (len(headers) + 1)

    img = Image.new("RGB", (img_width, img_height), color="white")
    draw = ImageDraw.Draw(img)

    x = cell_padding
    y = cell_padding
    for i, h in enumerate(headers):
        draw.rectangle([x, y, x + column_widths[i], y + row_height], fill=(220, 220, 220), outline="black")
        _text_center(draw, h, x, y, column_widths[i], row_height, header_font)
        x += column_widths[i] + cell_padding

    y_offset = y + row_height + cell_padding
    drawn = set()

    def get_bg_color(status_val):
        if status_val == 2:
            return (255, 180, 180)
        elif status_val == 1:
            return (255, 200, 150)
        else:
            return (200, 255, 200)

    for row_index in range(total_rows):
        slot = raw_slots[row_index]
        row_y = y_offset + row_index * (row_height + cell_padding)
        x = cell_padding

        status_val = slot.get("status", 0) or 0
        bg_color = get_bg_color(status_val)

        draw.rectangle([x, row_y, x + column_widths[0], row_y + row_height], outline="black", fill=bg_color)
        _text_center(draw, slot.get("time", ""), x, row_y, column_widths[0], row_height, text_font)
        x += column_widths[0] + cell_padding

        for j, key in enumerate(["group_name", "booking_type", "comment"]):
            if row_index in drawn:
                x += column_widths[j + 1] + cell_padding
                continue

            rowspan = merge_map.get(row_index, 1)
            height = row_height if rowspan == 1 else (rowspan * (row_height + cell_padding)) - cell_padding

            draw.rectangle([x, row_y, x + column_widths[j + 1], row_y + height], outline="black", fill=bg_color)

            value = slot.get(key) or ""
            if key == "group_name" and not is_admin(requester_id) and value:
                value = "Занято"

            _multiline_center(draw, value, x, row_y, column_widths[j + 1], height, text_font, cell_padding=cell_padding)

            for k in range(row_index + 1, row_index + rowspan):
                drawn.add(k)
            x += column_widths[j + 1] + cell_padding

    path = "daily_schedule.png"
    img.save(path, dpi=(300, 300))
    return path
