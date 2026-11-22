"""C5: опрос через сутки после доставки — «всё хорошо?» и типы проблем.

Оценку ⭐ клиент ставит сразу, но настоящие проблемы всплывают позже.
Через сутки бот спрашивает мягко:

- «✅ Всё хорошо» — просто спасибо, больше не спрашиваем;
- «⚠️ Есть проблема» — выбор типа (недовес, качество, не привезли, другое)
  и затем описание или фото; заявка уходит менеджеру с кнопками решения.

Здесь тексты и типы — без Telegram.
"""
from __future__ import annotations

PROBLEM_TYPES = (
    ("weight", "⚖️ Недовес"),
    ("quality", "🥬 Качество"),
    ("missing", "📦 Не привезли"),
    ("other", "❓ Другое"),
)

LABELS = dict(PROBLEM_TYPES)


def problem_label(code: str) -> str:
    return LABELS.get(str(code), "другое")


def survey_text(order_id: int) -> str:
    return (
        f"Как прошёл заказ №{order_id}?\n"
        "Если всё в порядке — просто закройте сообщение. "
        "Если что-то не так — скажите, поправим."
    )


def thanks_text() -> str:
    return "Спасибо! Рады, что всё хорошо. Если что — просто напишите."


def problem_prompt(order_id: int) -> str:
    return f"Что именно не так с заказом №{order_id}? Выберите вариант:"


def details_prompt(label: str) -> str:
    return (
        f"Записал: {label}. Опишите коротко, что случилось, — "
        "можно приложить фото одним сообщением. Менеджер свяжется."
    )


def claim_text(label: str, details: str) -> str:
    body = str(details or "").strip()
    if not body:
        return f"Опрос после доставки: {label}"
    return f"{label}: {body}"


def survey_note() -> str:
    return "Ответьте «✅ Всё хорошо» или «⚠️ Есть проблема»."
