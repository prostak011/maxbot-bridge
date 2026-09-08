# -*- coding: utf-8 -*-
"""MaxBot Bridge — конвейер сообщений MAX → вебхук (OpenClaw/n8n) → MAX.

Каждое сообщение из любого чата превращается во «входной конверт»
(см. АРХИТЕКТУРА_ВОРОНКА.md): source/chat_name/from_name/from_id/text/files[]/
timestamp — и уходит POST-запросом на WEBHOOK_URL с заголовком X-MaxBot-Token.

Названия чатов и имена участников резолвятся так:
  1) объекты pymax (chat.title, sender),  2) NameStore (people/chats.json),
  3) вопрос владельцу в чат утверждения (режим обучения).

Чат утверждения — управляющий: в нём парсятся команды обучения
«ID 123 = Иван» / «ЧАТ -726... = 354 цех»; остальные сообщения из него
уходят в вебхук как type=approval.
"""

from __future__ import annotations

import asyncio
import logging
from typing import Any

import httpx

from .media import extract_files
from .names import NameStore, ts

log = logging.getLogger("maxbot.bridge")


class Bridge:
    def __init__(self, settings: Any, names: NameStore) -> None:
        self.settings = settings
        self.names = names
        self.client: Any = None
        self.me: Any = None
        self.connected: bool = False

    # ------------------------------------------------------------------ #
    # Резолвинг чата
    # ------------------------------------------------------------------ #
    async def resolve_chat(self, client: Any, chat_id: Any) -> str | None:
        """Название чата: pymax-кеш → get_chat() → chats.json → вопрос."""
        if chat_id is None:
            return None
        # 1. Локальный кеш чатов клиента
        for chat in (getattr(client, "chats", None) or []):
            if getattr(chat, "id", None) == chat_id:
                title = getattr(chat, "title", None)
                if title:
                    self.names.learn_chat(chat_id, str(title), source="pymax")
                    return str(title)
        # 2. JSON-кеш
        cached = self.names.chat(chat_id)
        if cached:
            return cached
        # 3. Запрос к серверу
        try:
            chat = await client.get_chat(chat_id)
            title = getattr(chat, "title", None)
            if title:
                self.names.learn_chat(chat_id, str(title), source="pymax")
                return str(title)
        except Exception as exc:
            log.debug("get_chat(%s): %s", chat_id, exc)
        return None

    # ------------------------------------------------------------------ #
    # Резолвинг отправителя
    # ------------------------------------------------------------------ #
    async def resolve_sender(self, client: Any, message: Any) -> tuple[int | None, str | None]:
        """В pymax Message.sender — это int (user_id), имя догружаем get_user()."""
        user_id = getattr(message, "sender", None) or getattr(message, "sender_id", None)
        if user_id is None:
            return None, None
        # 1. JSON-кеш
        name = self.names.person(user_id)
        if name:
            return user_id, name
        # 2. Догрузка пользователя с сервера: user.names — список Name
        try:
            user = await client.get_user(int(user_id))
            if user is not None:
                name = NameStore.user_display_name(user)
                if name:
                    self.names.learn_person(user_id, name, source="pymax")
                    return user_id, name
        except Exception as exc:
            log.debug("get_user(%s): %s", user_id, exc)
        return user_id, None

    # ------------------------------------------------------------------ #
    # Режим обучения: вопрос владельцу
    # ------------------------------------------------------------------ #
    async def ask_unknown(self, kind: str, uid: Any, context: str) -> None:
        approval = self.settings.approval_chat_id
        if approval is None:
            return
        key = f"{kind}:{uid}"
        asked = (
            self.names.asked_people if kind == "person" else self.names.asked_chats
        )
        if key in asked:
            return
        asked.add(key)
        prefix = "ID" if kind == "person" else "ЧАТ"
        await self.send_text(
            approval,
            f"❓ Неизвестный {('участник' if kind == 'person' else 'чат')} {prefix} {uid} "
            f"({context}).\nОтветьте: {prefix} {uid} = Имя",
        )

    # ------------------------------------------------------------------ #
    # Отправка в MAX
    # ------------------------------------------------------------------ #
    async def send_text(self, chat_id: int, text: str, reply_to: int | None = None) -> bool:
        client = self.client
        if client is None:
            log.error("клиент не инициализирован — отправка невозможна")
            return False
        try:
            kwargs: dict = {"chat_id": int(chat_id), "text": text}
            if reply_to is not None:
                kwargs["reply_to"] = int(reply_to)
            await client.send_message(**kwargs)
            return True
        except TypeError as first_err:
            # некоторые версии pymax: send_message(chat_id, text)
            log.debug("send_message(**kwargs) TypeError (%s), пробуем позиционные аргументы", first_err)
            try:
                await client.send_message(int(chat_id), text)
                return True
            except Exception as exc:
                log.error("ошибка send_message (фолбэк): %s [первая ошибка: %s]", exc, first_err)
                return False
        except Exception as exc:
            log.error("ошибка send_text(%s): %s", chat_id, exc)
            return False

    # ------------------------------------------------------------------ #
    # Вебхук
    # ------------------------------------------------------------------ #
    async def post_webhook(self, payload: dict) -> dict | None:
        if not self.settings.webhook_ready:
            log.debug("webhook_url не задан — конверт не отправлен: %s", payload.get("type"))
            return None
        headers = {"Content-Type": "application/json"}
        if self.settings.webhook_token:
            headers["Authorization"] = f"Bearer {self.settings.webhook_token}"
        try:
            async with httpx.AsyncClient(
                timeout=120, verify=self.settings.webhook_verify_ssl
            ) as http:
                resp = await http.post(
                    self.settings.webhook_url, json=payload, headers=headers
                )
            if resp.status_code >= 400:
                log.error("webhook HTTP %s: %s", resp.status_code, resp.text[:300])
                return None
            try:
                return resp.json()
            except Exception:
                return {"text": resp.text}
        except Exception as exc:
            log.error("ошибка webhook: %s", exc)
            return None

    # ------------------------------------------------------------------ #
    # Главный обработчик
    # ------------------------------------------------------------------ #
    async def handle_message(self, message: Any, client: Any) -> None:
        try:
            self.client = client
            if getattr(client, "me", None) is not None:
                self.me = client.me

            # Собственные сообщения игнорируем (не зацикливаемся)
            my_id = None
            me = getattr(client, "me", None)
            contact = getattr(me, "contact", None) if me else None
            my_id = getattr(contact, "id", None) if contact else getattr(me, "id", None)
            sender_uid = getattr(message, "sender", None)
            if my_id is not None and sender_uid == my_id:
                return

            chat_id = getattr(message, "chat_id", None)
            if chat_id is None:
                return
            if int(chat_id) in self.settings.ignore_chats:
                return

            text = (getattr(message, "text", "") or "").strip()
            chat_name = await self.resolve_chat(client, chat_id)
            user_id, user_name = await self.resolve_sender(client, message)
            context = f"сообщение в чате {chat_name or chat_id}"

            # --- Чат утверждения: команды обучения и служебные сообщения ---
            is_approval = (
                self.settings.approval_chat_id is not None
                and int(chat_id) == int(self.settings.approval_chat_id)
            )
            if is_approval and text:
                learn = self.names.parse_learn_command(text)
                if learn:
                    if learn["kind"] == "person":
                        self.names.learn_person(learn["id"], learn["name"])
                    else:
                        self.names.learn_chat(learn["id"], learn["name"])
                    await self.send_text(
                        chat_id,
                        f"✅ Запомнил {('участника' if learn['kind'] == 'person' else 'чат')} "
                        f"{learn['id']} = «{learn['name']}»",
                    )
                    return
                # Иные сообщения из чата утверждения — как управляющие команды
                await self.post_webhook(
                    {
                        "type": "approval",
                        "chat_id": chat_id,
                        "chat_name": chat_name,
                        "from_id": user_id,
                        "from_name": user_name,
                        "text": text,
                        "timestamp": ts(),
                    }
                )
                return

            # --- Названия/имена: при отсутствии спрашиваем владельца ---
            if chat_name is None:
                await self.ask_unknown("chat", chat_id, context)
            if user_name is None and user_id is not None:
                await self.ask_unknown("person", user_id, context)

            # --- Вложения ---
            files = await extract_files(client, message, self.settings.media_max_bytes)
            has_files = any(f.get("base64") for f in files)

            if not text and not has_files:
                return

            envelope: dict = {
                "type": "message",
                "source": "max",
                "chat_id": chat_id,
                "chat_name": chat_name,
                "chat_unknown": chat_name is None,
                "message_id": getattr(message, "id", None),
                "from_id": user_id,
                "from_name": user_name,
                "from_unknown": user_name is None,
                "text": text,
                "files": files,
                "timestamp": ts(),
            }
            # Пересланные сообщения: pymax кладёт источник в message.link
            link = getattr(message, "link", None)
            if link is not None:
                linked = getattr(link, "message", None)
                envelope["forward"] = {
                    "chat_id": getattr(link, "chat_id", None),
                    "text": (getattr(linked, "text", "") or "")[:2000] if linked else None,
                }

            answer = await self.post_webhook(envelope)

            # Опциональный автответ: если OpenClaw вернул {"answer": "..."}
            if answer and self.settings.auto_reply:
                reply = ""
                if isinstance(answer, dict):
                    reply = str(answer.get("answer") or answer.get("output") or "").strip()
                if reply:
                    await self.send_text(chat_id, reply, reply_to=envelope["message_id"])

        except Exception:
            log.exception("handle_message: непредвиденная ошибка")

    # ------------------------------------------------------------------ #
    # Стартовая синхронизация чатов (автозахват названий)
    # ------------------------------------------------------------------ #
    async def bootstrap_chats(self, client: Any) -> None:
        self.client = client
        me = getattr(client, "me", None)
        contact = getattr(me, "contact", None) if me else None
        self.me = me
        log.info(
            "клиент запущен, аккаунт: %s",
            (getattr(contact, "id", None) or getattr(me, "id", None) or "unknown"),
        )
        try:
            chats = await client.fetch_chats()
            known = 0
            for chat in chats or []:
                cid = getattr(chat, "id", None)
                title = getattr(chat, "title", None)
                if cid is None or not title:
                    continue
                if self.names.chat(cid) is None:
                    self.names.learn_chat(cid, str(title), source="pymax")
                known += 1
            log.info("синхронизировано чатов: %d", known)
        except Exception as exc:
            log.warning("fetch_chats: %s", exc)
