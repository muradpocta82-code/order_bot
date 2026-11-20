"""C8: правила замены позиций — «заменить на …», «не заменять», «спросить».

Когда позиции нет в наличии, бот предлагает похожие. Менеджер может задать
правило для позиции:

- `спросить` (по умолчанию) — предлагать похожие из той же категории;
- `заменить на X` — предлагать только конкретную позицию;
- `не заменять` — не предлагать ничего, просто «нет в наличии».

Правила хранятся в `substitute_rules(catalog_id, mode, substitute_id)`.
Здесь чистая логика выбора замены и разбора команды /sub.
"""
from __future__ import annotations

from dataclasses import dataclass

from .catalog import Catalog, CatalogItem

MODE_ASK = "ask"
MODE_NO = "no"
MODE_ONLY = "only"

NO_WORDS = ("нет", "не", "не надо", "не заменять", "no")
ASK_WORDS = ("спросить", "спрашивать", "ask", "авто")


@dataclass(frozen=True)
class SubRule:
    mode: str = MODE_ASK
    substitute_id: int | None = None

    @property
    def text(self) -> str:
        if self.mode == MODE_NO:
            return "не заменять"
        if self.mode == MODE_ONLY and self.substitute_id:
            return f"заменять на id {self.substitute_id}"
        return "спросить"


def parse_rule(value: str) -> SubRule | None:
    """Разбирает хвост команды: «нет», «спросить», «1250»."""
    raw = " ".join(str(value or "").strip().lower().split())
    if not raw:
        return None
    if raw in NO_WORDS:
        return SubRule(MODE_NO)
    if raw in ASK_WORDS:
        return SubRule(MODE_ASK)
    if raw.isdigit() and int(raw) > 0:
        return SubRule(MODE_ONLY, int(raw))
    return None


def pick(catalog: Catalog, item: CatalogItem, rule: SubRule | None) -> list[CatalogItem]:
    """Какие замены показать клиенту с учётом правила."""
    if rule is None:
        return catalog.substitutes(item)
    if rule.mode == MODE_NO:
        return []
    if rule.mode == MODE_ONLY and rule.substitute_id:
        chosen = catalog.get(rule.substitute_id)
        if chosen is None or chosen.id == item.id:
            return []
        return [chosen]
    return catalog.substitutes(item)


def rules_text(catalog: Catalog, rules: dict[int, dict]) -> str:
    """Список заданных правил для менеджера."""
    if not rules:
        return (
            "Правил замены нет: при отсутствии бот предлагает похожие позиции "
            "из той же категории.\n"
            "Задать: <code>/sub гуада чеддер</code>, <code>/sub гуада нет</code>, "
            "<code>/sub гуада спросить</code>"
        )
    lines = ["🔁 <b>Правила замены</b>", ""]
    for catalog_id, row in sorted(rules.items()):
        item = catalog.get(int(catalog_id))
        name = item.name if item is not None else f"позиция {catalog_id}"
        mode = str(row.get("mode") or MODE_ASK)
        if mode == MODE_NO:
            tail = "не заменять"
        elif mode == MODE_ONLY:
            sub = catalog.get(row.get("substitute_id"))
            tail = f"заменять на «{sub.name}»" if sub is not None else "замена не найдена"
        else:
            tail = "спрашивать"
        lines.append(f"• {name} — {tail}")
    lines.append("")
    lines.append("Изменить: /sub позиция замена · снять: /sub позиция спросить")
    return "\n".join(lines)
