"""Распознавание голоса: Gemini (онлайн) с офлайн-fallback на Vosk."""
from __future__ import annotations

import asyncio
import json
import logging
import time
import re
import secrets
import wave
from dataclasses import dataclass
from pathlib import Path

from .ai import AIClient
from .config import Settings

log = logging.getLogger(__name__)


@dataclass
class Transcription:
    text: str
    provider: str
    error: str = ""


class Transcriber:
    def __init__(self, settings: Settings, ai: AIClient):
        self.settings = settings
        self.ai = ai
        self._vosk_model = None
        self._vosk_lock = asyncio.Lock()
        self._vosk_used_at = 0.0

    async def transcribe(self, audio_path: Path, mime_type: str = "audio/ogg") -> Transcription:
        errors: list[str] = []

        if self.ai.gemini_enabled:
            try:
                data = audio_path.read_bytes()
                if len(data) <= self.settings.max_voice_mb * 1024 * 1024:
                    text = await self.ai.transcribe_audio(data, mime_type=mime_type)
                    if text:
                        return Transcription(self._clean(text), "gemini")
                    errors.append("Gemini не вернул текст")
                else:
                    errors.append("файл слишком большой для Gemini")
            except Exception as exc:
                log.warning("Gemini transcription error: %s", exc)
                errors.append("Gemini недоступен")
        else:
            errors.append("Gemini не настроен (ключ отсутствует или отклонён)")

        if self.settings.vosk_model_path is not None:
            wav_path = self.settings.data_dir / "tmp" / f"{secrets.token_hex(6)}.wav"
            wav_path.parent.mkdir(parents=True, exist_ok=True)
            try:
                await self._to_wav(audio_path, wav_path)
                text = await self._recognize_vosk(wav_path)
                if text:
                    return Transcription(self._clean(text), "vosk")
                errors.append("офлайн-модель не разобрала речь")
            except Exception as exc:
                log.warning("Vosk error: %s", exc)
                errors.append("офлайн-распознавание недоступно")
            finally:
                wav_path.unlink(missing_ok=True)
        else:
            errors.append("офлайн-модель Vosk не установлена")

        return Transcription("", "none", "; ".join(errors))

    async def _to_wav(self, source: Path, destination: Path) -> None:
        process = await asyncio.create_subprocess_exec(
            "ffmpeg", "-y", "-hide_banner", "-loglevel", "error",
            "-i", str(source), "-ar", "16000", "-ac", "1", "-c:a", "pcm_s16le",
            str(destination),
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
        )
        try:
            _, stderr = await asyncio.wait_for(process.communicate(), timeout=120)
        except TimeoutError:
            process.kill()
            raise RuntimeError("ffmpeg timeout") from None
        if process.returncode != 0:
            raise RuntimeError(stderr.decode("utf-8", "replace")[:300])

    async def _get_vosk_model(self):
        if self._vosk_model is not None:
            return self._vosk_model
        async with self._vosk_lock:
            if self._vosk_model is None:
                self._vosk_model = await asyncio.to_thread(self._load_vosk_model)
                self._vosk_used_at = time.time()
        return self._vosk_model

    def _load_vosk_model(self):
        import vosk

        vosk.SetLogLevel(-1)
        model_path = str(self.settings.vosk_model_path)
        log.info("Загружаю офлайн-модель Vosk: %s", model_path)
        return vosk.Model(model_path)

    async def _recognize_vosk(self, wav_path: Path) -> str:
        model = await self._get_vosk_model()
        self._vosk_used_at = time.time()
        return await asyncio.to_thread(self._recognize_sync, model, wav_path)

    async def release_idle(self, minutes: int = 10) -> bool:
        """Выгружает офлайн-модель, если ею давно не пользовались (A5).

        Модель Vosk занимает сотни мегабайт; на сервере с 2 ГБ памяти
        держать её постоянно незачем — загрузим снова при следующем голосовом.
        """
        if minutes <= 0 or self._vosk_model is None:
            return False
        idle = time.time() - float(self._vosk_used_at or 0)
        if idle < minutes * 60:
            return False
        async with self._vosk_lock:
            if self._vosk_model is None:
                return False
            self._vosk_model = None
            self._vosk_used_at = 0.0
        log.info("Офлайн-модель Vosk выгружена после %s мин простоя", int(idle // 60))
        return True

    @staticmethod
    def _recognize_sync(model, wav_path: Path) -> str:
        import vosk

        with wave.open(str(wav_path), "rb") as stream:
            if stream.getnchannels() != 1 or stream.getsampwidth() != 2:
                raise RuntimeError("неподходящий WAV")
            recognizer = vosk.KaldiRecognizer(model, stream.getframerate())
            parts: list[str] = []
            while True:
                chunk = stream.readframes(8000)
                if not chunk:
                    break
                if recognizer.AcceptWaveform(chunk):
                    parts.append(json.loads(recognizer.Result()).get("text", ""))
            parts.append(json.loads(recognizer.FinalResult()).get("text", ""))
        return " ".join(part for part in parts if part).strip()

    @staticmethod
    def _clean(text: str) -> str:
        text = re.sub(r"\s+", " ", text).strip()
        return text.strip(" .,\"'«»")
