#!/usr/bin/env python3
"""Оценка точности разбора заказов: локальный парсер и DeepSeek.

Запуск:
    venv/bin/python scripts/eval_parse.py
    venv/bin/python scripts/eval_parse.py --local-only
    venv/bin/python scripts/eval_parse.py --deepseek-only --limit 20
"""
from __future__ import annotations

import argparse
import asyncio
import json
import sys
from dataclasses import dataclass, field
from datetime import datetime
from pathlib import Path
from typing import Any
from zoneinfo import ZoneInfo

BASE_DIR = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(BASE_DIR))

from orderbot.catalog import Catalog  # noqa: E402
from orderbot.config import Settings, load_settings  # noqa: E402
from orderbot.extras import correct_voice_text  # noqa: E402
from orderbot.drafts import build_draft  # noqa: E402
from orderbot.nlu import parse_local  # noqa: E402

TZ = ZoneInfo("Asia/Tashkent")
NOW = datetime(2026, 11, 3, 12, 0, tzinfo=TZ)
GOLDEN_PATH = BASE_DIR / "tests" / "orders_golden.jsonl"


@dataclass
class CaseError:
    case_id: int
    phrase: str
    detail: str


@dataclass
class Metrics:
    name: str
    cases: int = 0
    items: int = 0
    catalog_ok: int = 0
    qty_ok: int = 0
    unit_ok: int = 0
    date_ok: int = 0
    order_ok: int = 0
    errors: list[CaseError] = field(default_factory=list)

    def pct(self, value: int, total: int) -> str:
        return f"{value / total * 100:.1f}%" if total else "—"

    def report(self, show_errors: int = 8) -> None:
        print(f"\n{self.name}")
        print("-" * 70)
        print(f"  заказов:             {self.cases}")
        print(f"  позиций:             {self.items}")
        print(f"  catalog_id:          {self.catalog_ok}/{self.items}  ({self.pct(self.catalog_ok, self.items)})")
        print(f"  количество:           {self.qty_ok}/{self.items}  ({self.pct(self.qty_ok, self.items)})")
        print(f"  единица:              {self.unit_ok}/{self.items}  ({self.pct(self.unit_ok, self.items)})")
        print(f"  дата доставки:        {self.date_ok}/{self.cases}  ({self.pct(self.date_ok, self.cases)})")
        print(f"  заказ целиком:        {self.order_ok}/{self.cases}  ({self.pct(self.order_ok, self.cases)})")
        if self.errors:
            print(f"\n  Ошибки (первые {min(show_errors, len(self.errors))}):")
            for error in self.errors[:show_errors]:
                print(f"   #{error.case_id}: {error.phrase}")
                print(f"      {error.detail}")


def load_cases(path: Path) -> list[dict[str, Any]]:
    cases = []
    with path.open("r", encoding="utf-8") as stream:
        for line_no, line in enumerate(stream, start=1):
            line = line.strip()
            if not line:
                continue
            try:
                case = json.loads(line)
            except json.JSONDecodeError as exc:
                raise ValueError(f"{path}:{line_no}: {exc}") from exc
            if not case.get("phrase") or not case.get("items"):
                raise ValueError(f"{path}:{line_no}: нужны phrase и items")
            cases.append(case)
    if not cases:
        raise ValueError(f"{path}: пустой набор")
    return cases


def _num(value: Any) -> float:
    try:
        return round(float(value), 3)
    except (TypeError, ValueError):
        return 0.0


def _draft_items(draft: dict[str, Any]) -> list[dict[str, Any]]:
    result = []
    for item in draft.get("items", []):
        qty = item.get("final_qty")
        if qty is None:
            qty = item.get("qty")
        unit = item.get("final_unit") or item.get("unit")
        result.append(
            {
                "catalog_id": item.get("catalog_id"),
                "qty": _num(qty) if qty is not None else None,
                "unit": unit,
            }
        )
    return result


def compare(
    metrics: Metrics,
    case: dict[str, Any],
    draft: dict[str, Any] | None,
) -> None:
    metrics.cases += 1
    expected = case["items"]
    metrics.items += len(expected)
    if draft is None:
        metrics.errors.append(CaseError(case["id"], case["phrase"], "нет результата"))
        return

    actual = _draft_items(draft)
    problems: list[str] = []

    if len(actual) != len(expected):
        problems.append(f"позиций {len(actual)}, ожидалось {len(expected)}")

    for index, expected_item in enumerate(expected):
        if index >= len(actual):
            problems.append(f"позиция {index + 1}: пропущена")
            continue
        actual_item = actual[index]
        if actual_item["catalog_id"] == expected_item.get("catalog_id"):
            metrics.catalog_ok += 1
        else:
            problems.append(
                f"позиция {index + 1}: catalog_id {actual_item['catalog_id']} != {expected_item.get('catalog_id')}"
            )
        if actual_item["qty"] == _num(expected_item.get("qty")):
            metrics.qty_ok += 1
        else:
            problems.append(
                f"позиция {index + 1}: qty {actual_item['qty']} != {expected_item.get('qty')}"
            )
        if actual_item["unit"] == expected_item.get("unit"):
            metrics.unit_ok += 1
        else:
            problems.append(
                f"позиция {index + 1}: unit {actual_item['unit']} != {expected_item.get('unit')}"
            )

    actual_date = draft.get("delivery_date")
    if actual_date == case.get("delivery_date"):
        metrics.date_ok += 1
    else:
        problems.append(f"дата {actual_date} != {case.get('delivery_date')}")

    if not problems and len(actual) == len(expected):
        metrics.order_ok += 1
    else:
        metrics.errors.append(CaseError(case["id"], case["phrase"], "; ".join(problems)))


def build_from_parsed(
    parsed,
    catalog: Catalog,
    settings: Settings,
) -> dict[str, Any]:
    return build_draft(
        parsed=parsed,
        catalog=catalog,
        chat_id=1,
        user_id=1,
        user_name="Eval",
        username=None,
        source="eval",
        transcript=None,
        accept_threshold=settings.fuzzy_accept,
        candidate_threshold=settings.fuzzy_candidate,
    )


def evaluate_local(
    cases: list[dict[str, Any]],
    catalog: Catalog,
    settings: Settings,
) -> Metrics:
    metrics = Metrics("Локальный парсер")
    for case in cases:
        text = case["phrase"]
        # Голосовые кейсы проходят ту же коррекцию, что и в боте.
        if case.get("voice"):
            text = correct_voice_text(text, catalog)
        parsed = parse_local(text, TZ, now=NOW)
        draft = build_from_parsed(parsed, catalog, settings)
        compare(metrics, case, draft)
    return metrics


async def evaluate_deepseek(
    cases: list[dict[str, Any]],
    catalog: Catalog,
    settings: Settings,
) -> Metrics | None:
    if not settings.deepseek_api_key:
        return None

    from orderbot.ai import AIClient

    metrics = Metrics("DeepSeek")
    ai = AIClient(settings, catalog)
    try:
        for case in cases:
            if not ai.deepseek_enabled:
                compare(metrics, case, None)
                continue
            payload = await ai._deepseek_json(case["phrase"], NOW.date())
            parsed = ai._order_from_payload(payload, case["phrase"], "deepseek", NOW)
            if parsed is None:
                compare(metrics, case, None)
                continue
            draft = build_from_parsed(parsed, catalog, settings)
            compare(metrics, case, draft)
    finally:
        await ai.close()
    return metrics


def main() -> int:
    parser = argparse.ArgumentParser(description="Точность разбора заказов")
    parser.add_argument("--golden", type=Path, default=GOLDEN_PATH)
    parser.add_argument("--limit", type=int, default=0, help="проверить первые N заказов")
    parser.add_argument("--local-only", action="store_true")
    parser.add_argument("--deepseek-only", action="store_true")
    parser.add_argument("--show-errors", type=int, default=8)
    args = parser.parse_args()
    if args.local_only and args.deepseek_only:
        parser.error("--local-only и --deepseek-only несовместимы")

    cases = load_cases(args.golden)
    if args.limit and args.limit > 0:
        cases = cases[: args.limit]

    settings = load_settings()
    catalog = Catalog.load(settings.catalog_path)
    print("=" * 70)
    print("ОЦЕНКА РАЗБОРА ЗАКАЗОВ")
    print("=" * 70)
    print(f"Золотой набор: {args.golden} ({len(cases)} заказов)")
    print(f"Каталог:       {settings.catalog_path} ({len(catalog.items)} позиций)")
    print(f"Дата отчёта:   {NOW.isoformat()}")

    if not args.deepseek_only:
        local = evaluate_local(cases, catalog, settings)
        local.report(args.show_errors)

    if not args.local_only:
        if settings.deepseek_api_key:
            metrics = asyncio.run(evaluate_deepseek(cases, catalog, settings))
            if metrics is not None:
                metrics.report(args.show_errors)
        else:
            print("\nDeepSeek")
            print("-" * 70)
            print("  ключ не задан — оценка DeepSeek пропущена")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
