# -*- coding: utf-8 -*-
"""Настройки MaxBot Bridge — читаются из /data/options.json (опции аддона HA)."""

from __future__ import annotations

import json
import re
from pathlib import Path

DATA_DIR = Path("/data")
OPTIONS_FILE = DATA_DIR / "options.json"

VALID_LOG_LEVELS = {"INFO", "DEBUG", "WARNING", "ERROR"}


def normalize_phone(raw: str) -> str:
    """Нормализовать телефон в формат +7XXXXXXXXXX.

    Принимает: +79967857133, 89967857133, +7 (996) 785-71-33 и т.д.
    Возвращает: +79967857133 или пустую строку при невалидном вводе.
    """
    digits = re.sub(r"[^\d]", "", raw)
    if not digits:
        return ""
    # 8XXXXXXXXXX (11 цифр, начинается на 8) → +7 + 10 цифр
    if len(digits) == 11 and digits.startswith("8"):
        return "+7" + digits[1:]
    # 7XXXXXXXXXX (11 цифр, начинается на 7) → + + всё
    if len(digits) == 11 and digits.startswith("7"):
        return "+" + digits
    # 10 цифр без префикса → +7 + 10 цифр
    if len(digits) == 10:
        return "+7" + digits
    # Уже с + — возвращаем как есть (с +)
    if raw.strip().startswith("+") and len(digits) >= 10:
        return "+" + digits
    # Фолбэк: возвращаем цифры с +
    if len(digits) >= 10:
        return "+" + digits[-10:] if len(digits) > 10 else "+" + digits
    return ""


class Settings:
    """Плоская обёртка над options.json с безопасными значениями по умолчанию."""

    def __init__(self, raw: dict) -> None:
        self.max_phone: str = normalize_phone((raw.get("max_phone") or "").strip())
        self.webhook_url: str = (raw.get("webhook_url") or "").strip().rstrip("/")
        self.webhook_token: str = (raw.get("webhook_token") or "").strip()
        # Проверка TLS-сертификата webhook (false для self-signed в LAN)
        self.webhook_verify_ssl: bool = bool(raw.get("webhook_verify_ssl", False))
        # 0 = чат утверждения не задан
        self.approval_chat_id: int | None = (
            int(raw.get("approval_chat_id")) if raw.get("approval_chat_id") else None
        )
        self.ignore_chats: set[int] = {
            int(c) for c in (raw.get("ignore_chats") or [])
        }
        self.auto_reply: bool = bool(raw.get("auto_reply", False))
        self.max_2fa_password: str = (raw.get("max_2fa_password") or "").strip()
        try:
            self.media_max_bytes: int = int(float(raw.get("media_max_mb", 20)) * 1024 * 1024)
        except (TypeError, ValueError):
            self.media_max_bytes = 20 * 1024 * 1024
        self.log_level: str = str(raw.get("log_level", "INFO")).upper()
        if self.log_level not in VALID_LOG_LEVELS:
            self.log_level = "INFO"
        # Способ первого входа: "qr" (WebClient, без SMS) или "sms" (Client, TCP)
        self.auth_method: str = str(raw.get("auth_method", "qr")).lower()
        if self.auth_method not in {"qr", "sms"}:
            self.auth_method = "qr"
        try:
            self.http_port: int = int(raw.get("http_port", 8099))
        except (TypeError, ValueError):
            self.http_port = 8099
        # Сессия pymax живёт в /data/cache (персистентно, попадает в бэкапы HA)
        self.work_dir: str = str(DATA_DIR / "cache")

    @property
    def webhook_ready(self) -> bool:
        return bool(self.webhook_url)


def load_settings() -> Settings:
    """Прочитать опции аддона; при отсутствии файла — пустые значения."""
    raw: dict = {}
    if OPTIONS_FILE.exists():
        try:
            raw = json.loads(OPTIONS_FILE.read_text(encoding="utf-8"))
        except Exception:
            raw = {}
    return Settings(raw)
