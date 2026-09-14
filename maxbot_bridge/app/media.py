# -*- coding: utf-8 -*-
"""Скачивание вложений из MAX.

Ключевая правка против maxbot_v7 (где фото не работало): правильный API pymax:
- фото: PhotoAttachment.base_url → скачать по URL → base64;
- файлы: client.get_file_by_id(chat_id, message_id, file_id) → info.url → base64;
- прочие вложения (видео/голос/стикеры): пробуем тот же get_file_by_id по
  типовым полям, при неудаче честно фиксируем метаданные без содержимого.
"""

from __future__ import annotations

import base64
import logging
from typing import Any

import httpx

log = logging.getLogger("maxbot.media")

try:  # типы pymax могут переехать между версиями — пробуем канонический путь
    from pymax.types.domain import (  # type: ignore
        FileAttachment,
        PhotoAttachment,
    )
except Exception:  # pragma: no cover — резерв на случай изменения структуры
    PhotoAttachment = None  # type: ignore
    FileAttachment = None  # type: ignore


def _is_photo(attach: Any) -> bool:
    if PhotoAttachment is not None and isinstance(attach, PhotoAttachment):
        return True
    return str(getattr(attach, "type", "")).lower() in {"photo", "image"}


def _is_file(attach: Any) -> bool:
    if FileAttachment is not None and isinstance(attach, FileAttachment):
        return True
    return str(getattr(attach, "type", "")).lower() in {"file", "document"}


async def _download_b64(url: str, max_bytes: int) -> str | None:
    """Скачать по URL и вернуть base64; большие файлы и ошибки — None."""
    if not url or not str(url).startswith("http"):
        log.warning("неверный URL вложения: %r", url[:80] if url else url)
        return None
    try:
        async with httpx.AsyncClient(timeout=60, follow_redirects=True) as http:
            async with http.stream("GET", str(url)) as resp:
                if resp.status_code >= 400:
                    log.warning("скачивание вложения: HTTP %s", resp.status_code)
                    return None
                buf = bytearray()
                async for chunk in resp.aiter_bytes(64 * 1024):
                    buf.extend(chunk)
                    if len(buf) > max_bytes:
                        log.warning(
                            "вложение больше лимита (%d байт) — пропущено", max_bytes
                        )
                        return None
        return base64.b64encode(bytes(buf)).decode("ascii")
    except Exception as exc:
        log.error("ошибка скачивания вложения: %s", exc)
        return None


async def extract_files(client: Any, message: Any, max_bytes: int) -> list[dict]:
    """Извлечь все вложения сообщения в список {type, name?, base64?}."""
    files: list[dict] = []
    chat_id = getattr(message, "chat_id", None)
    message_id = getattr(message, "id", None)
    attaches = getattr(message, "attaches", None) or []

    for attach in attaches:
        attach_type = str(getattr(attach, "type", "unknown") or "unknown")

        if _is_photo(attach):
            url = getattr(attach, "base_url", None)
            data = await _download_b64(url, max_bytes) if url else None
            files.append(
                {
                    "type": "photo",
                    "photo_id": getattr(attach, "photo_id", None),
                    "base64": data,
                }
            )
            continue

        # Файл/видео/голос: нужен временный URL через get_file_by_id
        file_id = None
        for attr in ("file_id", "video_id", "voice_id", "audio_id"):
            file_id = getattr(attach, attr, None)
            if file_id is not None:
                break

        url = None
        if file_id is not None and chat_id is not None and message_id is not None:
            try:
                info = await client.get_file_by_id(
                    chat_id=chat_id, message_id=message_id, file_id=file_id
                )
                url = getattr(info, "url", None) if info else None
            except Exception as exc:
                log.warning("get_file_by_id(%s) не удался: %s", file_id, exc)

        data = await _download_b64(url, max_bytes) if url else None
        entry: dict = {"type": attach_type, "id": file_id, "base64": data}
        name = getattr(attach, "name", None) or getattr(attach, "filename", None)
        if name:
            entry["name"] = str(name)
        # Транскрипция голосовых/аудио через STT-адаптер (Фаза 8.2):
        # base64 → OpenAI-совместимый /v1/audio/transcriptions → {'text': '...'}
        if attach_type.lower() in {"voice", "audio"} and data:
            text = await _transcribe_b64(data)
            if text:
                entry["stt_text"] = text
                log.info("STT: голосовое %s → %d символов", file_id, len(text))
            else:
                log.warning("STT не дал текст для %s", file_id)
        files.append(entry)

    return files


async def _transcribe_b64(b64_data: str, language: str = "ru") -> str | None:
    """Отправить base64-аудио в STT-адаптер (OpenAI-совместимый /v1/audio/transcriptions) → текст."""
    try:
        from .settings import load_settings

        url = load_settings().stt_adapter_url
    except Exception:
        url = None
    if not url:
        log.debug("STT-адаптер не настроен (stt_adapter_url пуст)")
        return None
    try:
        boundary = "----maxbot-stt"
        mp = (
            f"--{boundary}\r\n"
            f"Content-Disposition: form-data; name=\"file\"; filename=\"voice.ogg\"\r\n"
            f"Content-Type: audio/ogg\r\n\r\n"
        ).encode() + base64.b64decode(b64_data) + (
            f"\r\n--{boundary}\r\n"
            f"Content-Disposition: form-data; name=\"language\"\r\n\r\n{language}\r\n"
            f"--{boundary}--\r\n"
        ).encode()
        async with httpx.AsyncClient(timeout=120) as http:
            resp = await http.post(
                url,
                content=mp,
                headers={"Content-Type": f"multipart/form-data; boundary={boundary}"},
            )
        if resp.status_code == 200:
            return str(resp.json().get("text", "")).strip() or None
        log.warning("STT-адаптер: HTTP %s", resp.status_code)
    except Exception as exc:
        log.error("STT-адаптер: %s", exc)
    return None
