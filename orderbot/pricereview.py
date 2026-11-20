"""B6: «Цены на завтра» — новый прайс из закупки и наценки по категории.

Менеджер пишет `/pricenew`: бот берёт действующие закупочные цены (`cost_prices`),
умножает на наценку категории и показывает, что подорожает и что подешевеет —
с процентами и итоговой наценкой. По кнопке «Опубликовать» цены уезжают
в `catalog.xlsx` (предыдущая версия архивируется в `price_versions`),
и сразу пересобирается `data/catalog.json` для мини-аппа.

Отдельные строки правятся командой `/pricenew_set помидоры 1.35`.
Наценки: `/markup` — таблица, `/markup Сыры 30`, `/markup default 25`.

Чистая логика (без Telegram) лежит здесь, чтобы её покрывали тесты.
"""
from __future__ import annotations

import json
import logging
import shutil
from dataclasses import dataclass, replace
from datetime import date, datetime, timedelta
from pathlib import Path

from openpyxl import load_workbook

from .catalog import Catalog
from .formatting import esc, money

log = logging.getLogger(__name__)

PROPOSAL_KEY = "price_review_proposal"
MARKUP_DEFAULT_KEY = "markup_default"
MARKUP_PREFIX = "markup:"
LAST_DONE_KEY = "price_reviewed_at"
LAST_REMIND_KEY = "price_review_reminded"
REMIND_DAYS_KEY = "price_review_days"

DEFAULT_MARKUP = 25.0
DEFAULT_REMIND_DAYS = 14
MAX_MARKUP = 500.0
MIN_PRICE = 0.01
MAX_PRICE = 100000.0
BIG_CHANGE_PERCENT = 50.0   # такие правки показываем с предупреждением


@dataclass(frozen=True)
class Proposal:
    catalog_id: int
    name: str
    category: str
    cost: float
    old_price: float
    new_price: float

    @property
    def diff(self) -> float:
        return round(self.new_price - self.old_price, 4)

    @property
    def percent(self) -> float:
        return (self.diff / self.old_price * 100.0) if self.old_price else 0.0

    @property
    def margin_percent(self) -> float:
        if self.new_price <= 0:
            return 0.0
        return (self.new_price - self.cost) / self.new_price * 100.0

    @property
    def big_change(self) -> bool:
        return abs(self.percent) >= BIG_CHANGE_PERCENT


def clamp_markup(value: float) -> float:
    try:
        number = float(value)
    except (TypeError, ValueError):
        return DEFAULT_MARKUP
    return max(0.0, min(MAX_MARKUP, round(number, 2)))


def target_price(cost: float, markup: float) -> float:
    """Цена продажи от закупки: закупка × (1 + наценка/100), до копеек."""
    return round(max(0.0, float(cost)) * (1 + clamp_markup(markup) / 100.0), 2)


# ---------------------------------------------------------------------------
# настройки: наценки и сроки
# ---------------------------------------------------------------------------


async def default_markup(db) -> float:
    raw = (await db.get_setting(MARKUP_DEFAULT_KEY, "") or "").strip()
    if not raw:
        return DEFAULT_MARKUP
    try:
        return clamp_markup(float(raw.replace(",", ".")))
    except ValueError:
        return DEFAULT_MARKUP


async def markups(db, catalog: Catalog) -> dict[str, float]:
    """Наценка по категориям: своя, если задана, иначе общая."""
    base = await default_markup(db)
    result: dict[str, float] = {}
    for category in sorted({item.category or "" for item in catalog.items}):
        if not category:
            continue
        raw = (await db.get_setting(MARKUP_PREFIX + category, "") or "").strip()
        result[category] = clamp_markup(raw.replace(",", ".")) if raw else base
    return result


async def remind_days(db) -> int:
    raw = (await db.get_setting(REMIND_DAYS_KEY, "") or "").strip()
    if not raw:
        return DEFAULT_REMIND_DAYS
    try:
        return max(0, min(365, int(float(raw.replace(",", ".")))))
    except ValueError:
        return DEFAULT_REMIND_DAYS


# ---------------------------------------------------------------------------
# расчёт предложения
# ---------------------------------------------------------------------------


def build(
    catalog: Catalog,
    costs: dict[int, float],
    markup_by_category: dict[str, float],
    default: float = DEFAULT_MARKUP,
) -> tuple[list[Proposal], int]:
    """Предложение нового прайса. Возвращает (изменения, сколько позиций без закупки)."""
    proposals: list[Proposal] = []
    missing = 0
    for item in catalog.items:
        cost = costs.get(item.id)
        if cost is None or float(cost) <= 0:
            missing += 1
            continue
        markup = markup_by_category.get(item.category or "", default)
        new_price = target_price(float(cost), markup)
        if not (MIN_PRICE <= new_price <= MAX_PRICE):
            continue
        old_price = float(item.price or 0)
        if old_price and abs(new_price - old_price) < 0.005:
            continue
        proposals.append(
            Proposal(
                catalog_id=item.id,
                name=item.name,
                category=item.category or "",
                cost=float(cost),
                old_price=old_price,
                new_price=new_price,
            )
        )
    # Сначала самое заметное изменение по деньгам: с него начинать правку.
    proposals.sort(key=lambda p: -abs(p.diff))
    return proposals, missing


def render(proposals: list[Proposal], missing: int = 0, limit: int = 20) -> str:
    if not proposals:
        lines = ["✅ Новый прайс совпадает с текущим — менять нечего."]
        if missing:
            lines.append(
                f"⚠️ Без закупочной цены: {missing} поз. — их не считаю. "
                "Задать: /cost помидоры 1.10"
            )
        return "\n".join(lines)
    up = sum(1 for p in proposals if p.diff > 0)
    down = len(proposals) - up
    lines = [f"🧮 <b>Цены на завтра</b> — меняется {len(proposals)} поз.", ""]
    for proposal in proposals[:limit]:
        arrow = "▲" if proposal.diff > 0 else "▼"
        warn = " ⚠️" if proposal.big_change else ""
        lines.append(
            f"{arrow} {esc(proposal.name)}: {money(proposal.old_price)} → "
            f"<b>{money(proposal.new_price)}</b> "
            f"({proposal.percent:+.0f}%, наценка {proposal.margin_percent:.0f}%){warn}"
        )
    if len(proposals) > limit:
        lines.append(f"…и ещё {len(proposals) - limit}")
    lines.append("")
    lines.append(f"Подорожают: {up} · подешевеют: {down}")
    if missing:
        lines.append(
            f"⚠️ Без закупочной цены: {missing} поз. — их не трогаю (/cost позиция цена)."
        )
    lines.append("")
    lines.append("✅ Опубликовать — цены сразу увидят клиенты.")
    lines.append("/pricenew_set помидоры 1.35 — поправить одну строку.")
    return "\n".join(lines)


def markups_text(
    catalog: Catalog,
    markup_by_category: dict[str, float],
    default: float,
    costs: dict[int, float] | None = None,
) -> str:
    """Таблица наценок по категориям + фактическая наценка по текущим ценам."""
    costs = costs or {}
    counts: dict[str, int] = {}
    actual: dict[str, list[float]] = {}
    for item in catalog.items:
        category = item.category or "без категории"
        counts[category] = counts.get(category, 0) + 1
        cost = costs.get(item.id)
        price = float(item.price or 0)
        if cost and price > 0:
            actual.setdefault(category, []).append((price - float(cost)) / price * 100.0)
    lines = ["📐 <b>Наценки по категориям</b>", f"Общая: <b>{default:.0f}%</b>", ""]
    for category in sorted(counts):
        value = markup_by_category.get(category, default)
        line = f"• {esc(category)} — {value:.0f}% ({counts[category]} поз.)"
        percents = actual.get(category)
        if percents:
            line += f", сейчас {sum(percents) / len(percents):.0f}%"
        lines.append(line)
    lines.append("")
    lines.append("Изменить: /markup Сыры 30 · /markup default 25")
    return "\n".join(lines)


# ---------------------------------------------------------------------------
# хранение предложения между сообщениями
# ---------------------------------------------------------------------------


def payload(prices: dict[int, float]) -> str:
    return json.dumps(
        [{"id": item_id, "price": price} for item_id, price in prices.items()],
        ensure_ascii=False,
    )


def to_payload(proposals: list[Proposal]) -> str:
    return payload({p.catalog_id: p.new_price for p in proposals})


def from_payload(raw: str | None) -> dict[int, float]:
    if not raw:
        return {}
    try:
        data = json.loads(raw)
    except (TypeError, ValueError):
        return {}
    prices: dict[int, float] = {}
    if not isinstance(data, list):
        return {}
    for entry in data:
        try:
            item_id = int(entry["id"])
            price = float(entry["price"])
        except (KeyError, TypeError, ValueError):
            continue
        if MIN_PRICE <= price <= MAX_PRICE:
            prices[item_id] = round(price, 2)
    return prices


# ---------------------------------------------------------------------------
# запись в прайс-файл
# ---------------------------------------------------------------------------


def backup_catalog(path: Path, versions_dir: Path, tag: str = "") -> Path | None:
    """Копия прайса перед правкой: в data.price_versions и в data/backups."""
    if not Path(path).exists():
        return None
    versions_dir = Path(versions_dir)
    versions_dir.mkdir(parents=True, exist_ok=True)
    stamp = datetime.now().strftime("%Y%m%d-%H%M%S")
    suffix = f"-{tag}" if tag else ""
    target = versions_dir / f"catalog-{stamp}{suffix}.xlsx"
    try:
        shutil.copy2(path, target)
    except OSError:
        log.warning("Не удалось сохранить копию прайса %s", path)
        return None
    return target


def write_prices(path: Path, prices: dict[int, float]) -> tuple[int, int]:
    """Пакетная запись цен в xlsx. Возвращает (записано, не найдено)."""
    if not prices:
        return 0, 0
    try:
        workbook = load_workbook(path)
    except Exception:
        log.exception("Не удалось открыть прайс %s", path)
        return 0, len(prices)
    worksheet = workbook.worksheets[0]
    header = [str(cell.value or "").strip().lower() for cell in worksheet[1]]
    price_col = next(
        (i + 1 for i, title in enumerate(header) if title.startswith("цена")), None
    )
    id_col = next(
        (i + 1 for i, title in enumerate(header) if title in ("id", "код", "артикул")),
        None,
    )
    if price_col is None or id_col is None:
        workbook.close()
        return 0, len(prices)
    updated = 0
    for row in range(2, worksheet.max_row + 1):
        raw_id = worksheet.cell(row=row, column=id_col).value
        try:
            row_id = int(raw_id)
        except (TypeError, ValueError):
            continue
        new_price = prices.get(row_id)
        if new_price is None:
            continue
        cell = worksheet.cell(row=row, column=price_col)
        cell.value = float(new_price)
        cell.number_format = "$#,##0.00"
        updated += 1
    if updated:
        try:
            workbook.save(path)
        except Exception:
            log.exception("Не удалось записать прайс %s", path)
            workbook.close()
            return 0, len(prices)
    workbook.close()
    return updated, len(prices) - updated


def apply_to_catalog(catalog: Catalog, prices: dict[int, float]) -> None:
    """Обновляет цены в памяти, чтобы клиенты сразу видели новые."""
    if not prices:
        return
    items = [
        replace(item, price=prices[item.id]) if item.id in prices else item
        for item in catalog.items
    ]
    catalog.items = items
    catalog.by_id = {item.id: item for item in items}
    catalog._phrases = [
        (catalog.by_id.get(item.id, item), norm, lemma)
        for item, norm, lemma in catalog._phrases
    ]


def mark_reviewed_today() -> str:
    return datetime.now().date().isoformat()


# ---------------------------------------------------------------------------
# напоминание «прайс не обновлялся N дней»
# ---------------------------------------------------------------------------


def needs_reminder(
    last_done: str | None, last_reminded: str | None, today: date
) -> bool:
    """Напоминание не чаще раза в неделю, если прайс не пересматривали."""
    if last_reminded:
        try:
            if date.fromisoformat(str(last_reminded)[:10]) >= today - timedelta(days=6):
                return False
        except ValueError:
            pass
    return True


async def send_reminder(bot, db, settings) -> bool:
    """Напоминает менеджеру, что прайс давно не пересматривали (раз в неделю)."""
    manager = settings.manager_chat_id
    if manager is None:
        return False
    days = await remind_days(db)
    if days <= 0:
        return False
    last_done = (await db.get_setting(LAST_DONE_KEY, "") or "").strip()
    today = datetime.now(settings.timezone).date()
    if last_done:
        try:
            done = date.fromisoformat(last_done[:10])
        except ValueError:
            done = None
        if done is not None and (today - done).days < days:
            return False
    last_reminded = await db.get_setting(LAST_REMIND_KEY, "")
    if not needs_reminder(last_done, last_reminded, today):
        return False
    await bot.send_message(manager, reminder_text(days, last_done or None))
    await db.set_setting(LAST_REMIND_KEY, today.isoformat())
    return True


def reminder_text(days: int, last_done: str | None) -> str:
    if last_done:
        try:
            when = date.fromisoformat(str(last_done)[:10]).strftime("%d.%m")
        except ValueError:
            when = "давно"
        head = f"Прошло больше {days} дней с пересмотра цен (последний — {when})."
    else:
        head = f"Цены ни разу не пересматривались (порог {days} дней)."
    return (
        "🧮 <b>Пора посмотреть цены</b>\n\n"
        f"{head}\n"
        "Соберу новый прайс из закупки и наценки: /pricenew\n"
        "Наценки по категориям: /markup"
    )
