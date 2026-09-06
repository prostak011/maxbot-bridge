# -*- coding: utf-8 -*-
"""Настройки MaxBot Bridge — читаются из /data/options.json (опции аддона HA)."""

from __future__ import annotations

import json
from pathlib import Path

DATA_DIR = Path("/data")
OPTIONS_FILE = DATA_DIR / "options.json"

VALID_LOG_LEVELS = {"INFO", "DEBUG", "WARNING", "ERROR"}


class Settings:
    """Плоская обёртка над options.json с безопасными значениями по умолчанию."""

    def __init__(self, raw: dict) -> None:
        self.max_phone: str = (raw.get("max_phone") or "").strip()
        self.webhook_url: str = (raw.get("webhook_url") or "").strip().rstrip("/")
        self.webhook_token: str = (raw.get("webhook_token") or "").strip()
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
