"""Акт сверки с клиентом за период (B4).

PDF по образцу накладной: таблица «дата · заказ · сумма · оплачено · долг»,
итоги и отдельный список претензий с зачётом (кредит-нот), если они были.

Деньги считаются в валюте прайса (USD). Акт — документ для пересылки клиенту,
поэтому в нём нет внутренних пометок менеджера.
"""
from __future__ import annotations

from datetime import datetime
from pathlib import Path
from typing import Any

FONT = "/usr/share/fonts/truetype/dejavu/DejaVuSans.ttf"
FONT_BOLD = "/usr/share/fonts/truetype/dejavu/DejaVuSans-Bold.ttf"


def totals(
    orders: list[dict[str, Any]], credit_notes: list[dict[str, Any]] | None = None
) -> dict[str, float]:
    """Итоги акта: сколько выставлено, сколько оплачено, сколько должны."""
    billed = 0.0
    paid = 0.0
    for order in orders:
        if str(order.get("status")) == "cancelled":
            continue
        total = float(order.get("total") or 0)
        billed += total
        part = float(order.get("paid_amount") or 0)
        if str(order.get("payment_status")) == "paid" and part <= 0:
            part = total
        paid += min(total, part)
    notes = 0.0
    for note in credit_notes or []:
        try:
            notes += float(note.get("amount") or 0)
        except (TypeError, ValueError):
            continue
    return {
        "billed": billed,
        "paid": paid,
        "credit": notes,
        "balance": billed - paid - notes,
    }


def _short_date(value: Any) -> str:
    text = str(value or "")
    try:
        return datetime.fromisoformat(text[:19]).strftime("%d.%m.%Y")
    except ValueError:
        return text[:10]


def build_statement(
    client: dict[str, Any],
    orders: list[dict[str, Any]],
    target: Path,
    start: str,
    end: str,
    credit_notes: list[dict[str, Any]] | None = None,
) -> Path:
    """Собирает PDF-акт сверки и возвращает путь к файлу."""
    from fpdf import FPDF

    numbers = totals(orders, credit_notes)
    pdf = FPDF(orientation="P", unit="mm", format="A4")
    pdf.add_page()
    pdf.add_font("DejaVu", "", FONT)
    pdf.add_font("DejaVu", "B", FONT_BOLD)

    pdf.set_font("DejaVu", "B", 15)
    pdf.cell(0, 9, "Акт сверки расчётов", new_x="LMARGIN", new_y="NEXT")
    pdf.set_font("DejaVu", "", 10)
    pdf.cell(0, 6, f"Период: {start} — {end}", new_x="LMARGIN", new_y="NEXT")
    pdf.cell(
        0, 6, f"Клиент: {client.get('venue') or client.get('name') or ''}",
        new_x="LMARGIN", new_y="NEXT",
    )
    if client.get("address"):
        pdf.cell(0, 6, f"Адрес: {client.get('address')}", new_x="LMARGIN", new_y="NEXT")
    if client.get("phone"):
        pdf.cell(0, 6, f"Телефон: {client.get('phone')}", new_x="LMARGIN", new_y="NEXT")
    pdf.ln(3)

    pdf.set_font("DejaVu", "B", 9)
    pdf.cell(22, 7, "Дата", border=1)
    pdf.cell(18, 7, "Заказ", border=1)
    pdf.cell(30, 7, "Статус", border=1)
    pdf.cell(30, 7, "Сумма", border=1)
    pdf.cell(30, 7, "Оплачено", border=1)
    pdf.cell(35, 7, "Долг", border=1, new_x="LMARGIN", new_y="NEXT")
    pdf.set_font("DejaVu", "", 9)

    status_labels = {
        "new": "новый",
        "accepted": "в работе",
        "ready": "собран",
        "delivering": "в доставке",
        "done": "доставлен",
        "failed": "не доставлен",
        "cancelled": "отменён",
    }
    for order in orders:
        total = float(order.get("total") or 0)
        paid = (
            float(order.get("paid_amount") or total)
            if str(order.get("payment_status")) == "paid"
            else 0.0
        )
        cancelled = str(order.get("status")) == "cancelled"
        debt = 0.0 if cancelled else total - paid
        pdf.cell(22, 6, _short_date(order.get("created_at")), border=1)
        pdf.cell(18, 6, f"№{order.get('id')}", border=1)
        pdf.cell(
            30, 6, status_labels.get(str(order.get("status")), str(order.get("status"))),
            border=1,
        )
        pdf.cell(30, 6, f"${total:.2f}", border=1)
        pdf.cell(30, 6, f"${paid:.2f}", border=1)
        pdf.cell(35, 6, f"${debt:.2f}", border=1, new_x="LMARGIN", new_y="NEXT")

    if not orders:
        pdf.cell(0, 6, "Заказов за период нет.", new_x="LMARGIN", new_y="NEXT")

    pdf.set_font("DejaVu", "B", 10)
    pdf.cell(0, 7, f"Выставлено: ${numbers['billed']:.2f}", new_x="LMARGIN", new_y="NEXT")
    pdf.cell(0, 7, f"Оплачено: ${numbers['paid']:.2f}", new_x="LMARGIN", new_y="NEXT")
    if numbers["credit"]:
        pdf.cell(
            0, 7, f"Зачтено по претензиям: ${numbers['credit']:.2f}",
            new_x="LMARGIN", new_y="NEXT",
        )
    pdf.cell(
        0, 8, f"Сальдо на конец периода: ${numbers['balance']:.2f}",
        new_x="LMARGIN", new_y="NEXT",
    )
    if numbers["balance"] <= 0.005:
        pdf.set_font("DejaVu", "B", 11)
        pdf.cell(0, 7, "Расчёты закрыты", new_x="LMARGIN", new_y="NEXT")
    else:
        pdf.set_font("DejaVu", "B", 11)
        pdf.cell(
            0, 7, f"К оплате: ${numbers['balance']:.2f}", new_x="LMARGIN", new_y="NEXT"
        )
    pdf.set_font("DejaVu", "", 10)

    if credit_notes:
        pdf.ln(3)
        pdf.set_font("DejaVu", "B", 10)
        pdf.cell(0, 6, "Претензии с зачётом (кредит-ноты)", new_x="LMARGIN", new_y="NEXT")
        pdf.set_font("DejaVu", "", 9)
        for note in credit_notes:
            order_ref = note.get("order_id")
            line = (
                f"№{order_ref if order_ref else 'без заказа'} · "
                f"{_short_date(note.get('created_at'))} · "
                f"${float(note.get('amount') or 0):.2f} · "
                f"{note.get('reason') or note.get('text') or ''}"
            )
            pdf.multi_cell(0, 5, line[:180], new_x="LMARGIN", new_y="NEXT")

    target.parent.mkdir(parents=True, exist_ok=True)
    pdf.output(str(target))
    return target
