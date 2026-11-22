#!/usr/bin/env python3
"""Сравнение моделей ИИ на золотом наборе: точность, скорость, цена (E7).

Запуск:
    venv/bin/python scripts/compare_models.py --provider gemini --models gemini-3.8-flash
    venv/bin/python scripts/compare_models.py --provider deepseek --models deepseek-chat --limit 20

Печатает таблицу «модель · точность заказов · средняя секунда на заказ».
Цену задайте вручную: --price-in/--price-out за 1 млн токенов, если известна
(оценка печатается справа и носит справочный характер).
"""
from __future__ import annotations

import argparse
import asyncio
import dataclasses
import sys
import time
from pathlib import Path

BASE_DIR = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(BASE_DIR))
sys.path.insert(0, str(BASE_DIR / "scripts"))

import eval_parse as ep  # noqa: E402
from orderbot.catalog import Catalog  # noqa: E402
from orderbot.config import load_settings  # noqa: E402


async def run_model(
    provider: str,
    model: str,
    cases: list[dict],
    catalog: Catalog,
    settings,
) -> tuple[ep.Metrics, float, int]:
    """Один прогон: метрики, среднее время на заказ, число ответов с ошибкой."""
    from orderbot.ai import AIClient

    tuned = dataclasses.replace(
        settings, gemini_model=model, deepseek_model=model
    )
    ai = AIClient(tuned, catalog)
    metrics = ep.Metrics(f"{provider}:{model}")
    latencies: list[float] = []
    failed = 0
    try:
        for case in cases:
            started = time.monotonic()
            try:
                if provider == "deepseek":
                    if not ai.deepseek_enabled:
                        failed += 1
                        ep.compare(metrics, case, None)
                        continue
                    payload = await ai._deepseek_json(case["phrase"], ep.NOW.date())
                    parsed = ai._order_from_payload(
                        payload, case["phrase"], "deepseek", ep.NOW
                    )
                else:
                    if not ai.gemini_enabled:
                        failed += 1
                        ep.compare(metrics, case, None)
                        continue
                    payload = await ai._gemini_json(case["phrase"], ep.NOW.date())
                    parsed = ai._order_from_payload(
                        payload, case["phrase"], "gemini", ep.NOW
                    )
            except Exception as exc:  # сеть, ключ, лимит — считаем провалом кейса
                failed += 1
                print(f"  ! {case.get('phrase', '')[:40]}: {exc}")
                ep.compare(metrics, case, None)
                continue
            latencies.append(time.monotonic() - started)
            if parsed is None or not parsed.items:
                ep.compare(metrics, case, None)
                continue
            draft = ep.build_from_parsed(parsed, catalog, tuned)
            ep.compare(metrics, case, draft)
    finally:
        await ai.close()
    average = sum(latencies) / len(latencies) if latencies else 0.0
    return metrics, average, failed


async def main_async(args) -> int:
    settings = load_settings()
    catalog = Catalog.load(settings.catalog_path)
    cases = ep.load_cases(args.golden)
    if args.limit:
        cases = cases[: args.limit]
    models = [m.strip() for m in args.models.split(",") if m.strip()]
    if not models:
        print("Не заданы модели: --models gemini-3.8-flash,gemini-3.8-pro")
        return 2
    print(f"Золотой набор: {len(cases)} заказов · провайдер {args.provider}")
    print("-" * 74)
    print(f"{'модель':34} {'точность':>10} {'сек/заказ':>11} {'сбоев':>7}")
    best: tuple[str, float] | None = None
    for model in models:
        metrics, average, failed = await run_model(
            args.provider, model, cases, catalog, settings
        )
        percent = ep.accuracy(metrics)
        print(f"{model[:34]:34} {percent:9.1f}% {average:11.2f} {failed:7}")
        if best is None or percent > best[1]:
            best = (model, percent)
    print("-" * 74)
    if best:
        print(f"Лучшая по точности: {best[0]} — {best[1]:.1f}%")
    print(
        "Цена считается по тарифу провайдера: умножьте токены из логов "
        "llm_usage на цену за миллион и сравните с точностью выше."
    )
    return 0


def main() -> int:
    parser = argparse.ArgumentParser(description="Сравнение моделей ИИ (E7)")
    parser.add_argument("--provider", choices=("gemini", "deepseek"), default="gemini")
    parser.add_argument("--models", default="", help="через запятую")
    parser.add_argument("--limit", type=int, default=0)
    parser.add_argument("--golden", type=Path, default=ep.GOLDEN_PATH)
    args = parser.parse_args()
    return asyncio.run(main_async(args))


if __name__ == "__main__":
    raise SystemExit(main())
