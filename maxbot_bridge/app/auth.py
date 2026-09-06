# -*- coding: utf-8 -*-
"""Провайдеры авторизации pymax.

SMS-код приходит не из консоли, а через веб-страницу аддона:
пользователь открывает http://<ha>:8099/auth и вводит код из SMS.
Пароль 2FA (если включён) берётся из опций аддона.
"""

from __future__ import annotations

import asyncio
import logging

log = logging.getLogger("maxbot.auth")


class WebSmsCodeProvider:
    """Провайдер SMS-кода на asyncio.Queue.

    pymax вызывает ``get_code(phone)`` при первой авторизации; метод ждёт,
    пока пользователь введёт код на странице /auth (POST /auth/code кладёт
    его в очередь через ``set_code``).
    """

    def __init__(self) -> None:
        self._queue: asyncio.Queue[str] = asyncio.Queue()
        self.waiting: bool = False
        self.phone: str | None = None

    async def get_code(self, phone: str) -> str:
        self.waiting = True
        self.phone = phone
        log.info("Запрошен SMS-код для %s — введите его на странице /auth", phone)
        try:
            return await self._queue.get()
        finally:
            self.waiting = False

    async def set_code(self, code: str) -> None:
        log.info("SMS-код получен через веб-интерфейс")
        await self._queue.put(code.strip())

    @property
    def need_code(self) -> bool:
        return self.waiting


class OptionsPasswordProvider:
    """Провайдер пароля 2FA из опций аддона (может быть пустым)."""

    def __init__(self, password: str) -> None:
        self._password = password

    async def get_password(self, hint: str | None = None) -> str:
        if not self._password:
            log.error("MAX требует пароль 2FA, но он не задан в опциях аддона")
        return self._password
