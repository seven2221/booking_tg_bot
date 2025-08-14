from datetime import datetime
from PIL import Image, ImageDraw, ImageFont
from lib.db_init import get_connection
from lib.utils import is_admin, format_date
from lib.schedule_tasks import (
    get_schedule_for_day,
    get_daily_schedule_from_db,
)

def _load_upcoming_dates(number_of_days):
    from datetime import datetime, timedelta
    today = datetime.now().date()
    return [(today + timedelta(days=i)).strftime("%Y-%m-%d") for i in range(number_of_days)]


def _load_slot_status(date_str: str, time_str: str) -> int:
    conn = get_connection()
    try:
        cur = conn.cursor(buffered=True)
        try:
            cur.execute(
                "SELECT status FROM slots WHERE date = %s AND time = %s LIMIT 1",
                (date_str, time_str)
            )
            row = cur.fetchone()
            return int(row[0]) if row and row[0] is not None else 0
        finally:
            cur.close()
    finally:
        conn.close()


def _load_slot_status_and_group(date_str: str, time_str: str) -> tuple[int, str | None]:
    conn = get_connection()
    try:
        cur = conn.cursor(buffered=True)
        try:
            cur.execute(
                "SELECT status, group_name FROM slots WHERE date = %s AND time = %s LIMIT 1",
                (date_str, time_str)
            )
            row = cur.fetchone()
            if not row:
                return 0, None
            status = int(row[0]) if row[0] is not None else 0
            group_name = row[1]
            return status, group_name
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
    dates_to_show = _load_upcoming_dates(days_to_show)
    if not dates_to_show:
        return None
    schedule_by_date = {
        date_value: [
            (time_str, status_value, group_name)
            for (time_str, status_value, _booking_id, group_name) in get_schedule_for_day(date_value, requester_id)
            if "11:00" <= time_str <= "23:00"
        ]
        for date_value in dates_to_show
    }
    max_slots_per_day = max((len(slots) for slots in schedule_by_date.values()), default=1)
    cell_width, cell_height, padding = 450, 70, 10
    time_font, group_font, date_font = _get_fonts()
    columns = 7
    rows = (len(dates_to_show) + columns - 1) // columns
    image_width = columns * (cell_width + padding) + padding
    image_height = rows * ((max_slots_per_day + 1) * (cell_height + padding)) + padding
    image = Image.new("RGB", (image_width, image_height), color="white")
    draw = ImageDraw.Draw(image)
    for row_index in range(rows):
        for column_index in range(columns):
            index = row_index * columns + column_index
            if index >= len(dates_to_show):
                break
            date_value = dates_to_show[index]
            x = padding + column_index * (cell_width + padding)
            y = padding + row_index * ((max_slots_per_day + 1) * (cell_height + padding))
            draw.rectangle([x, y, x + cell_width, y + cell_height], fill=(220, 220, 220))
            formatted_date = format_date(date_value)
            bbox = draw.textbbox((0, 0), formatted_date, font=date_font)
            text_left, text_top, text_right, text_bottom = bbox
            text_width = text_right - text_left
            text_height = text_bottom - text_top
            text_x = x + (cell_width - text_width) // 2
            text_y = y + (cell_height - text_height) // 2
            draw.text((text_x, text_y), formatted_date, fill="black", font=date_font)
    is_admin_mode = is_admin(requester_id)
    for row_index in range(rows):
        for slot_index in range(max_slots_per_day):
            for column_index in range(columns):
                index = row_index * columns + column_index
                if index >= len(dates_to_show):
                    break
                date_value = dates_to_show[index]
                x = padding + column_index * (cell_width + padding)
                y = (
                    padding
                    + row_index * ((max_slots_per_day + 1) * (cell_height + padding))
                    + (slot_index + 1) * (cell_height + padding)
                )
                try:
                    time_str, status_prefetched, group_prefetched = schedule_by_date[date_value][slot_index]
                except IndexError:
                    time_str, status_prefetched, group_prefetched = "", 0, ""
                if status_prefetched > 0 and is_admin_mode:
                    live_status = _load_slot_status(date_value, time_str) if time_str else 0
                    if live_status == 2:
                        background_color = (255, 180, 180)
                    elif live_status == 1:
                        background_color = (255, 200, 150)
                    else:
                        background_color = (255, 200, 200)
                else:
                    background_color = (200, 255, 200)
                draw.rectangle([x, y, x + cell_width, y + cell_height], fill=background_color, outline="black")
                if time_str:
                    time_y = y + (cell_height - 26) // 2
                    draw.text((x + padding, time_y), time_str, fill="black", font=time_font)
                cell_text = ""
                if time_str:
                    if is_admin_mode:
                        status_live, group_live = _load_slot_status_and_group(date_value, time_str)
                        if status_live and status_live > 0:
                            cell_text = group_live or group_prefetched or "занято"
                    else:
                        status_live = _load_slot_status(date_value, time_str)
                        if status_live and status_live > 0:
                            cell_text = "занято"
                if cell_text:
                    bbox2 = draw.textbbox((0, 0), cell_text, font=group_font)
                    t_left, t_top, t_right, t_bottom = bbox2
                    text_width2 = t_right - t_left
                    text_height2 = t_bottom - t_top
                    text_x2 = x + (cell_width - text_width2) // 2
                    text_y2 = y + (cell_height - text_height2) // 2
                    draw.text((text_x2, text_y2), cell_text, font=group_font, fill="black")
    output_path = "schedule_grid.png"
    image.save(output_path, dpi=(300, 300))
    return output_path


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
