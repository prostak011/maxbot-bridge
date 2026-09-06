# -*- coding: utf-8 -*-
"""Провайдеры авторизации pymax: SMS-код и QR.

Два способа входа (переключаются на лету через страницу /auth):

- **QR** (по умолчанию): WebQrProvider получает qr_link от QrAuthFlow,
  страница /auth рисует его как SVG (segno). Пользователь сканирует QR
  из приложения MAX (Настройки → Устройства). SMS и лимиты не участвуют.
- **SMS**: WebSmsCodeProvider ждёт код на странице /auth. Код запрашивается
  при старте клиента без сессии (повторный запрос — через /auth/request_code).

Пароль 2FA (если включён) берётся из опций аддона.
"""

from __future__ import annotations

import asyncio
import logging
import time

log = logging.getLogger("maxbot.auth")


class WebQrProvider:
    """QrHandler-совместимый провайдер: сохраняет qr_link для веб-страницы.

    pymax вызывает show_qr(qr_url) в момент получения QR от сервера.
    Страница /auth читает qr_url через /auth/status и /auth/qr.svg.
    """

    def __init__(self) -> None:
        self.qr_url: str | None = None
        self.updated_at: float = 0.0
        self.waiting: bool = False

    async def show_qr(self, qr_url: str) -> None:
        self.qr_url = qr_url
        self.updated_at = time.time()
        self.waiting = True
        log.info("QR получен — отсканируйте его на странице /auth")

    def reset(self) -> None:
        self.qr_url = None
        self.waiting = False

    @property
    def has_qr(self) -> bool:
        return bool(self.qr_url)


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
