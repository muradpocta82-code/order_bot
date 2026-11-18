"""Inline-клавиатуры."""
from __future__ import annotations

import os

from aiogram.types import (
    InlineKeyboardButton,
    InlineKeyboardMarkup,
    KeyboardButton,
    ReplyKeyboardMarkup,
    ReplyKeyboardRemove,
    WebAppInfo,
)

from .catalog import Catalog
from .formatting import money
from .units import format_qty, plural

# Адрес мини-приложения каталога задаётся в .env (CATALOG_WEBAPP_URL).
CATALOG_WEBAPP_URL = os.getenv("CATALOG_WEBAPP_URL", "").strip()


def _short(text: str, limit: int = 52) -> str:
    text = " ".join(str(text).split())
    return text if len(text) <= limit else text[: limit - 1] + "…"


def candidates_keyboard(
    draft_id: str, idx: int, catalog: Catalog, candidate_ids: list[int]
) -> InlineKeyboardMarkup:
    rows: list[list[InlineKeyboardButton]] = []
    for cid in candidate_ids:
        item = catalog.get(cid)
        if item is None:
            continue
        label = f"{_short(item.name, 38)} · {money(item.price)}/{item.unit}"
        rows.append(
            [
                InlineKeyboardButton(
                    text=label, callback_data=f"pick:{draft_id}:{idx}:{item.id}"
                )
            ]
        )
    rows.append(
        [
            InlineKeyboardButton(text="Нет в списке", callback_data=f"none:{draft_id}:{idx}"),
            InlineKeyboardButton(text="Пропустить", callback_data=f"skip:{draft_id}:{idx}"),
        ]
    )
    rows.append(
        [InlineKeyboardButton(text="Отменить", callback_data=f"dcancel:{draft_id}")]
    )
    return InlineKeyboardMarkup(inline_keyboard=rows)


def qty_keyboard(draft_id: str, idx: int, unit: str | None) -> InlineKeyboardMarkup:
    values = [1, 2, 5, 10]
    row: list[InlineKeyboardButton] = []
    rows: list[list[InlineKeyboardButton]] = []
    for value in values:
        row.append(
            InlineKeyboardButton(
                text=f"{value} {plural(unit, value)}",
                callback_data=f"qty:{draft_id}:{idx}:{value}",
            )
        )
        if len(row) == 2:
            rows.append(row)
            row = []
    if row:
        rows.append(row)
    rows.append(
        [
            InlineKeyboardButton(
                text="Ввести число", callback_data=f"qtytext:{draft_id}:{idx}"
            )
        ]
    )
    rows.append(
        [InlineKeyboardButton(text="Отмена", callback_data=f"dcancel:{draft_id}")]
    )
    return InlineKeyboardMarkup(inline_keyboard=rows)


def price_update_keyboard(token: str) -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(
        inline_keyboard=[
            [
                InlineKeyboardButton(
                    text="✅ Обновить", callback_data=f"price:apply:{token}"
                ),
                InlineKeyboardButton(
                    text="❌ Отмена", callback_data=f"price:cancel:{token}"
                ),
            ]
        ]
    )


def manager_status_keyboard(
    order_id: int, status: str | None = None
) -> InlineKeyboardMarkup:
    """Кнопки упаковщика. Доставку подтверждает только доставщик."""
    rows: list[list[InlineKeyboardButton]] = []
    if status not in ("done", "cancelled", "failed"):
        if status in (None, "new", "accepted"):
            rows.append(
                [
                    InlineKeyboardButton(
                        text="✅ Принять", callback_data=f"ost:{order_id}:accepted"
                    ),
                    InlineKeyboardButton(
                        text="📦 Собран — доставщику", callback_data=f"ost:{order_id}:ready"
                    ),
                ]
            )
            rows.append(
                [
                    InlineKeyboardButton(
                        text="⚖️ Довес", callback_data=f"wgh:{order_id}"
                    )
                ]
            )
            rows.append(
                [
                    InlineKeyboardButton(
                        text="📝 Отдать в долг", callback_data=f"credit:{order_id}"
                    )
                ]
            )
        elif status in ("ready", "delivering"):
            rows.append(
                [
                    InlineKeyboardButton(
                        text="📨 Отправить доставщику ещё раз",
                        callback_data=f"ost:{order_id}:ready",
                    )
                ]
            )
        rows.append(
            [
                InlineKeyboardButton(
                    text="❌ Отменить заказ", callback_data=f"ost:{order_id}:cancelled"
                )
            ]
        )
    return InlineKeyboardMarkup(inline_keyboard=rows)


def courier_status_keyboard(
    order_id: int, status: str, payment_status: str
) -> InlineKeyboardMarkup:
    """Кнопки доставщика: в путь, оплата, доставлено, проблема."""
    if status in ("done", "cancelled", "failed"):
        return InlineKeyboardMarkup(
            inline_keyboard=[
                [
                    InlineKeyboardButton(
                        text="🏠 Доставки", callback_data="cd:list:0"
                    )
                ]
            ]
        )
    rows: list[list[InlineKeyboardButton]] = []
    if status == "ready":
        rows.append(
            [
                InlineKeyboardButton(
                    text="🚚 Выехал к клиенту", callback_data=f"cour:start:{order_id}"
                )
            ]
        )
    if payment_status != "paid":
        pay_row = [
            InlineKeyboardButton(
                text="💵 Наличные получены",
                callback_data=f"cour:pay:{order_id}:cash",
            )
        ]
        if status == "delivering":
            pay_row.append(
                InlineKeyboardButton(
                    text="🛰 Прибыл — USDT",
                    callback_data=f"cour:arrive:{order_id}",
                )
            )
            pay_row.append(
                InlineKeyboardButton(
                    text="🏦 Перевод по чеку",
                    callback_data=f"cour:transfer:{order_id}",
                )
            )
        rows.append(pay_row)
    rows.append(
        [
            InlineKeyboardButton(
                text="✅ Доставлено", callback_data=f"cour:done:{order_id}"
            )
        ]
    )
    rows.append(
        [
            InlineKeyboardButton(
                text="⚠️ Не доставлено", callback_data=f"cour:fail:{order_id}"
            )
        ]
    )
    return InlineKeyboardMarkup(inline_keyboard=rows)


def courier_back_keyboard() -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(
        inline_keyboard=[
            [InlineKeyboardButton(text="🏠 Доставки", callback_data="cd:list:0")]
        ]
    )


def courier_panel_keyboard(
    orders: list[dict], page: int = 0, pages: int = 1
) -> InlineKeyboardMarkup:
    rows: list[list[InlineKeyboardButton]] = []
    row: list[InlineKeyboardButton] = []
    for order in orders:
        row.append(
            InlineKeyboardButton(
                text=f"№{order['id']}",
                callback_data=f"cd:view:{order['id']}:{page}",
            )
        )
        if len(row) == 5:
            rows.append(row)
            row = []
    if row:
        rows.append(row)
    nav: list[InlineKeyboardButton] = []
    if page > 0:
        nav.append(
            InlineKeyboardButton(text="⬅️ Назад", callback_data=f"cd:list:{page - 1}")
        )
    if page + 1 < pages:
        nav.append(
            InlineKeyboardButton(text="Вперёд ➡️", callback_data=f"cd:list:{page + 1}")
        )
    if nav:
        rows.append(nav)
    rows.append(
        [InlineKeyboardButton(text="🔄 Обновить", callback_data=f"cd:list:{page}")]
    )
    return InlineKeyboardMarkup(inline_keyboard=rows)


def manager_back_keyboard() -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(
        inline_keyboard=[
            [
                InlineKeyboardButton(
                    text="📥 Входящие", callback_data="mp:inbox:0"
                ),
                InlineKeyboardButton(
                    text="🏠 Панель", callback_data="mp:panel"
                ),
            ]
        ]
    )


def broadcast_confirm_keyboard() -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(
        inline_keyboard=[
            [
                InlineKeyboardButton(
                    text="📤 Отправить", callback_data="bc:send"
                ),
                InlineKeyboardButton(
                    text="❌ Отмена", callback_data="bc:cancel"
                ),
            ]
        ]
    )


def rating_keyboard(order_id: int) -> InlineKeyboardMarkup:
    """Оценка заказа после доставки."""
    return InlineKeyboardMarkup(
        inline_keyboard=[
            [
                InlineKeyboardButton(
                    text=f"{n}⭐", callback_data=f"rate:{order_id}:{n}"
                )
                for n in (1, 2, 3, 4, 5)
            ],
            [
                InlineKeyboardButton(
                    text="⚠️ Есть проблема", callback_data=f"prob:{order_id}"
                )
            ],
        ]
    )


def contact_keyboard() -> ReplyKeyboardMarkup:
    return ReplyKeyboardMarkup(
        keyboard=[[KeyboardButton(text="📱 Отправить телефон", request_contact=True)]],
        resize_keyboard=True,
        one_time_keyboard=True,
    )


def location_keyboard() -> ReplyKeyboardMarkup:
    return ReplyKeyboardMarkup(
        keyboard=[[KeyboardButton(text="📍 Отправить адрес", request_location=True)]],
        resize_keyboard=True,
        one_time_keyboard=True,
    )


def remove_keyboard() -> ReplyKeyboardRemove:
    return ReplyKeyboardRemove()


def weigh_confirm_keyboard(order_id: int) -> InlineKeyboardMarkup:
    """Клиент подтверждает изменившийся итог заказа."""
    return InlineKeyboardMarkup(
        inline_keyboard=[
            [
                InlineKeyboardButton(
                    text="✅ Согласен", callback_data=f"wacc:{order_id}"
                ),
                InlineKeyboardButton(
                    text="⚠️ Не согласен", callback_data=f"wdis:{order_id}"
                ),
            ]
        ]
    )


def client_order_keyboard(order_id: int) -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(
        inline_keyboard=[
            [
                InlineKeyboardButton(
                    text="🔁 Повторить", callback_data=f"repeat:{order_id}"
                ),
                InlineKeyboardButton(
                    text="❌ Отменить", callback_data=f"ccancel:{order_id}"
                ),
            ]
        ]
    )


def edit_confirm_keyboard() -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(
        inline_keyboard=[
            [
                InlineKeyboardButton(text="✅ Да", callback_data="editok:yes"),
                InlineKeyboardButton(text="❌ Нет", callback_data="editok:no"),
            ]
        ]
    )


def unit_keyboard(
    draft_id: str, idx: int, catalog_unit: str, pack: str | None, requested: float, requested_unit: str
) -> InlineKeyboardMarkup:
    """Кнопки выбора фасовки, когда повар назвал единицу не как в прайсе."""
    from .units import pack_size, plural

    rows: list[list[InlineKeyboardButton]] = []
    size = pack_size(pack, catalog_unit)
    for count in (1, 2):
        label = f"{count} {plural(catalog_unit, count)}"
        if size:
            label += f" ({format_qty(size * count)} {catalog_unit})"
        rows.append(
            [
                InlineKeyboardButton(
                    text=label, callback_data=f"unit:{draft_id}:{idx}:pack{count}"
                )
            ]
        )
    rows.append(
        [
            InlineKeyboardButton(
                text=f"Ровно {format_qty(requested)} {plural(requested_unit, requested)}",
                callback_data=f"unit:{draft_id}:{idx}:exact",
            )
        ]
    )
    rows.append(
        [InlineKeyboardButton(text="Отменить", callback_data=f"dcancel:{draft_id}")]
    )
    return InlineKeyboardMarkup(inline_keyboard=rows)


def slots_keyboard(draft_id: str, labels: list[str]) -> InlineKeyboardMarkup:
    """Кнопки интервалов доставки."""
    rows: list[list[InlineKeyboardButton]] = []
    for idx, label in enumerate(labels):
        rows.append(
            [
                InlineKeyboardButton(
                    text=f"🕐 {label}", callback_data=f"slot:{draft_id}:{idx}"
                )
            ]
        )
    rows.append(
        [InlineKeyboardButton(text="Отменить", callback_data=f"dcancel:{draft_id}")]
    )
    return InlineKeyboardMarkup(inline_keyboard=rows)


def alt_keyboard(
    draft_id: str, idx: int, suggestions: list[tuple[int, str]]
) -> InlineKeyboardMarkup:
    rows: list[list[InlineKeyboardButton]] = []
    for catalog_id, name in suggestions[:4]:
        rows.append(
            [
                InlineKeyboardButton(
                    text=_short(name, 45), callback_data=f"alt:{draft_id}:{idx}:{catalog_id}"
                )
            ]
        )
    rows.append(
        [
            InlineKeyboardButton(
                text="Убрать из заказа", callback_data=f"skip:{draft_id}:{idx}"
            )
        ]
    )
    return InlineKeyboardMarkup(inline_keyboard=rows)


def date_shift_keyboard(order_id: int) -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(
        inline_keyboard=[
            [
                InlineKeyboardButton(
                    text="✅ Да", callback_data=f"shift:yes:{order_id}"
                ),
                InlineKeyboardButton(
                    text="📅 Выбрать дату", callback_data=f"shift:pick:{order_id}"
                ),
            ]
        ]
    )


def user_access_keyboard(user_id: int) -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(
        inline_keyboard=[
            [
                InlineKeyboardButton(
                    text="✅ Допустить", callback_data=f"acc:ok:{user_id}"
                ),
                InlineKeyboardButton(
                    text="❌ Отклонить", callback_data=f"acc:no:{user_id}"
                ),
            ]
        ]
    )


def reminder_keyboard(user_id: int) -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(
        inline_keyboard=[
            [
                InlineKeyboardButton(
                    text="✅ Да, как обычно", callback_data=f"rday:yes:{user_id}"
                )
            ],
            [
                InlineKeyboardButton(
                    text="Нет, сам напишу", callback_data=f"rday:no:{user_id}"
                ),
                InlineKeyboardButton(
                    text="Не напоминать", callback_data=f"rday:off:{user_id}"
                ),
            ],
        ]
    )


def price_undo_keyboard(version_id: int) -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(
        inline_keyboard=[
            [
                InlineKeyboardButton(
                    text="↩️ Вернуть этот прайс", callback_data=f"pundo:{version_id}"
                ),
                InlineKeyboardButton(text="Отмена", callback_data="pundo:no"),
            ]
        ]
    )


def retry_keyboard() -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(
        inline_keyboard=[
            [
                InlineKeyboardButton(
                    text="🔁 Попробовать ещё раз", callback_data="retry:order"
                )
            ]
        ]
    )


def client_back_keyboard() -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(
        inline_keyboard=[
            [InlineKeyboardButton(text="В меню", callback_data="cl:menu")]
        ]
    )


def manager_panel_keyboard() -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(
        inline_keyboard=[
            [
                InlineKeyboardButton(
                    text="📥 Входящие заказы", callback_data="mp:inbox:0"
                )
            ],
            [InlineKeyboardButton(text="📜 История", callback_data="mp:history:0")],
        ]
    )


def client_menu_keyboard(
    usually: list[dict] | None = None, catalog: Catalog | None = None
) -> InlineKeyboardMarkup:
    rows: list[list[InlineKeyboardButton]] = []
    if usually and catalog is not None:
        for entry in usually:
            item = catalog.get(entry.get("catalog_id"))
            if item is None:
                continue
            qty = entry.get("qty") or 1
            unit = entry.get("unit") or item.unit
            label = f"{_short(item.name, 24)} {format_qty(qty)} {plural(unit, qty)}"
            rows.append(
                [
                    InlineKeyboardButton(
                        text=label, callback_data=f"usual:{item.id}"
                    )
                ]
            )
    rows.append(
        [InlineKeyboardButton(text="Список товаров", callback_data="cl:price")]
    )
    if CATALOG_WEBAPP_URL:
        rows.append(
            [
                InlineKeyboardButton(
                    text="📖 Каталог с ценами", web_app=WebAppInfo(url=CATALOG_WEBAPP_URL)
                )
            ]
        )
    return InlineKeyboardMarkup(inline_keyboard=rows)


def orders_list_keyboard(
    orders: list[dict], kind: str, page: int, pages: int
) -> InlineKeyboardMarkup:
    rows: list[list[InlineKeyboardButton]] = []
    row: list[InlineKeyboardButton] = []
    for order in orders:
        row.append(
            InlineKeyboardButton(
                text=f"№{order['id']}",
                callback_data=f"mp:view:{order['id']}:{kind}:{page}",
            )
        )
        if len(row) == 5:
            rows.append(row)
            row = []
    if row:
        rows.append(row)

    nav: list[InlineKeyboardButton] = []
    if page > 0:
        nav.append(
            InlineKeyboardButton(text="⬅️ Назад", callback_data=f"mp:{kind}:{page - 1}")
        )
    if page + 1 < pages:
        nav.append(
            InlineKeyboardButton(text="Вперёд ➡️", callback_data=f"mp:{kind}:{page + 1}")
        )
    if nav:
        rows.append(nav)
    rows.append(
        [InlineKeyboardButton(text="🔄 Обновить", callback_data=f"mp:{kind}:{page}")]
    )
    rows.append(
        [InlineKeyboardButton(text="🏠 Панель менеджера", callback_data="mp:panel")]
    )
    return InlineKeyboardMarkup(inline_keyboard=rows)


def order_detail_keyboard(
    order_id: int, kind: str, page: int, status: str | None = None
) -> InlineKeyboardMarkup:
    rows = [
        *manager_status_keyboard(order_id, status).inline_keyboard,
        [
            InlineKeyboardButton(
                text="⬅️ Назад к списку", callback_data=f"mp:{kind}:{page}"
            )
        ],
    ]
    return InlineKeyboardMarkup(inline_keyboard=rows)


def morning_price_keyboard(user_id: int) -> InlineKeyboardMarkup:
    """Утреннее сообщение: заказать «как обычно», каталог, отказ от рассылки."""
    rows: list[list[InlineKeyboardButton]] = [
        [InlineKeyboardButton(text="📦 Как обычно", callback_data=f"rday:yes:{user_id}")]
    ]
    if CATALOG_WEBAPP_URL:
        rows.append(
            [
                InlineKeyboardButton(
                    text="📖 Каталог с ценами", web_app=WebAppInfo(url=CATALOG_WEBAPP_URL)
                )
            ]
        )
    rows.append(
        [InlineKeyboardButton(text="🔕 Не присылать", callback_data=f"priceoff:{user_id}")]
    )
    return InlineKeyboardMarkup(inline_keyboard=rows)


FAIL_REASONS = (
    ("place", "🚪 Не на месте"),
    ("refuse", "🙅 Клиент отказался"),
    ("money", "💸 Нет оплаты"),
    ("address", "🗺 Не нашли адрес"),
)


def fail_reason_keyboard(order_id: int) -> InlineKeyboardMarkup:
    """Причина недоставки: одна кнопка вместо разговора по телефону."""
    rows: list[list[InlineKeyboardButton]] = []
    row: list[InlineKeyboardButton] = []
    for code, label in FAIL_REASONS:
        row.append(
            InlineKeyboardButton(text=label, callback_data=f"failr:{order_id}:{code}")
        )
        if len(row) == 2:
            rows.append(row)
            row = []
    if row:
        rows.append(row)
    rows.append(
        [InlineKeyboardButton(text="↩️ Назад", callback_data=f"cour:card:{order_id}")]
    )
    return InlineKeyboardMarkup(inline_keyboard=rows)


def reason_label(code: str) -> str:
    for key, label in FAIL_REASONS:
        if key == code:
            return label
    return "без причины"


RATING_REASONS = (
    ("late", "⏰ Опоздали"),
    ("weight", "⚖️ Недовес"),
    ("quality", "🥬 Качество"),
    ("polite", "🙂 Вежливость"),
    ("other", "❓ Другое"),
)


def rating_reason_keyboard(order_id: int) -> InlineKeyboardMarkup:
    """Почему низкая оценка: кнопки вместо разговора."""
    rows: list[list[InlineKeyboardButton]] = []
    row: list[InlineKeyboardButton] = []
    for code, label in RATING_REASONS:
        row.append(
            InlineKeyboardButton(text=label, callback_data=f"rrate:{order_id}:{code}")
        )
        if len(row) == 2:
            rows.append(row)
            row = []
    if row:
        rows.append(row)
    return InlineKeyboardMarkup(inline_keyboard=rows)


def rating_reason_label(code: str) -> str:
    for key, label in RATING_REASONS:
        if key == code:
            return label
    return "без причины"


CLIENT_REPLY_BUTTONS = ("📦 Мои заказы", "🔁 Как обычно", "☎️ Менеджер", "📖 Прайс")


def client_reply_keyboard() -> ReplyKeyboardMarkup:
    """Постоянные кнопки под полем ввода: на кухне печатать неудобно."""
    rows = [
        [KeyboardButton(text="📦 Мои заказы"), KeyboardButton(text="🔁 Как обычно")],
        [KeyboardButton(text="☎️ Менеджер"), KeyboardButton(text="📖 Прайс")],
    ]
    return ReplyKeyboardMarkup(keyboard=rows, resize_keyboard=True, is_persistent=True)


def claim_actions_keyboard(claim_id: int) -> InlineKeyboardMarkup:
    """Решение по претензии: заменить, кредит-нота или отклонить."""
    rows = [
        [
            InlineKeyboardButton(
                text="🔁 Заменить в след. заказе", callback_data=f"clm:{claim_id}:replace"
            )
        ],
        [
            InlineKeyboardButton(text="🧾 Кредит-нота", callback_data=f"clm:{claim_id}:credit"),
            InlineKeyboardButton(text="❌ Отклонить", callback_data=f"clm:{claim_id}:reject"),
        ],
    ]
    return InlineKeyboardMarkup(inline_keyboard=rows)


def pack_checklist_keyboard(
    order_id: int, items: list[dict], checked: set[str]
) -> InlineKeyboardMarkup:
    """Чек-лист сборки: тап по строке отмечает позицию собранной."""
    rows: list[list[InlineKeyboardButton]] = []
    for index, item in enumerate(items):
        if item.get("status") == "skipped":
            continue
        qty = format_qty(item.get("qty"))
        unit = plural(item.get("unit"), item.get("qty"))
        name = str(item.get("name") or item.get("raw") or "позиция")[:28]
        mark = "✅" if str(index) in checked else "⬜"
        rows.append(
            [
                InlineKeyboardButton(
                    text=f"{mark} {name} {qty} {unit}"[:60],
                    callback_data=f"chk:{order_id}:{index}",
                )
            ]
        )
    rows.append(
        [
            InlineKeyboardButton(
                text="🔄 Обновить", callback_data=f"chk:{order_id}:refresh"
            ),
            InlineKeyboardButton(text="🏠 Панель", callback_data="mp:panel"),
        ]
    )
    return InlineKeyboardMarkup(inline_keyboard=rows)
