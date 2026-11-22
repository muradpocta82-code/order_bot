"""AI-провайдеры: DeepSeek для JSON-разбора, Gemini для голоса и fallback."""
from __future__ import annotations

import asyncio
import hashlib
import json
import logging
import re
import time
from datetime import date, datetime, timedelta
from typing import Any

from google import genai
from google.genai import types as genai_types
from openai import AsyncOpenAI

from .catalog import Catalog
from .nlu import parse_local
from .config import Settings
from .nlu import ParsedItem, ParsedOrder

log = logging.getLogger(__name__)

WEEKDAYS_RU = (
    "понедельник", "вторник", "среда", "четверг", "пятница", "суббота", "воскресенье",
)

RU_MONTHS = {
    "января": 1, "февраля": 2, "марта": 3, "апреля": 4, "мая": 5, "июня": 6,
    "июля": 7, "августа": 8, "сентября": 9, "октября": 10, "ноября": 11, "декабря": 12,
}

SYSTEM_PROMPT = """Ты — ассистент оптового поставщика продуктов для кафе, ресторанов и пекарен.
Сегодня {today} ({weekday}), часовой пояс Asia/Tashkent.

Разбери заказ повара и верни СТРОГО один JSON-объект без markdown и пояснений.

Каталог (id | название | единица | фасовка | цена, USD):
{catalog}

Формат ответа:
{{"items":[{{"raw":"бекона коробку","catalog_id":1,"qty":1,"unit":"коробка","candidates":[]}}], "delivery_date":"2026-11-04", "delivery_time":"10:00", "comment":""}}

Правила:
- qty — число (может быть дробным); если количество не названо — null.
- unit — единица измерения из фразы повара (кг, л, шт, упаковка, пакет, коробка, ведро, мешок...).
- catalog_id — id подходящей позиции каталога. Если не уверен — null, а в candidates перечисли до 3 наиболее вероятных id.
- raw — исходная фраза позиции без количества.
- delivery_date — ISO-дата (YYYY-MM-DD); сегодня/завтра/послезавтра считай от {today}. Если дата не названа — null.
- delivery_time — время в формате HH:MM или null.
- comment — просьбы и уточнения, не относящиеся к позициям (например, «позвонить за час»).
- Не придумывай позиции, которых нет в заказе. Если заказ пустой — {{"items":[]}}.
- Верни только JSON."""


SHADOW_PROMPT = """Ты — старший кладовщик оптового поставщика продуктов для HoReCa.
Сегодня {today} ({weekday}), часовой пояс Asia/Tashkent.

Задача: разобрать заказ повара максимально строго и вернуть один JSON без пояснений.

Каталог (id | название | единица | фасовка | цена, USD):
{catalog}

Формат: {{"items":[{{"raw":"...","catalog_id":1,"qty":1,"unit":"коробка","candidates":[]}}], "delivery_date":null, "delivery_time":null, "comment":""}}

Требования:
- catalog_id обязателен, если позиция есть в каталоге; иначе null и до 3 id в candidates.
- qty — число или null; единицу бери из фразы повара, не подменяй на единицу каталога.
- Не объединяй разные позиции и не придумывай позиции, которых не было.
- delivery_date — ISO-дата от {today}; если не названа — null.
- Верни только JSON."""


def _is_auth_error(exc: Exception) -> bool:
    for attr in ("status_code", "code", "http_status"):
        value = getattr(exc, attr, None)
        try:
            if int(value) in (401, 403):
                return True
        except (TypeError, ValueError):
            pass
    text = str(exc).lower()
    markers = (
        "api key not valid", "unauthenticated", "invalid api key", "incorrect api key",
        "access_token_type_unsupported", "api_key_service_blocked", "unauthorized",
        "authenticationerror", "permissiondenied",
    )
    return any(marker in text for marker in markers)


def _is_quota_error(exc: Exception) -> bool:
    text = str(exc).lower()
    return any(
        marker in text
        for marker in (
            "429",
            "resource_exhausted",
            "resource exhausted",
            "quota",
            "rate limit",
            "too many requests",
            "exceeded your current quota",
        )
    )


def _retry_delay_seconds(exc: Exception, default: float = 900.0) -> float:
    """Достаёт паузу из ответа Gemini (retryDelay / «retry in 14h9m»)."""
    text = str(exc)
    match = re.search(r"retryDelay['\"]?\s*:\s*['\"]?(\d+(?:\.\d+)?)s", text, re.IGNORECASE)
    if match:
        seconds = float(match.group(1))
    else:
        match = re.search(
            r"retry in\s+(?:(\d+)h)?\s*(?:(\d+)m)?\s*(\d+(?:\.\d+)?)s",
            text,
            re.IGNORECASE,
        )
        if not match:
            return default
        seconds = (
            int(match.group(1) or 0) * 3600
            + int(match.group(2) or 0) * 60
            + float(match.group(3))
        )
    # Не уходим в спячку навсегда, но и не долбим API каждую минуту.
    return min(max(seconds, 60.0), 12 * 3600.0)


def _short_error(exc: Exception, limit: int = 220) -> str:
    text = " ".join(str(exc).split())
    return text if len(text) <= limit else text[: limit - 1] + "…"


def _extract_json(raw: str | None) -> dict[str, Any] | None:
    if not raw:
        return None
    text = raw.strip()
    if text.startswith("```"):
        text = re.sub(r"^```(?:json)?\s*", "", text)
        text = re.sub(r"\s*```$", "", text)
    start = text.find("{")
    end = text.rfind("}")
    if start == -1 or end == -1 or end <= start:
        return None
    try:
        payload = json.loads(text[start : end + 1])
    except json.JSONDecodeError:
        return None
    return payload if isinstance(payload, dict) else None


def _safe_qty(value: Any) -> float | None:
    if isinstance(value, str):
        match = re.search(r"\d+(?:[.,]\d+)?", value)
        if not match:
            return None
        value = match.group(0).replace(",", ".")
    try:
        qty = float(value)
    except (TypeError, ValueError):
        return None
    if qty <= 0 or qty > 100000:
        return None
    return round(qty, 3)


class AIClient:
    def __init__(self, settings: Settings, catalog: Catalog):
        self.settings = settings
        self.catalog = catalog
        self.deepseek_enabled = bool(settings.deepseek_api_key)
        self.gemini_enabled = bool(settings.gemini_api_key)
        self.deepseek_status = "not_configured" if not settings.deepseek_api_key else "unknown"
        self.gemini_status = "not_configured" if not settings.gemini_api_key else "unknown"
        self.gemini_model_active = settings.gemini_model
        # model -> monotonic-время, до которого запросы к модели не делаем
        self._gemini_cooldown: dict[str, float] = {}
        self._gemini_quota_logged: set[str] = set()
        self.started_at = time.time()
        self.last_error_at: dict[str, float] = {}
        self.last_ok_at: dict[str, float] = {}
        self.calls_today: dict[str, int] = {"deepseek": 0, "gemini": 0}
        self._cache: dict[str, tuple[float, "ParsedOrder"]] = {}
        self._user_calls: dict[int, list[float]] = {}
        self.budget_blocked = 0
        self._calls_date = date.today()
        self._deepseek = (
            AsyncOpenAI(
                api_key=settings.deepseek_api_key,
                base_url=settings.deepseek_base_url,
                timeout=45.0,
                max_retries=1,
            )
            if settings.deepseek_api_key
            else None
        )
        self._gemini = genai.Client(api_key=settings.gemini_api_key) if settings.gemini_api_key else None

    # ------------------------------------------------------------------
    # диагностика
    # ------------------------------------------------------------------
    def _count_call(self, provider: str) -> None:
        today = date.today()
        if today != self._calls_date:
            self._calls_date = today
            self.calls_today = {"deepseek": 0, "gemini": 0}
        self.calls_today[provider] = self.calls_today.get(provider, 0) + 1

    def _mark_ok(self, provider: str) -> None:
        self.last_ok_at[provider] = time.time()

    def _mark_error(self, provider: str) -> None:
        self.last_error_at[provider] = time.time()

    def provider_down(self, provider: str, seconds: float = 600.0) -> bool:
        enabled = (
            self.deepseek_enabled
            if provider == "deepseek"
            else self.gemini_enabled
        )
        error = self.last_error_at.get(provider)
        ok = self.last_ok_at.get(provider, 0.0)
        return bool(
            enabled
            and error is not None
            and error > ok
            and time.time() - error >= seconds
        )

    async def check_health(self) -> None:
        if self._deepseek is not None:
            try:
                await asyncio.wait_for(self._deepseek.models.list(), timeout=15)
                self.deepseek_enabled = True
                self.deepseek_status = "ok"
                log.info("DeepSeek: ключ принят")
            except Exception as exc:
                if _is_auth_error(exc):
                    self.deepseek_enabled = False
                    self.deepseek_status = "auth_failed"
                    log.warning("DeepSeek: ключ отклонён (%s) — текстовый разбор пойдёт локально", exc)
                else:
                    self.deepseek_status = "unreachable"
                    log.warning("DeepSeek: проверка не удалась (%s) — попробую позже", exc)
        else:
            log.info("DeepSeek: ключ не задан — используется локальный разбор")

        if self._gemini is not None:
            # Проверка генерацией ("ping") тратила бесплатную квоту Gemini
            # (20 запросов/день на модель) и на каждом старте получала 429.
            # Проверяем лениво: первый реальный запрос определит доступность,
            # а ошибки обрабатываются в _gemini_json/transcribe_audio.
            self.gemini_status = "lazy"
            log.info(
                "Gemini: ключ задан, модель %s — проверка при первом запросе "
                "(без траты квоты)",
                self.settings.gemini_model,
            )
        else:
            log.info("Gemini: ключ не задан — голос через офлайн Vosk")

    # ------------------------------------------------------------------
    # разбор текста
    # ------------------------------------------------------------------
    def _system_prompt(self, today: date) -> str:
        catalog_lines = [
            f"{item.id} | {item.name} | {item.unit} | {item.pack} | {item.price:.0f}"
            for item in self.catalog.items
        ]
        template = SHADOW_PROMPT if getattr(self, "_shadow_mode", False) else SYSTEM_PROMPT
        return template.format(
            today=today.isoformat(),
            weekday=WEEKDAYS_RU[today.weekday()],
            catalog="\n".join(catalog_lines),
        )

    def _cache_key(self, text: str) -> str:
        normalized = " ".join(text.lower().split())
        return hashlib.sha1(
            f"{normalized}|{len(self.catalog.items)}".encode("utf-8")
        ).hexdigest()

    def _cache_get(self, key: str) -> "ParsedOrder | None":
        entry = self._cache.get(key)
        if not entry:
            return None
        created, order = entry
        if time.time() - created > 7 * 24 * 3600:
            self._cache.pop(key, None)
            return None
        return order

    def _user_allowed(self, user_id: int | None) -> bool:
        limit = int(getattr(self.settings, "llm_user_hourly", 0) or 0)
        if not limit or user_id is None:
            return True
        now = time.time()
        stamps = [value for value in self._user_calls.get(user_id, []) if now - value < 3600]
        self._user_calls[user_id] = stamps
        return len(stamps) < limit

    def _note_user_call(self, user_id: int | None) -> None:
        if user_id is None:
            return
        self._user_calls.setdefault(user_id, []).append(time.time())

    def _budget_left(self) -> bool:
        budget = int(getattr(self.settings, "llm_daily_budget", 0) or 0)
        if not budget:
            return True
        used = sum(self.calls_today.values())
        return used < budget

    async def parse(
        self, text: str, now: datetime, user_id: int | None = None
    ) -> ParsedOrder | None:
        today = now.date()
        if self.settings.local_first and self._local_ready(text, now):
            local = parse_local(text, self.settings.timezone, now)
            if local and local.items:
                log.info("Разбор локальный: %d позиций", len(local.items))
                return local
        key = self._cache_key(text)
        cached = self._cache_get(key)
        if cached is not None:
            log.info("Разбор из кэша: %d позиций", len(cached.items))
            return cached
        if not self._budget_left():
            self.budget_blocked += 1
            log.warning("Дневной бюджет ИИ исчерпан — разбираю локально")
            return None
        if not self._user_allowed(user_id):
            log.warning("Лимит ИИ на пользователя %s исчерпан — разбор локальный", user_id)
            return None
        provider = str(getattr(self.settings, "ai_provider", "auto") or "auto")
        if provider in ("auto", "deepseek") and self.deepseek_enabled and self._deepseek is not None:
            payload = await self._deepseek_json(text, today)
            order = self._order_from_payload(payload, text, "deepseek", now)
            if order and order.items:
                self._note_user_call(user_id)
                self._cache[key] = (time.time(), order)
                log.info("Разбор DeepSeek: %d позиций", len(order.items))
                return order

        if provider in ("auto", "gemini") and self.gemini_enabled and self._gemini is not None:
            payload = await self._gemini_json(text, today)
            order = self._order_from_payload(payload, text, "gemini", now)
            if order and order.items:
                self._note_user_call(user_id)
                self._cache[key] = (time.time(), order)
                log.info("Разбор Gemini: %d позиций", len(order.items))
                return order
        return None

    async def shadow_parse(self, text: str, now: datetime):
        """Разбор тем же провайдером, но новым промптом (E10).

        Ничего не меняет у клиента: результат только сравнивается и логируется.
        """
        if not (self.deepseek_enabled or self.gemini_enabled):
            return None
        today = now.date()
        previous = getattr(self, "_shadow_mode", False)
        self._shadow_mode = True
        try:
            if self.deepseek_enabled and self._deepseek is not None:
                payload = await self._deepseek_json(text, today)
                return self._order_from_payload(payload, text, "deepseek-shadow", now)
            if self.gemini_enabled and self._gemini is not None:
                payload = await self._gemini_json(text, today)
                return self._order_from_payload(payload, text, "gemini-shadow", now)
        except Exception:
            log.info("Теневой разбор не удался", exc_info=True)
        finally:
            self._shadow_mode = previous
        return None

    def _local_ready(self, text: str, now: datetime) -> bool:
        """Простые фразы разбираем без ИИ: все позиции с количеством и единицей."""
        if len(text) > 200:
            return False
        try:
            parsed = parse_local(text, self.settings.timezone, now)
        except Exception:
            return False
        if not parsed or not parsed.items or len(parsed.items) > 8:
            return False
        return all(
            item.qty is not None and bool(item.unit) for item in parsed.items
        )

    async def _deepseek_json(self, text: str, today: date) -> dict[str, Any] | None:
        assert self._deepseek is not None
        self._count_call("deepseek")
        try:
            response = await self._deepseek.chat.completions.create(
                model=self.settings.deepseek_model,
                messages=[
                    {"role": "system", "content": self._system_prompt(today)},
                    {"role": "user", "content": f"Текст заказа: «{text}»"},
                ],
                response_format={"type": "json_object"},
                temperature=0,
                max_tokens=1600,
            )
            content = response.choices[0].message.content if response.choices else None
            self._mark_ok("deepseek")
            return _extract_json(content)
        except Exception as exc:
            self._mark_error("deepseek")
            if _is_auth_error(exc):
                self.deepseek_enabled = False
                self.deepseek_status = "auth_failed"
                log.warning("DeepSeek отключён: %s", exc)
            else:
                log.warning("DeepSeek ошибка: %s", exc)
            return None

    async def _gemini_json(self, text: str, today: date) -> dict[str, Any] | None:
        assert self._gemini is not None
        self._count_call("gemini")
        prompt = f"{self._system_prompt(today)}\n\nТекст заказа: «{text}»"
        for model in self._gemini_models():
            try:
                response = await asyncio.wait_for(
                    self._gemini.aio.models.generate_content(
                        model=model,
                        contents=prompt,
                        config=genai_types.GenerateContentConfig(
                            response_mime_type="application/json",
                            temperature=0,
                            max_output_tokens=1600,
                        ),
                    ),
                    timeout=45,
                )
                self._gemini_success(model)
                return _extract_json(getattr(response, "text", "") or "")
            except Exception as exc:
                if self._gemini_failure(model, exc, "JSON"):
                    return None
        return None

    def _gemini_models(self) -> list[str]:
        """Модели в порядке приоритета, кроме тех, что на паузе после ошибки."""
        models = [
            self.gemini_model_active,
            self.settings.gemini_model,
            "gemini-3.8-flash",
            "gemini-flash-latest",
            "gemini-3.5-flash",
        ]
        seen: list[str] = []
        now = time.monotonic()
        for model in models:
            if model and model not in seen:
                seen.append(model)
        return [
            model
            for model in seen
            if self._gemini_cooldown.get(model, 0.0) <= now
        ]

    def _gemini_success(self, model: str) -> None:
        self._mark_ok("gemini")
        self.gemini_enabled = True
        self.gemini_status = "ok"
        self.gemini_model_active = model
        self._gemini_cooldown.pop(model, None)
        self._gemini_quota_logged.discard(model)

    def _gemini_failure(self, model: str, exc: Exception, context: str) -> bool:
        """Ставит модель на паузу. Возвращает True, если ключ отклонён."""
        self._mark_error("gemini")
        if _is_auth_error(exc):
            self.gemini_enabled = False
            self.gemini_status = "auth_failed"
            log.warning("Gemini (%s): ключ отклонён (%s)", context, _short_error(exc))
            return True

        quota = _is_quota_error(exc)
        delay = _retry_delay_seconds(exc) if quota else 120.0
        self._gemini_cooldown[model] = time.monotonic() + delay
        self.gemini_status = "quota" if quota else "degraded"
        minutes = max(1, round(delay / 60))
        if quota and model not in self._gemini_quota_logged:
            self._gemini_quota_logged.add(model)
            log.warning(
                "Gemini (%s): лимит/квота исчерпаны, модель %s на паузе ~%d мин",
                context,
                model,
                minutes,
            )
        elif not quota:
            log.warning(
                "Gemini (%s) %s: %s — пауза ~%d мин",
                context,
                model,
                _short_error(exc),
                minutes,
            )
        else:
            log.debug("Gemini (%s): модель %s всё ещё на паузе", context, model)
        return False

    def _order_from_payload(
        self, payload: dict[str, Any] | None, source_text: str, provider: str, now: datetime
    ) -> ParsedOrder | None:
        if not payload:
            return None
        raw_items = payload.get("items")
        if not isinstance(raw_items, list):
            return None
        items: list[ParsedItem] = []
        for entry in raw_items[:30]:
            if not isinstance(entry, dict):
                continue
            raw = str(entry.get("raw") or "").strip()
            catalog_id: int | None = None
            try:
                candidate_id = int(entry.get("catalog_id"))
                if self.catalog.get(candidate_id) is not None:
                    catalog_id = candidate_id
            except (TypeError, ValueError):
                catalog_id = None
            if not raw and catalog_id is not None:
                item = self.catalog.get(catalog_id)
                raw = item.name if item else ""
            if not raw:
                continue
            candidates: list[int] = []
            for value in entry.get("candidates") or []:
                try:
                    cid = int(value)
                except (TypeError, ValueError):
                    continue
                if cid not in candidates and self.catalog.get(cid) is not None:
                    candidates.append(cid)
            unit = str(entry.get("unit") or "").strip() or None
            items.append(
                ParsedItem(
                    raw=raw,
                    qty=_safe_qty(entry.get("qty")),
                    unit=unit,
                    catalog_id=catalog_id,
                    candidates=candidates[:4],
                )
            )
        if not items:
            return None

        delivery_date = self._parse_iso_date(payload.get("delivery_date"), now.date())
        if delivery_date is None:
            delivery_date = now.date() + timedelta(days=1)
        delivery_time = self._parse_time(payload.get("delivery_time"))
        comment = str(payload.get("comment") or "").strip()
        return ParsedOrder(
            items=items,
            delivery_date=delivery_date,
            delivery_time=delivery_time,
            comment=comment,
            provider=provider,
            raw_text=source_text,
        )

    @staticmethod
    def _parse_iso_date(value: Any, today: date) -> date | None:
        if not value:
            return None
        text = str(value).lower().replace("ё", "е").strip()
        if "послезавтра" in text:
            return today + timedelta(days=2)
        if "завтра" in text:
            return today + timedelta(days=1)
        if "сегодня" in text:
            return today

        parsed: date | None = None
        explicit_year = True

        # 2026-11-04, 2026/11/04, 2026.11.04
        match = re.search(r"(?<!\d)(\d{4})[-/.](\d{1,2})[-/.](\d{1,2})(?!\d)", text)
        if match:
            try:
                parsed = date(int(match.group(1)), int(match.group(2)), int(match.group(3)))
            except ValueError:
                parsed = None
        else:
            # 04.11.2026 / 04.11 / 4 ноября 2026 / 4 ноября
            match = re.search(
                r"(?<!\d)(\d{1,2})[./](\d{1,2})(?:[./](\d{2,4}))?(?!\d)", text
            )
            if match:
                day, month = int(match.group(1)), int(match.group(2))
                raw_year = match.group(3)
                year = int(raw_year) if raw_year else today.year
                if raw_year and year < 100:
                    year += 2000
                explicit_year = bool(raw_year)
                try:
                    parsed = date(year, month, day)
                except ValueError:
                    parsed = None
            else:
                match = re.search(
                    r"(?<!\d)(\d{1,2})\s+(" + "|".join(RU_MONTHS) + r")(?:\s+(\d{4}))?(?!\d)",
                    text,
                )
                if match:
                    day = int(match.group(1))
                    month = RU_MONTHS[match.group(2)]
                    raw_year = match.group(3)
                    year = int(raw_year) if raw_year else today.year
                    explicit_year = bool(raw_year)
                    try:
                        parsed = date(year, month, day)
                    except ValueError:
                        parsed = None

        if parsed is None:
            return None
        # Без года дата в прошлом означает ближайший будущий год.
        if not explicit_year and parsed < today:
            try:
                parsed = parsed.replace(year=parsed.year + 1)
            except ValueError:
                return None
        if parsed < today - timedelta(days=1):
            return None
        return parsed

    @staticmethod
    def _parse_time(value: Any) -> str | None:
        if not value:
            return None
        match = re.search(r"(\d{1,2})(?::(\d{2}))?", str(value))
        if not match:
            return None
        hour, minute = int(match.group(1)), int(match.group(2) or 0)
        if 0 <= hour <= 23 and 0 <= minute <= 59:
            return f"{hour:02d}:{minute:02d}"
        return None

    # ------------------------------------------------------------------
    # голос
    # ------------------------------------------------------------------
    async def recognize_image(
        self, image: bytes, mime_type: str = "image/jpeg"
    ) -> str | None:
        """Распознаёт рукописный или печатный список продуктов на фото."""
        if not self.gemini_enabled or self._gemini is None:
            return None
        self._count_call("gemini")
        products = ", ".join(item.name for item in self.catalog.items[:30])
        prompt = (
            "На фото — список продуктов для заказа. Перенеси его в текст: "
            "каждая позиция с новой строки, с количеством и единицей, если они указаны. "
            "Возможные продукты: " + products + ". "
            "Единицы: кг, л, шт, упаковка, пакет, коробка, ведро, мешок. "
            "Верни только текст списка, без пояснений."
        )
        try:
            image_part = genai_types.Part.from_bytes(data=image, mime_type=mime_type)
        except Exception:
            image_part = genai_types.Part(
                inline_data=genai_types.Blob(data=image, mime_type=mime_type)
            )
        for model in self._gemini_models():
            try:
                response = await self._gemini.aio.models.generate_content(
                    model=model,
                    contents=[prompt, image_part],
                    config=genai_types.GenerateContentConfig(
                        temperature=0, max_output_tokens=1200
                    ),
                )
                text = (getattr(response, "text", "") or "").strip()
                if text:
                    self._gemini_success(model)
                    return text
            except Exception as exc:
                if self._gemini_failure(model, exc, "vision"):
                    break
        return None

    async def transcribe_audio(self, audio: bytes, mime_type: str = "audio/ogg") -> str | None:
        if not self.gemini_enabled or self._gemini is None:
            return None
        self._count_call("gemini")
        products = ", ".join(item.name for item in self.catalog.items[:30])
        prompt = (
            "Транскрибируй это голосовое сообщение на русском языке дословно. "
            f"В заказе могут быть продукты: {products}. "
            "Единицы: кг, л, шт, упаковка, пакет, коробка, ведро, мешок. "
            "В нём могут быть числа и даты. "
            "Верни только распознанный текст, без пояснений и кавычек."
        )
        try:
            audio_part = genai_types.Part.from_bytes(data=audio, mime_type=mime_type)
        except Exception:
            audio_part = genai_types.Part(
                inline_data=genai_types.Blob(data=audio, mime_type=mime_type)
            )
        deadline = time.monotonic() + 75
        for model in self._gemini_models():
            remaining = deadline - time.monotonic()
            if remaining <= 1:
                log.warning("Gemini STT: общий лимит 75 с исчерпан — перехожу на Vosk")
                break
            try:
                response = await asyncio.wait_for(
                    self._gemini.aio.models.generate_content(
                        model=model,
                        contents=[prompt, audio_part],
                        config=genai_types.GenerateContentConfig(
                            temperature=0, max_output_tokens=1200
                        ),
                    ),
                    timeout=min(45.0, remaining),
                )
                text = (getattr(response, "text", "") or "").strip()
                if text:
                    self._gemini_success(model)
                    return text
            except Exception as exc:
                if self._gemini_failure(model, exc, "STT"):
                    return None
        return None

    async def close(self) -> None:
        if self._deepseek is not None:
            try:
                await self._deepseek.close()
            except Exception:
                pass
