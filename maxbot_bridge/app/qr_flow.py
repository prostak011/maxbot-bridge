# -*- coding: utf-8 -*-
"""Ручной QR-вход: 1 запрос на QR + 1 запрос на подтверждение.

Вместо QrAuthFlow pymax, который делает N poll-запросов (каждые 5-10 сек),
этот flow делает ровно 2 запроса к MAX:
  1. request_qr() → получаем qr_link + track_id
  2. confirm_qr(track_id) → после сканирования пользователем

Пользователь нажимает «Подтвердить» на странице /auth после сканирования QR.
Нет автоматического polling — нет лишних запросов к серверу MAX.
"""

from __future__ import annotations

import asyncio
import time
from typing import TYPE_CHECKING

from pymax.auth.base import AuthFlow
from pymax.auth.models import AuthResult
from pymax.logging import get_logger

from .auth import OptionsPasswordProvider, WebQrProvider

if TYPE_CHECKING:
    from pymax.app import App

logger = get_logger(__name__)

QR_CONFIRM_TIMEOUT = 120  # секунд на подтверждение QR


class ManualQrAuthFlow(AuthFlow):
    """Ручной QR-вход без автоматического polling.

    Делает только 2 запроса к MAX:
      1. request_qr() → qr_link + track_id
      2. confirm_qr(track_id) → после подтверждения пользователем

    Пользователь нажимает «Подтвердить» на странице /auth после сканирования QR.
    """

    def __init__(
        self,
        qr_provider: WebQrProvider,
        password_provider: OptionsPasswordProvider | None = None,
    ) -> None:
        self.qr_provider = qr_provider
        self.password_provider = password_provider

    async def authenticate(self, app: App) -> AuthResult:
        logger.info("starting manual QR authentication")

        # 1. Запрос QR у сервера MAX (1 запрос)
        qr_info = await app.api.auth.request_qr()
        logger.info("qr requested: expires_at=%s track_id=%s", qr_info.expires_at, qr_info.track_id)

        # 2. Передаём qr_link в провайдер (страница /auth покажет QR)
        self.qr_provider.set_qr(qr_info.qr_link, qr_info.track_id)

        # 3. Ждём подтверждения от пользователя (через API /auth/confirm_qr)
        timeout = max(5, (qr_info.expires_at / 1000) - time.time())
        logger.info("waiting for user to scan QR (timeout %.0f s)...", timeout)

        try:
            confirmed = await asyncio.wait_for(
                self.qr_provider.wait_confirmation(),
                timeout=timeout,
            )
        except asyncio.TimeoutError:
            self.qr_provider.set_expired()
            raise RuntimeError("QR authentication expired — нажмите «Запросить QR» снова")

        if not confirmed:
            self.qr_provider.set_expired()
            raise RuntimeError("QR authentication cancelled")

        # 4. Подтверждаем QR у сервера MAX (2-й запрос)
        logger.info("confirming QR login track_id=%s", qr_info.track_id)
        result = await app.api.auth.confirm_qr(qr_info.track_id)

        token = result.login_token
        if not token and result.password_challenge:
            token = await self._authenticate_with_password(
                app,
                track_id=result.password_challenge.track_id,
                hint=result.password_challenge.hint,
            )

        logger.info("QR authentication completed token_set=%s", bool(token))
        return AuthResult(token=token)

    async def _authenticate_with_password(
        self,
        app: App,
        track_id: str,
        hint: str | None,
    ) -> str:
        logger.info("starting 2fa password authentication")
        while True:
            if not self.password_provider:
                logger.error("2FA required but no password provider")
                raise RuntimeError("2FA password required but not configured")

            password = await self.password_provider.get_password(hint)
            logger.debug("2fa password provider returned password_set=%s", bool(password))
            if not password:
                logger.warning("2fa password is empty; retrying")
                continue

            try:
                response = await app.api.auth.check_password(track_id, password)
            except Exception as e:
                logger.error("2fa password check failed: %s", e)
                continue

            if response.error:
                logger.error("2fa password check failed error=%s", response.error)
                continue

            if response.login_token:
                logger.info("2fa password authentication completed")
                return response.login_token

            logger.error("2fa password response did not contain login token; retrying")
