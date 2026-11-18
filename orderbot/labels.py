"""Наклейки на коробки: номер заказа, заведение, адрес, слот.

Печатаются на обычном A4 — 8 наклеек на лист (2 колонки × 4 строки).
Нужно, чтобы на сборке не путать заказы, а курьеру не сверяться с телефоном.
"""
from __future__ import annotations

from datetime import datetime
from io import BytesIO
from typing import Any

from fpdf import FPDF

FONT = "/usr/share/fonts/truetype/dejavu/DejaVuSans.ttf"
FONT_BOLD = "/usr/share/fonts/truetype/dejavu/DejaVuSans-Bold.ttf"

WIDTH = 90.0
HEIGHT = 66.0
COLS = 2
ROWS = 4


def _clip(text: Any, limit: int) -> str:
    value = str(text or "").strip()
    return value if len(value) <= limit else value[: limit - 1] + "…"


def build_labels(orders: list[dict[str, Any]], day: str) -> bytes:
    pdf = FPDF(orientation="P", unit="mm", format="A4")
    pdf.set_auto_page_break(auto=False)
    pdf.add_font("DejaVu", "", FONT)
    pdf.add_font("DejaVu", "B", FONT_BOLD)
    pdf.add_page()

    per_page = COLS * ROWS
    for index, order in enumerate(orders):
        if index and index % per_page == 0:
            pdf.add_page()
        slot = index % per_page
        col = slot % COLS
        row = slot // COLS
        x = 10 + col * (WIDTH + 5)
        y = 10 + row * (HEIGHT + 4)

        pdf.set_draw_color(120, 120, 120)
        pdf.rect(x, y, WIDTH, HEIGHT)

        pdf.set_xy(x + 3, y + 2)
        pdf.set_font("DejaVu", "B", 16)
        pdf.cell(WIDTH - 6, 9, f"№ {order.get('id')}   {_clip(day, 12)}")

        pdf.set_xy(x + 3, y + 12)
        pdf.set_font("DejaVu", "", 8)
        pdf.cell(WIDTH - 6, 5, f"Слот: {_clip(order.get('delivery_time') or '—', 20)}")

        pdf.set_xy(x + 3, y + 18)
        pdf.set_font("DejaVu", "B", 11)
        pdf.multi_cell(
            WIDTH - 6,
            6,
            _clip(order.get("chef_name") or order.get("chef_id") or "Клиент", 40),
        )

        pdf.set_x(x + 3)
        pdf.set_font("DejaVu", "", 9)
        address = order.get("delivery_address") or "адрес не указан"
        pdf.multi_cell(WIDTH - 6, 5, _clip(address, 90))

        pdf.set_x(x + 3)
        phone = order.get("delivery_phone") or ""
        if phone:
            pdf.set_font("DejaVu", "", 9)
            pdf.multi_cell(WIDTH - 6, 5, _clip(phone, 30))

        comment = order.get("comment")
        if comment:
            pdf.set_x(x + 3)
            pdf.set_font("DejaVu", "", 8)
            pdf.multi_cell(WIDTH - 6, 4.5, _clip(comment, 80))

        items = [item for item in order.get("items") or [] if item.get("status") != "skipped"]
        pdf.set_xy(x + 3, y + HEIGHT - 12)
        pdf.set_font("DejaVu", "", 8)
        pdf.cell(WIDTH - 6, 5, f"Позиций: {len(items)} · {_money(order.get('total'))}")

    buffer = BytesIO()
    pdf.output(buffer)
    return buffer.getvalue()


def _money(value: Any) -> str:
    try:
        return f"${float(value or 0):.2f}"
    except (TypeError, ValueError):
        return "$0.00"


def today_label(day: str, count: int) -> str:
    moment = datetime.now().strftime("%Y%m%d-%H%M")
    return f"nakleyki-{day}-{count}шт-{moment}.pdf"
