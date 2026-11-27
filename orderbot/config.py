"""Конфигурация бота из .env."""
from __future__ import annotations

import os
from dataclasses import dataclass
from pathlib import Path
from zoneinfo import ZoneInfo

from dotenv import load_dotenv

BASE_DIR = Path(__file__).resolve().parent.parent
load_dotenv(BASE_DIR / ".env")


def _str(name: str, default: str = "") -> str:
    return os.getenv(name, default).strip()


def _int(name: str, default: int | None = None) -> int | None:
    raw = os.getenv(name, "").strip()
    if not raw:
        return default
    try:
        return int(raw)
    except ValueError:
        return default


def _float(name: str, default: float) -> float:
    raw = os.getenv(name, "").strip()
    if not raw:
        return default
    try:
        return float(raw)
    except ValueError:
        return default


def _ids(name: str) -> tuple[int, ...]:
    raw = os.getenv(name, "").strip()
    result: list[int] = []
    for part in raw.replace(";", ",").replace(" ", ",").split(","):
        part = part.strip()
        if not part:
            continue
        try:
            result.append(int(part))
        except ValueError:
            continue
    return tuple(result)


def _path(name: str, default: str) -> Path:
    raw = os.getenv(name, default).strip() or default
    path = Path(raw)
    return path if path.is_absolute() else BASE_DIR / path


@dataclass(frozen=True)
class Settings:
    bot_token: str
    manager_chat_id: int | None
    courier_chat_id: int | None
    ai_provider: str
    deepseek_api_key: str
    deepseek_base_url: str
    deepseek_model: str
    gemini_api_key: str
    gemini_model: str
    catalog_path: Path
    db_path: Path
    data_dir: Path
    vosk_model_path: Path | None
    timezone: ZoneInfo
    fuzzy_accept: float
    fuzzy_candidate: float
    max_voice_mb: int
    log_level: str
    timezone_name: str = "Asia/Tashkent"
    allowed_chat_ids: tuple[int, ...] = ()
    database_url: str = ""
    tonapi_key: str = ""
    ton_wallet: str = ""
    usdt_jetton: str = ""
    ton_window_minutes: int = 15
    ton_poll_seconds: int = 10
    alias_auto_global: bool = False
    min_order_usd: float = 0.0
    heartbeat_url: str = ""
    local_first: bool = True
    shadow_prompt_pct: float = 0.0
    llm_daily_budget: int = 0
    llm_user_hourly: int = 0
    llm_cost_per_call: float = 0.002
    google_sa_file: str = ""
    sheets_id: str = ""
    owner_web_token: str = ""

    @property
    def ton_enabled(self) -> bool:
        return bool(self.tonapi_key and self.ton_wallet and self.usdt_jetton)


def load_settings() -> Settings:
    timezone_name = _str("TIMEZONE", "Asia/Tashkent") or "Asia/Tashkent"
    try:
        tz = ZoneInfo(timezone_name)
    except Exception:
        timezone_name = "Asia/Tashkent"
        tz = ZoneInfo(timezone_name)

    vosk_path = _path("VOSK_MODEL_PATH", "models/vosk-model-small-ru-0.22")
    if not vosk_path.exists():
        vosk_path = None  # type: ignore[assignment]

    return Settings(
        bot_token=_str("BOT_TOKEN"),
        manager_chat_id=_int("MANAGER_CHAT_ID"),
        courier_chat_id=_int("COURIER_CHAT_ID"),
        ai_provider=(_str("AI_PROVIDER", "auto") or "auto").lower(),
        deepseek_api_key=_str("DEEPSEEK_API_KEY"),
        deepseek_base_url=_str("DEEPSEEK_BASE_URL", "https://api.deepseek.com") or "https://api.deepseek.com",
        deepseek_model=_str("DEEPSEEK_MODEL", "deepseek-chat") or "deepseek-chat",
        gemini_api_key=_str("GEMINI_API_KEY"),
        gemini_model=_str("GEMINI_MODEL", "gemini-3.8-flash") or "gemini-3.8-flash",
        catalog_path=_path("CATALOG_PATH", "catalog.xlsx"),
        db_path=_path("DB_PATH", "data/orderbot.db"),
        data_dir=_path("DATA_DIR", "data"),
        vosk_model_path=vosk_path,
        timezone=tz,
        fuzzy_accept=_float("FUZZY_ACCEPT", 88.0),
        fuzzy_candidate=_float("FUZZY_CANDIDATE", 55.0),
        max_voice_mb=_int("MAX_VOICE_MB", 20) or 20,
        log_level=_str("LOG_LEVEL", "INFO").upper() or "INFO",
        timezone_name=timezone_name,
        allowed_chat_ids=_ids("ALLOWED_CHAT_IDS"),
        database_url=_str("DATABASE_URL"),
        tonapi_key=_str("TONAPI_KEY"),
        ton_wallet=_str("WALLET_ADDRESS"),
        usdt_jetton=_str("USDT_JETTON_MASTER"),
        ton_window_minutes=max(1, _int("TON_WINDOW_MINUTES", 15) or 15),
        ton_poll_seconds=max(3, _int("TON_POLL_SECONDS", 10) or 10),
        alias_auto_global=_str("ALIAS_AUTO_GLOBAL") in ("1", "true", "yes", "on"),
        min_order_usd=_float("MIN_ORDER_USD", 0.0),
        heartbeat_url=_str("HEARTBEAT_URL"),
        local_first=_str("LOCAL_FIRST", "1") not in ("0", "false", "no", "off"),
        shadow_prompt_pct=max(0.0, min(100.0, _float("SHADOW_PROMPT_PCT", 0.0))),
        llm_daily_budget=_int("LLM_DAILY_BUDGET", 0) or 0,
        llm_user_hourly=_int("LLM_USER_HOURLY", 0) or 0,
        llm_cost_per_call=_float("LLM_COST_PER_CALL", 0.002),
        google_sa_file=_str("GOOGLE_SA_FILE"),
        sheets_id=_str("SHEETS_ID"),
        owner_web_token=_str("OWNER_WEB_TOKEN"),
    )
