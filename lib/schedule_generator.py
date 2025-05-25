import sqlite3
from datetime import datetime
from PIL import Image, ImageDraw, ImageFont
from lib.utils import is_admin, format_date
from lib.schedule_tasks import get_schedule_for_day

def create_schedule_grid_image(requester_id=None, days_to_show=28):
    today = datetime.now().strftime("%Y-%m-%d")

    conn = sqlite3.connect('bookings.db', check_same_thread=False)
    cursor = conn.cursor()

    cursor.execute(
        'SELECT DISTINCT date FROM slots WHERE date >= ? ORDER BY date LIMIT ?',
        (today, days_to_show)
    )
    dates = [row[0] for row in cursor.fetchall()]
    conn.close()

    if not dates:
        return None

    schedules = {
        date: [
            (t, s, g) for t, s, g in get_schedule_for_day(date, requester_id)
            if "11:00" <= t <= "23:00"
        ]
        for date in dates
    }

    max_slots = max(len(slots) for slots in schedules.values()) if schedules else 1
    cell_width, cell_height, padding = 450, 70, 10

    try:
        font_path = "/usr/share/fonts/truetype/dejavu/DejaVuSans.ttf"
        bold_font_path = "/usr/share/fonts/truetype/dejavu/DejaVuSans-Bold.ttf"
        time_font = ImageFont.truetype(bold_font_path, 26)
        group_font = ImageFont.truetype(font_path, 24)
        date_font = ImageFont.truetype(bold_font_path, 32)
    except OSError:
        time_font = group_font = date_font = ImageFont.load_default()

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
                date = dates[index]
                x = padding + col * (cell_width + padding)
                y = padding + row_offset * ((max_slots + 1) * (cell_height + padding)) + (row_index + 1) * (cell_height + padding)
                try:
                    time, status, group_name = schedules[date][row_index]
                except IndexError:
                    time, status, group_name = "", 0, ""
                if status > 0:
                    if is_admin(requester_id):
                        cursor = sqlite3.connect('bookings.db').cursor()
                        cursor.execute('SELECT status FROM slots WHERE date = ? AND time = ?', (date, time))
                        status = cursor.fetchone()[0]
                        cursor.close()
                        if status == 2:
                            bg_color = (255, 180, 180)
                        elif status == 1:
                            bg_color = (255, 200, 150)
                        else:
                            bg_color = (255, 200, 200)
                    else:
                        bg_color = (255, 180, 180)
                else:
                    bg_color = (200, 255, 200)
                draw.rectangle([x, y, x + cell_width, y + cell_height], fill=bg_color, outline="black")
                draw.text((x + padding, y + (cell_height - 26) // 2), time, fill="black", font=time_font)

                if status > 0:
                    label = group_name if is_admin(requester_id) else "Занято"
                    fitted_font = group_font
                    while True:
                        line_width = draw.textbbox((0, 0), label, font=fitted_font)[2]
                        if line_width <= cell_width * 0.6 or fitted_font.size <= 28:
                            break
                        fitted_font = ImageFont.truetype(font_path, fitted_font.size - 1)
                    draw.text(
                        (x + cell_width // 4 + padding, y + (cell_height - fitted_font.size) // 2),
                        label,
                        fill="black",
                        font=fitted_font
                    )

    path = "schedule_grid.png"
    img.save(path, dpi=(300, 300))
    return path