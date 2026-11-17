"""Оплата USDT в сети TON: ссылки, QR и разбор переводов через TonAPI."""
from __future__ import annotations

import base64
import io
from dataclasses import dataclass
from typing import Any, Iterable

import aiohttp

TONAPI_BASE = "https://tonapi.io/v2"
NANO = 1_000_000  # у USDT 6 знаков после запятой


class TonPayError(RuntimeError):
    """Ошибка обращения к TonAPI."""


def friendly_to_raw(address: str) -> str:
    """Переводит адрес TON (UQ…/EQ… или 0:hex) в raw-формат workchain:hash."""
    address = (address or "").strip().replace(" ", "")
    if not address:
        raise ValueError("пустой адрес")
    if ":" in address:
        workchain, _, body = address.partition(":")
        return f"{int(workchain)}:{body.lower()}"
    padded = address + "=" * (-len(address) % 4)
    data = base64.urlsafe_b64decode(padded)
    if len(data) != 36:
        raise ValueError(f"неверная длина адреса TON: {len(data)}")
    workchain = data[1] - 256 if data[1] > 127 else data[1]
    return f"{workchain}:{data[2:34].hex()}"


def payment_link(wallet: str, jetton: str, amount_nano: int, comment: str) -> str:
    return (
        f"ton://transfer/{wallet}?jetton={jetton}"
        f"&amount={int(amount_nano)}&text={comment}"
    )


def qr_png(text: str) -> bytes:
    import qrcode  # тяжёлая зависимость — грузим только когда нужен QR

    image = qrcode.make(text)
    buffer = io.BytesIO()
    image.save(buffer, format="PNG")
    return buffer.getvalue()


def usdt_to_nano(amount: float) -> int:
    return int(round(float(amount) * NANO))


def nano_to_text(nano: int) -> str:
    return f"{nano / NANO:.2f}"


@dataclass(frozen=True)
class JettonTransfer:
    tx_hash: str
    amount_nano: int
    comment: str
    jetton_raw: str
    recipient_raw: str
    timestamp: int


def _addr(value: Any) -> str:
    if not isinstance(value, str) or not value:
        return ""
    try:
        return friendly_to_raw(value)
    except Exception:
        return value.lower()


def extract_transfers(events: Iterable[dict]) -> list[JettonTransfer]:
    """Достаёт из событий TonAPI входящие переводы жетонов."""
    result: list[JettonTransfer] = []
    for event in events or []:
        if not isinstance(event, dict) or event.get("in_progress"):
            continue
        tx_hash = str(event.get("event_id") or "")
        timestamp = int(event.get("timestamp") or 0)
        for action in event.get("actions") or []:
            if action.get("type") != "JettonTransfer":
                continue
            if action.get("status") not in (None, "ok"):
                continue
            payload = action.get("JettonTransfer") or {}
            try:
                amount_nano = int(payload.get("amount") or 0)
            except (TypeError, ValueError):
                continue
            jetton = payload.get("jetton") or {}
            recipient = payload.get("recipient") or payload.get("destination") or {}
            result.append(
                JettonTransfer(
                    tx_hash=tx_hash or str(event.get("ext_msg_hash") or ""),
                    amount_nano=amount_nano,
                    comment=str(payload.get("comment") or ""),
                    jetton_raw=_addr(jetton.get("address") or jetton.get("jetton_address")),
                    recipient_raw=_addr(recipient.get("address")),
                    timestamp=timestamp,
                )
            )
    return result


def match_payment(
    transfers: Iterable[JettonTransfer],
    *,
    comment: str,
    required_nano: int,
    wallet_raw: str,
    jetton_raw: str,
) -> JettonTransfer | None:
    """Ищет перевод, который закрывает конкретный заказ."""
    for transfer in transfers:
        if transfer.comment.strip() != comment:
            continue
        if transfer.jetton_raw != jetton_raw:
            continue
        if wallet_raw and transfer.recipient_raw != wallet_raw:
            continue
        if transfer.amount_nano < required_nano:
            continue
        return transfer
    return None


class TonPay:
    """Минимальный клиент TonAPI для чтения входящих событий кошелька."""

    def __init__(self, api_key: str, wallet: str, jetton: str, *, timeout: float = 20.0):
        self.api_key = (api_key or "").strip()
        self.wallet = (wallet or "").strip()
        self.jetton = (jetton or "").strip()
        self.wallet_raw = friendly_to_raw(self.wallet) if self.wallet else ""
        self.jetton_raw = friendly_to_raw(self.jetton) if self.jetton else ""
        self.timeout = timeout

    @property
    def configured(self) -> bool:
        return bool(self.api_key and self.wallet and self.jetton)

    async def transfers(self, limit: int = 40) -> list[JettonTransfer]:
        url = f"{TONAPI_BASE}/accounts/{self.wallet_raw}/events?limit={int(limit)}"
        headers = {"Authorization": f"Bearer {self.api_key}"}
        timeout = aiohttp.ClientTimeout(total=self.timeout)
        try:
            async with aiohttp.ClientSession(timeout=timeout) as session:
                async with session.get(url, headers=headers) as response:
                    text = await response.text()
                    if response.status == 429:
                        raise TonPayError("TonAPI: слишком много запросов")
                    if response.status >= 400:
                        raise TonPayError(f"TonAPI HTTP {response.status}: {text[:200]}")
                    import json

                    data = json.loads(text or "{}")
        except aiohttp.ClientError as exc:
            raise TonPayError(f"TonAPI сеть: {exc}") from exc
        return extract_transfers(data.get("events") or [])
