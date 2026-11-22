"""PDF-накладная по заказу (fpdf2 + DejaVu для кириллицы)."""
from __future__ import annotations

from pathlib import Path
from typing import Any

FONT = "/usr/share/fonts/truetype/dejavu/DejaVuSans.ttf"
FONT_BOLD = "/usr/share/fonts/truetype/dejavu/DejaVuSans-Bold.ttf"


def build_invoice(
    order: dict[str, Any],
    target: Path,
    credits: list[dict[str, Any]] | None = None,
) -> Path:
    from fpdf import FPDF

    pdf = FPDF(orientation="P", unit="mm", format="A4")
    pdf.add_page()
    pdf.add_font("DejaVu", "", FONT)
    pdf.add_font("DejaVu", "B", FONT_BOLD)
    pdf.set_font("DejaVu", "B", 16)
    pdf.cell(0, 10, f"Накладная №{order.get('id')}", new_x="LMARGIN", new_y="NEXT")
    pdf.set_font("DejaVu", "", 10)
    pdf.cell(0, 6, f"Дата: {order.get('created_at', '')}", new_x="LMARGIN", new_y="NEXT")
    pdf.cell(0, 6, f"Клиент: {order.get('chef_name') or ''}", new_x="LMARGIN", new_y="NEXT")
    spot_name = order.get("location_name") or order.get("venue")
    if spot_name:
        pdf.cell(0, 6, f"Заведение: {spot_name}", new_x="LMARGIN", new_y="NEXT")
    if order.get("delivery_address"):
        pdf.cell(0, 6, f"Адрес: {order.get('delivery_address')}", new_x="LMARGIN", new_y="NEXT")
    if order.get("delivery_phone"):
        pdf.cell(0, 6, f"Телефон: {order.get('delivery_phone')}", new_x="LMARGIN", new_y="NEXT")
    pdf.ln(4)

    pdf.set_font("DejaVu", "B", 10)
    pdf.cell(10, 7, "№", border=1)
    pdf.cell(85, 7, "Позиция", border=1)
    pdf.cell(25, 7, "Кол-во", border=1)
    pdf.cell(25, 7, "Цена", border=1)
    pdf.cell(30, 7, "Сумма", border=1, new_x="LMARGIN", new_y="NEXT")
    pdf.set_font("DejaVu", "", 9)
    for index, item in enumerate(order.get("items") or [], start=1):
        if item.get("status") == "skipped":
            continue
        name = str(item.get("name") or item.get("raw") or "")[:60]
        qty = item.get("qty") or 0
        unit = item.get("unit") or ""
        price = item.get("price") or 0
        total = item.get("total") or 0
        pdf.cell(10, 6, str(index), border=1)
        pdf.cell(85, 6, name, border=1)
        pdf.cell(25, 6, f"{qty} {unit}", border=1)
        pdf.cell(25, 6, f"${price:.2f}", border=1)
        pdf.cell(30, 6, f"${total:.2f}", border=1, new_x="LMARGIN", new_y="NEXT")
    pdf.set_font("DejaVu", "B", 11)
    pdf.cell(0, 8, f"Итого: ${float(order.get('total') or 0):.2f}", new_x="LMARGIN", new_y="NEXT")
    pdf.set_font("DejaVu", "", 9)
    method = {"cash": "наличные", "usdt": "USDT (TON)", "transfer": "перевод по чеку"}.get(
        str(order.get("payment_method")), str(order.get("payment_method") or "")
    )
    pdf.cell(0, 6, f"Оплата: {method}", new_x="LMARGIN", new_y="NEXT")

    # B4: отметка об оплате и история зачётов — документ говорит сам за себя.
    total = round(float(order.get("total") or 0), 2)
    paid = round(float(order.get("paid_amount") or 0), 2)
    credited = round(sum(float(row.get("amount") or 0) for row in credits or []), 2)
    left = round(max(0.0, total - paid - credited), 2)
    pdf.cell(
        0, 6, f"Оплачено: ${paid:.2f} · Зачтено: ${credited:.2f} · Остаток: ${left:.2f}",
        new_x="LMARGIN", new_y="NEXT",
    )
    if credits:
        pdf.ln(2)
        pdf.set_font("DejaVu", "B", 9)
        pdf.cell(0, 6, "История зачётов", new_x="LMARGIN", new_y="NEXT")
        pdf.set_font("DejaVu", "", 9)
        for row in credits:
            day = str(row.get("created_at") or "")[:16]
            reason = str(row.get("reason") or "")[:60]
            pdf.cell(
                0, 5,
                f"Кредит-нота №{row.get('credit_note_id')} · {day} · "
                f"${float(row.get('amount') or 0):.2f} · {reason}",
                new_x="LMARGIN", new_y="NEXT",
            )
    if left <= 0.005 and total > 0:
        pdf.ln(3)
        pdf.set_font("DejaVu", "B", 12)
        pdf.cell(0, 8, "ОПЛАЧЕНО", new_x="LMARGIN", new_y="NEXT")
    else:
        pdf.ln(3)
        pdf.set_font("DejaVu", "B", 10)
        pdf.cell(0, 8, f"К ОПЛАТЕ: ${left:.2f}", new_x="LMARGIN", new_y="NEXT")
    pdf.output(str(target))
    return target
