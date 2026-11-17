"""Построение черновика заказа: сопоставление с прайсом и пересчёт сумм."""
from __future__ import annotations

import secrets
from datetime import date, datetime
from typing import Any

from .catalog import Catalog, CatalogItem, conflicting, normalize, risk_signature
from .nlu import ParsedOrder
from .units import canon_unit, convert_qty, convert_retail_pack

RESOLVED_STATUSES = {"ok", "not_in_catalog", "skipped"}


def _money(value: float | None) -> float | None:
    return None if value is None else round(float(value), 2)


def apply_catalog(item: dict[str, Any], catalog_item: CatalogItem) -> None:
    item["catalog_id"] = catalog_item.id
    item["name"] = catalog_item.name
    item["price"] = float(catalog_item.price)
    item["pack"] = catalog_item.pack
    item["catalog_unit"] = catalog_item.unit
    item["unit_warning"] = False
    item["sum"] = None

    qty = item.get("qty")
    if qty is None:
        item["final_qty"] = None
        item["final_unit"] = catalog_item.unit
        return

    converted = convert_qty(qty, item.get("unit"), catalog_item.unit)
    if converted is None:
        converted = convert_retail_pack(
            qty, item.get("unit"), catalog_item.pack, catalog_item.unit
        )
    if converted is None:
        item["final_qty"] = float(qty)
        item["final_unit"] = canon_unit(item.get("unit")) or catalog_item.unit
        item["unit_warning"] = True
        return
    item["final_qty"] = round(float(converted), 3)
    item["final_unit"] = catalog_item.unit
    item["sum"] = _money(converted * catalog_item.price)


def _base_item(raw: str, qty: float | None, unit: str | None, status: str = "new") -> dict[str, Any]:
    return {
        "raw": raw,
        "qty": qty,
        "unit": canon_unit(unit),
        "catalog_id": None,
        "name": None,
        "price": None,
        "pack": None,
        "catalog_unit": None,
        "final_qty": qty,
        "final_unit": canon_unit(unit),
        "sum": None,
        "unit_warning": False,
        "status": status,
        "candidates": [],
        "score": 0.0,
        "note": "",
    }


def _resolve_with_catalog(
    item: dict[str, Any],
    catalog: Catalog,
    accept_threshold: float,
    candidate_threshold: float,
) -> None:
    raw = item.get("raw") or ""
    matches = catalog.match(raw, limit=5, min_score=max(35.0, candidate_threshold - 15))
    top = matches[0] if matches else None
    runner_up = matches[1] if len(matches) > 1 else None

    confident = bool(
        top
        and top.score >= accept_threshold
        and (runner_up is None or top.score - runner_up.score >= 4)
    )
    if confident and top is not None:
        # Не принимать молча вариант с другой жирностью/сортом/состоянием.
        extra = risk_signature(top.item.name) - risk_signature(raw)
        variants = {frozenset(risk_signature(m.item.name)) for m in matches[:4]}
        if conflicting(raw, top.item.name) or (extra and len(variants) > 1):
            confident = False
    if confident and top is not None:
        apply_catalog(item, top.item)
        item["score"] = top.score
        item["status"] = "ok" if item["qty"] is not None else "needs_qty"
        item["note"] = "" if item["qty"] is not None else "количество не указано"
        item["candidates"] = [m.item.id for m in matches[1:5] if m.score >= candidate_threshold]
        return

    candidates = [m.item.id for m in matches if m.score >= candidate_threshold][:4]
    item["status"] = "needs_choice"
    item["candidates"] = candidates
    item["score"] = top.score if top else 0.0


def build_draft(
    parsed: ParsedOrder,
    catalog: Catalog,
    chat_id: int,
    user_id: int,
    user_name: str,
    username: str | None,
    source: str,
    transcript: str | None,
    accept_threshold: float = 88.0,
    candidate_threshold: float = 55.0,
    aliases: dict[str, int] | None = None,
) -> dict[str, Any]:
    items: list[dict[str, Any]] = []
    for parsed_item in parsed.items:
        item = _base_item(parsed_item.raw, parsed_item.qty, parsed_item.unit)
        # Сначала — ранее подтверждённый выбор пользователя для этой фразы.
        if aliases:
            alias_item = catalog.get(aliases.get(normalize(parsed_item.raw)))
            if alias_item is not None:
                apply_catalog(item, alias_item)
                item["status"] = "ok" if item["qty"] is not None else "needs_qty"
                item["score"] = 100.0
                items.append(item)
                continue
        if parsed_item.catalog_id is not None:
            catalog_item = catalog.get(parsed_item.catalog_id)
            if catalog_item is not None:
                apply_catalog(item, catalog_item)
                item["status"] = "ok" if item["qty"] is not None else "needs_qty"
                item["score"] = 100.0
                if parsed_item.candidates:
                    item["candidates"] = [
                        cid for cid in parsed_item.candidates if catalog.get(cid) is not None
                    ][:4]
                items.append(item)
                continue
        if parsed_item.candidates:
            valid = [cid for cid in parsed_item.candidates if catalog.get(cid) is not None][:4]
            if valid:
                item["candidates"] = valid
                item["status"] = "needs_choice"
                items.append(item)
                continue
        _resolve_with_catalog(item, catalog, accept_threshold, candidate_threshold)
        items.append(item)

    # объединяем одинаковые позиции
    merged: list[dict[str, Any]] = []
    for item in items:
        duplicate = None
        if item["status"] in ("ok", "needs_qty") and item.get("catalog_id") is not None and item.get("qty") is not None:
            for existing in merged:
                if (
                    existing.get("catalog_id") == item["catalog_id"]
                    and existing.get("status") in ("ok", "needs_qty")
                    and existing.get("qty") is not None
                    and existing.get("final_unit") == item.get("final_unit")
                ):
                    duplicate = existing
                    break
        if duplicate is not None:
            duplicate["qty"] = float(duplicate["qty"]) + float(item["qty"])
            duplicate["final_qty"] = float(duplicate["final_qty"] or 0) + float(item["final_qty"] or 0)
            duplicate["sum"] = _money((duplicate.get("sum") or 0) + (item.get("sum") or 0))
            duplicate["raw"] = f"{duplicate['raw']} + {item['raw']}"
            continue
        merged.append(item)

    for index, item in enumerate(merged):
        item["idx"] = index

    now = datetime.now()
    draft = {
        "id": secrets.token_hex(4),
        "chat_id": chat_id,
        "user_id": user_id,
        "user_name": user_name,
        "username": username,
        "raw_text": parsed.raw_text,
        "transcript": transcript,
        "source": source,
        "provider": parsed.provider,
        "delivery_date": parsed.delivery_date.isoformat(),
        "delivery_time": parsed.delivery_time,
        "comment": parsed.comment,
        "items": merged,
        "summary_sent": False,
        "created_at": now.strftime("%Y-%m-%d %H:%M:%S"),
        "status": "draft",
    }
    return draft


def draft_from_order(
    order: dict[str, Any],
    chat_id: int,
    user_id: int,
    user_name: str,
    username: str | None,
    delivery_date: date,
) -> dict[str, Any]:
    """Повтор заказа: те же позиции, доставка на завтра, без вопросов."""
    items: list[dict[str, Any]] = []
    for source in order.get("items", []):
        if source.get("status") == "skipped":
            continue
        qty = float(source.get("qty") if source.get("qty") is not None else 1)
        unit = source.get("unit") or "шт"
        price = float(source.get("price") or 0)
        total = source.get("total")
        if total is None:
            total = round(qty * price, 2)
        catalog_id = source.get("catalog_id")
        status = "ok" if catalog_id else "not_in_catalog"
        items.append(
            {
                "raw": source.get("raw") or source.get("name") or "",
                "qty": qty,
                "unit": unit,
                "catalog_id": catalog_id,
                "name": source.get("name"),
                "price": price,
                "pack": None,
                "catalog_unit": unit,
                "final_qty": qty,
                "final_unit": unit,
                "sum": round(float(total), 2) if status == "ok" else None,
                "unit_warning": False,
                "status": status,
                "candidates": [],
                "score": 100.0,
                "note": "",
            }
        )
    for index, item in enumerate(items):
        item["idx"] = index

    now = datetime.now()
    return {
        "id": secrets.token_hex(4),
        "chat_id": chat_id,
        "user_id": user_id,
        "user_name": user_name,
        "username": username,
        "raw_text": None,
        "transcript": None,
        "source": "repeat",
        "provider": "repeat",
        "delivery_date": delivery_date.isoformat(),
        "delivery_time": None,
        "comment": order.get("comment"),
        "items": items,
        "summary_sent": False,
        "questions": 0,
        "created_at": now.strftime("%Y-%m-%d %H:%M:%S"),
        "status": "draft",
    }


def draft_from_usual(
    catalog_item: CatalogItem,
    qty: float,
    unit: str,
    chat_id: int,
    user_id: int,
    user_name: str,
    username: str | None,
    delivery_date: date,
) -> dict[str, Any]:
    """Заказ «как обычно»: одна позиция с обычным количеством, без вопросов."""
    price = float(catalog_item.price)
    total = round(float(qty) * price, 2)
    item = {
        "raw": catalog_item.name,
        "qty": float(qty),
        "unit": unit,
        "catalog_id": catalog_item.id,
        "name": catalog_item.name,
        "price": price,
        "pack": catalog_item.pack,
        "catalog_unit": catalog_item.unit,
        "final_qty": float(qty),
        "final_unit": unit,
        "sum": total,
        "unit_warning": False,
        "status": "ok",
        "candidates": [],
        "score": 100.0,
        "note": "",
        "idx": 0,
    }
    now = datetime.now()
    return {
        "id": secrets.token_hex(4),
        "chat_id": chat_id,
        "user_id": user_id,
        "user_name": user_name,
        "username": username,
        "raw_text": None,
        "transcript": None,
        "source": "usual",
        "provider": "usual",
        "delivery_date": delivery_date.isoformat(),
        "delivery_time": None,
        "comment": "",
        "items": [item],
        "summary_sent": False,
        "questions": 0,
        "created_at": now.strftime("%Y-%m-%d %H:%M:%S"),
        "status": "draft",
    }


def draft_total(draft: dict[str, Any]) -> float:
    total = 0.0
    for item in draft.get("items", []):
        if item.get("status") == "ok" and item.get("sum"):
            total += float(item["sum"])
    return round(total, 2)


def all_resolved(draft: dict[str, Any]) -> bool:
    return all(item.get("status") in RESOLVED_STATUSES for item in draft.get("items", []))


def unit_warnings(draft: dict[str, Any]) -> list[dict[str, Any]]:
    return [
        item
        for item in draft.get("items", [])
        if item.get("status") == "ok" and item.get("unit_warning")
    ]


def resolve_item(draft: dict[str, Any], idx: int, catalog_item: CatalogItem) -> dict[str, Any] | None:
    for item in draft["items"]:
        if item["idx"] == idx:
            apply_catalog(item, catalog_item)
            item["status"] = "ok" if item.get("qty") is not None else "needs_qty"
            item["note"] = "" if item.get("qty") is not None else "количество не указано"
            return item
    return None


def set_item_qty(draft: dict[str, Any], idx: int, qty: float, catalog: Catalog) -> dict[str, Any] | None:
    for item in draft["items"]:
        if item["idx"] == idx:
            item["qty"] = float(qty)
            catalog_item = catalog.get(item.get("catalog_id"))
            if catalog_item is not None:
                apply_catalog(item, catalog_item)
                item["status"] = "ok"
            else:
                item["status"] = "not_in_catalog"
                item["final_qty"] = float(qty)
            item["note"] = ""
            return item
    return None


def mark_not_in_catalog(draft: dict[str, Any], idx: int) -> dict[str, Any] | None:
    for item in draft["items"]:
        if item["idx"] == idx:
            item["status"] = "not_in_catalog"
            item["final_qty"] = item.get("qty") or 1
            item["final_unit"] = item.get("unit")
            item["sum"] = None
            return item
    return None


def mark_skipped(draft: dict[str, Any], idx: int) -> dict[str, Any] | None:
    for item in draft["items"]:
        if item["idx"] == idx:
            item["status"] = "skipped"
            item["sum"] = None
            return item
    return None


def delivery_text(draft: dict[str, Any]) -> str:
    try:
        day = date.fromisoformat(draft["delivery_date"])
    except (TypeError, ValueError):
        return "не указана"
    text = day.strftime("%d.%m.%Y")
    if draft.get("delivery_time"):
        text += f" к {draft['delivery_time']}"
    return text


def order_items_payload(draft: dict[str, Any]) -> list[dict[str, Any]]:
    payload: list[dict[str, Any]] = []
    for item in draft["items"]:
        if item.get("status") == "skipped":
            continue
        payload.append(
            {
                "catalog_id": item.get("catalog_id"),
                "name": item.get("name") or item.get("raw"),
                "qty": item.get("final_qty") if item.get("final_qty") is not None else 1,
                "unit": item.get("final_unit") or item.get("unit") or "шт",
                "price": item.get("price") or 0,
                "total": item.get("sum") or 0,
                "raw": item.get("raw"),
                "status": "unit_warning" if item.get("unit_warning") else item.get("status"),
                "stock_note": item.get("stock_note"),
            }
        )
    return payload
