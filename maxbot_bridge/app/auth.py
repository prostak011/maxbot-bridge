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
    """Ручной QR-провайдер: показывает QR и ждёт подтверждения через API.

    Используется ManualQrAuthFlow (2 запроса к MAX, без polling).
    Страница /auth рисует QR и показывает кнопку «Подтвердить».
    """

    def __init__(self) -> None:
        self.qr_url: str | None = None
        self.track_id: str | None = None
        self.updated_at: float = 0.0
        self.waiting: bool = False
        self.expired: bool = False
        self._confirm_queue: asyncio.Queue[bool] = asyncio.Queue()

    def set_qr(self, qr_url: str, track_id: str) -> None:
        """Вызывается ManualQrAuthFlow при получении QR от сервера."""
        self.qr_url = qr_url
        self.track_id = track_id
        self.updated_at = time.time()
        self.waiting = True
        self.expired = False
        log.info("QR получен (track=%s) — отсканируйте на странице /auth", track_id)

    async def wait_confirmation(self) -> bool:
        """Ждёт, пока пользователь нажмёт «Подтвердить» или истечёт таймаут."""
        return await self._confirm_queue.get()

    def confirm(self) -> None:
        """Вызывается из /auth/confirm_qr при нажатии кнопки «Подтвердить»."""
        if self.waiting and not self.expired:
            self._confirm_queue.put_nowait(True)
            log.info("QR подтверждён пользователем")

    def set_expired(self) -> None:
        """Вызывается при истечении QR-кода."""
        self.waiting = False
        self.expired = True
        self.qr_url = None
        self.track_id = None
        log.info("QR истёк")

    def reset(self) -> None:
        self.qr_url = None
        self.track_id = None
        self.waiting = False
        self.expired = False

    @property
    def has_qr(self) -> bool:
        return bool(self.qr_url) and not self.expired

    # Совместимость со старым API (show_qr для QrHandler protocol)
    async def show_qr(self, qr_url: str) -> None:
        """Совместимость с QrHandler protocol pymax (не используется в manual flow)."""
        self.qr_url = qr_url
        self.updated_at = time.time()
        self.waiting = True
        log.info("QR получен (show_qr) — отсканируйте на странице /auth")


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
