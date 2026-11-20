"""C13: оценки в отчёт — кто из бригады получает «3⭐» и ниже.

Клиент после доставки ставит оценку 1–5. Оценка лежит в заказе, а заказ знает
упаковщика (`assigned_to`) и курьера (`courier_chat_id`), поэтому можно честно
показать среднюю оценку и долю недовольных по каждому сотруднику.

Здесь только тексты и расчёты — данные приходят строками из базы.
"""
from __future__ import annotations

LOW_RATING = 3          # 3 и ниже считаем недовольством
MIN_RATED = 2           # одного отзыва мало для вывода


def low_share(ratings: list[int]) -> float:
    """Доля низких оценок в процентах."""
    if not ratings:
        return 0.0
    low = sum(1 for value in ratings if int(value) <= LOW_RATING)
    return low / len(ratings) * 100.0


def average(ratings: list[int]) -> float:
    if not ratings:
        return 0.0
    return sum(float(value) for value in ratings) / len(ratings)


def staff_rows(rows: list[dict]) -> list[dict]:
    """Готовит строки отчёта: средняя, число оценок, доля недовольных."""
    result: list[dict] = []
    for row in rows:
        try:
            count = int(row.get("n") or 0)
            total = float(row.get("total") or 0)
        except (TypeError, ValueError):
            continue
        if count <= 0:
            continue
        low = int(row.get("low") or 0)
        result.append(
            {
                "role": str(row.get("role") or ""),
                "name": str(row.get("name") or row.get("staff_id") or "сотрудник"),
                "count": count,
                "avg": round(total / count, 2),
                "low_share": round(low / count * 100.0, 0),
            }
        )
    result.sort(key=lambda item: (item["avg"], -item["count"]))
    return result


def ratings_block(rows: list[dict], min_rated: int = MIN_RATED) -> list[str]:
    """Строки для недельного отчёта. Пусто — если оценивать нечего."""
    prepared = [row for row in staff_rows(rows) if row["count"] >= min_rated]
    if not prepared:
        return []
    lines = ["", "⭐ <b>Оценки бригады</b>"]
    for row in prepared:
        role = "упаковщик" if row["role"] == "packer" else "курьер"
        line = f"• {role} {row['name']}: {row['avg']:.1f} из 5 ({row['count']} оц.)"
        if row["low_share"]:
            line += f", недовольных {row['low_share']:.0f}%"
        lines.append(line)
    worst = prepared[0]
    if worst["avg"] <= LOW_RATING:
        role = "упаковщик" if worst["role"] == "packer" else "курьер"
        lines.append(
            f"⚠️ Хуже всех: {role} {worst['name']} — {worst['avg']:.1f}. Стоит поговорить."
        )
    return lines


def overall_line(ratings: list[int]) -> str:
    """Одна строка про общее качество сервиса."""
    if not ratings:
        return "⭐ Оценок за неделю не было."
    return (
        f"⭐ Средняя оценка: {average(ratings):.1f} из 5 "
        f"({len(ratings)} оц.), недовольных {low_share(ratings):.0f}%"
    )
