# -*- coding: utf-8 -*-
"""Хранилище имён людей и чатов (people.json / chats.json).

Закрывает «дыру V8»: userbot видит ID вместо имён. Логика:
1. Имя пытаемся извлечь из объектов pymax (sender / chat.title / get_user).
2. Найденное автоматически сохраняем в JSON-кеш (автообучение).
3. Если имени нет нигде — один раз задаём вопрос в чат утверждения
   («ID X: кто это?»); ответ в формате «ID X = Имя» фиксируется навсегда.
"""

from __future__ import annotations

import json
import logging
import re
import time
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

log = logging.getLogger("maxbot.names")

RE_LEARN_PERSON = re.compile(r"^(?:id|ид)\s+(-?\d+)\s*=\s*(.+)$", re.IGNORECASE)
RE_LEARN_CHAT = re.compile(r"^(?:чат|chat)\s+(-?\d+)\s*=\s*(.+)$", re.IGNORECASE)


def _now() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


def _display(obj: Any) -> str:
    """Аккуратно превратить произвольное значение в непустую строку."""
    if obj is None:
        return ""
    if isinstance(obj, str):
        return obj.strip()
    if isinstance(obj, (int, float)):
        return str(obj)
    if isinstance(obj, (list, tuple)):
        for item in obj:
            text = _display(item)
            if text:
                return text
        return ""
    # Объект: пробуем типовые атрибуты
    for attr in ("name", "first_name", "last_name", "username", "nick", "title", "value", "text"):
        val = getattr(obj, attr, None)
        if isinstance(val, str) and val.strip():
            return val.strip()
    return ""


class NameStore:
    """JSON-кеш соответствий ID → имя, с автообучением и вопросами."""

    def __init__(self, data_dir: Any) -> None:
        data_dir = Path(data_dir)
        self.people_file = data_dir / "people.json"
        self.chats_file = data_dir / "chats.json"
        self.people: dict[str, dict] = {}
        self.chats: dict[str, dict] = {}
        # Чтобы не спрашивать повторно в рамках сессии
        self.asked_people: set[str] = set()
        self.asked_chats: set[str] = set()
        self._load()

    # ------------------------------------------------------------------ #
    # Персистентность
    # ------------------------------------------------------------------ #
    def _load(self) -> None:
        for path, store in ((self.people_file, "people"), (self.chats_file, "chats")):
            target = getattr(self, store)
            if path.exists():
                try:
                    target.update(json.loads(path.read_text(encoding="utf-8")))
                    log.info("%s загружен: %d записей", path.name, len(target))
                except Exception as exc:
                    log.warning("не удалось прочитать %s: %s", path, exc)

    def _save(self, path: Any, data: dict) -> None:
        try:
            path.parent.mkdir(parents=True, exist_ok=True)
            tmp = path.with_suffix(".json.tmp")
            tmp.write_text(json.dumps(data, ensure_ascii=False, indent=2), encoding="utf-8")
            tmp.replace(path)
        except Exception as exc:
            log.error("ошибка сохранения %s: %s", path, exc)

    # ------------------------------------------------------------------ #
    # Чтение / запись
    # ------------------------------------------------------------------ #
    def person(self, user_id: Any) -> str | None:
        entry = self.people.get(str(user_id))
        return entry["name"] if entry else None

    def chat(self, chat_id: Any) -> str | None:
        entry = self.chats.get(str(chat_id))
        return entry["title"] if entry else None

    def learn_person(self, user_id: Any, name: str, source: str = "manual") -> None:
        key = str(user_id)
        if not name or not name.strip():
            return
        self.people[key] = {
            "name": name.strip(),
            "source": source,
            "updated": _now(),
        }
        self.asked_people.discard(key)
        self._save(self.people_file, self.people)
        log.info("запомнил человека %s = «%s» (%s)", key, name.strip(), source)

    def learn_chat(self, chat_id: Any, title: str, source: str = "manual") -> None:
        key = str(chat_id)
        if not title or not title.strip():
            return
        self.chats[key] = {
            "title": title.strip(),
            "source": source,
            "updated": _now(),
        }
        self.asked_chats.discard(key)
        self._save(self.chats_file, self.chats)
        log.info("запомнил чат %s = «%s» (%s)", key, title.strip(), source)

    # ------------------------------------------------------------------ #
    # Автоизвлечение из объектов pymax
    # ------------------------------------------------------------------ #
    @staticmethod
    def user_display_name(user: Any) -> str | None:
        """Полное имя из pymax User (user.names — список Name)."""
        if user is None:
            return None
        names = getattr(user, "names", None) or []
        for entry in names:
            first = _display(getattr(entry, "first_name", None))
            last = _display(getattr(entry, "last_name", None))
            full = _display(getattr(entry, "name", None))
            text = " ".join(p for p in (first, last) if p) or full
            if text:
                return text
        # Резервные поля
        for attr in ("first_name", "last_name", "username", "nick"):
            text = _display(getattr(user, attr, None))
            if text:
                return text
        return None

    # ------------------------------------------------------------------ #
    # Команды обучения («ID 123 = Иван», «ЧАТ -726... = 354 цех»)
    # ------------------------------------------------------------------ #
    def parse_learn_command(self, text: str) -> dict | None:
        text = (text or "").strip()
        m = RE_LEARN_PERSON.match(text)
        if m:
            return {"kind": "person", "id": int(m.group(1)), "name": m.group(2).strip()}
        m = RE_LEARN_CHAT.match(text)
        if m:
            return {"kind": "chat", "id": int(m.group(1)), "name": m.group(2).strip()}
        return None

    def export(self) -> dict:
        return {"people": self.people, "chats": self.chats}


def ts() -> str:
    """ISO-время для конвертов."""
    return datetime.now(timezone.utc).isoformat(timespec="seconds")
