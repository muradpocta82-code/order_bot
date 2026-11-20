"""E4: сколько стоит ИИ — вызовы, оценка расхода, предупреждение о бюджете.

Точных токенов провайдеры в ответе не всегда дают, поэтому расход считается
по числу вызовов и цене за вызов (`LLM_COST_PER_CALL`, по умолчанию $0.002).
Этого достаточно, чтобы видеть порядок сумм и замечать рост.

Бюджет вызовов в сутки уже есть (`LLM_DAILY_BUDGET`). На 80 % бот один раз
в день предупреждает менеджера, а при исчерпании — считает заблокированные вызовы.
"""
from __future__ import annotations

from datetime import date

DEFAULT_COST_PER_CALL = 0.002
WARN_SHARE = 0.8


def estimate(calls: int, cost_per_call: float = DEFAULT_COST_PER_CALL) -> float:
    try:
        price = float(cost_per_call)
    except (TypeError, ValueError):
        price = DEFAULT_COST_PER_CALL
    return round(max(0, int(calls)) * max(0.0, price), 4)


def budget_share(used: int, budget: int) -> float:
    if not budget or budget <= 0:
        return 0.0
    return min(1.0, max(0.0, used / float(budget)))


def budget_warning(used: int, budget: int, cost: float) -> str:
    return (
        f"⚠️ ИИ израсходовал {used} из {budget} вызовов за сутки "
        f"({budget_share(used, budget) * 100:.0f}%, около ${cost:.2f}).\n"
        "Дальше бот перейдёт на локальный разбор — качество может просесть. "
        "Проверьте LLM_DAILY_BUDGET в настройках."
    )


def usage_line(calls: int, cost: float, budget: int = 0) -> str:
    line = f"🤖 ИИ за сутки: {int(calls)} вызовов, ~${float(cost):.2f}"
    if budget:
        line += f" (лимит {budget}, {budget_share(int(calls), budget) * 100:.0f}%)"
    return line


def per_order(cost: float, orders: int) -> float:
    if not orders:
        return 0.0
    return round(float(cost) / int(orders), 4)


def weekly_line(days_rows: list[dict], orders: int) -> str:
    """Строка для недельного отчёта."""
    calls = sum(int(row.get("calls") or 0) for row in days_rows)
    cost = sum(float(row.get("est_cost") or 0) for row in days_rows)
    if not calls:
        return ""
    return (
        f"🤖 ИИ за неделю: {calls} вызовов, ~${cost:.2f}"
        + (f", на заказ ~${per_order(cost, orders):.3f}" if orders else "")
    )


def today() -> str:
    return date.today().isoformat()
