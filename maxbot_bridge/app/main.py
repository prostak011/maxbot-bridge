# -*- coding: utf-8 -*-
"""MaxBot Bridge — точка входа.

Поднимает:
  1. HTTP API (порт 8099): /auth (QR + SMS), /send /learn для OpenClaw.
  2. Клиент pymax: WebClient (QR-вход, по умолчанию) или Client (SMS-вход).

Первый запуск: откройте http://<ha>:8099/auth — там QR и переключатель на SMS.
Способ входа меняется на лету (POST /auth/method) и сохраняется в /data.
После первого входа сессия хранится в /data/cache и авторизация не требуется.
"""

from __future__ import annotations

import asyncio
import json
import logging
import signal
import sys
from pathlib import Path

from .auth import OptionsPasswordProvider, WebQrProvider, WebSmsCodeProvider
from .bridge import Bridge
from .http_api import HttpApi
from .names import NameStore
from .settings import DATA_DIR, load_settings

log = logging.getLogger("maxbot")

METHOD_FILE = DATA_DIR / "auth_method.json"
RESTART_DELAY = 10.0


def setup_logging(level: str) -> None:
    logging.basicConfig(
        level=level,
        format="%(asctime)s | %(levelname)-7s | %(name)s | %(message)s",
        stream=sys.stdout,
    )


def load_method(default: str) -> str:
    """Выбранный пользователем способ входа (переживает рестарты)."""
    try:
        data = json.loads(METHOD_FILE.read_text(encoding="utf-8"))
        method = str(data.get("method", default)).lower()
        return method if method in {"qr", "sms"} else default
    except Exception:
        return default


def save_method(method: str) -> None:
    try:
        METHOD_FILE.parent.mkdir(parents=True, exist_ok=True)
        METHOD_FILE.write_text(json.dumps({"method": method}), encoding="utf-8")
    except Exception as exc:
        log.error("не удалось сохранить метод авторизации: %s", exc)


def build_client(settings, method: str, sms_provider, qr_provider):  # noqa: ANN001
    """Создать клиент pymax выбранного способа входа.

    qr  → WebClient (WebSocket, QR-вход, устройство WEB)
    sms → Client (TCP, вход по SMS-коду)
    """
    from pymax import Client, WebClient

    password_provider = (
        OptionsPasswordProvider(settings.max_2fa_password)
        if settings.max_2fa_password
        else None
    )
    work_dir = str(Path(settings.work_dir))

    if method == "qr":
        kwargs: dict = {"qr_provider": qr_provider, "work_dir": work_dir}
        if password_provider:
            kwargs["password_provider"] = password_provider
        return WebClient(session_name="main.db", **kwargs)

    kwargs = {
        "phone": settings.max_phone,
        "work_dir": work_dir,
        "session_name": "main.db",
        "sms_code_provider": sms_provider,
    }
    if password_provider:
        kwargs["password_provider"] = password_provider
    return Client(**kwargs)


async def run() -> None:
    settings = load_settings()
    setup_logging(settings.log_level)
    log.info("=== MaxBot Bridge стартует ===")
    log.info(
        "phone=%s, webhook=%s, approval_chat=%s, ignore=%s",
        settings.max_phone or "(не задан)",
        settings.webhook_url or "(не задан)",
        settings.approval_chat_id,
        sorted(settings.ignore_chats),
    )

    (DATA_DIR / "cache").mkdir(parents=True, exist_ok=True)

    # Сначала /data (ручной выбор), потом опция аддона как дефолт
    method = load_method(settings.auth_method)
    log.info("способ входа: %s", method)

    names = NameStore(DATA_DIR)
    bridge = Bridge(settings, names)
    sms_provider = WebSmsCodeProvider()
    qr_provider = WebQrProvider()
    bridge.qr_provider = qr_provider  # для страницы /auth (qr.svg)
    api = HttpApi(settings, names, sms_provider, bridge)
    runner = await api.start(settings.http_port)

    # pymax импортируем внутри: аддон должен стартовать даже при сбое импорта,
    # чтобы страница /auth и логи были доступны
    try:
        from pymax import Client, WebClient  # noqa: F401
    except Exception as exc:  # pragma: no cover
        log.error("pymax не импортируется: %s — проверьте requirements", exc)
        await asyncio.Event().wait()  # держим HTTP API живым для диагностики
        return

    stop_event = asyncio.Event()
    switch_task: asyncio.Task | None = None

    def request_stop() -> None:
        log.info("получен сигнал остановки")
        stop_event.set()

    loop = asyncio.get_running_loop()
    for sig in (signal.SIGINT, signal.SIGTERM):
        try:
            loop.add_signal_handler(sig, request_stop)
        except NotImplementedError:  # pragma: no cover
            pass

    async def client_loop() -> None:
        """Жизненный цикл клиента текущего метода: коннект + автоперезапуск."""
        nonlocal method
        while not stop_event.is_set():
            client = build_client(settings, method, sms_provider, qr_provider)

            @client.on_start()
            async def on_start(c) -> None:  # type: ignore[no-untyped-def]
                bridge.connected = True
                await bridge.bootstrap_chats(c)

            @client.on_disconnect()
            async def on_disconnect(exc, reconnect: bool, delay: float) -> None:  # type: ignore[no-untyped-def]
                bridge.connected = False
                log.warning(
                    "соединение потеряно: %s (reconnect=%s, delay=%s)", exc, reconnect, delay
                )

            @client.on_message()
            async def on_message(message, c) -> None:  # type: ignore[no-untyped-def]
                await bridge.handle_message(message, c)

            try:
                log.info(
                    "подключаемся к MAX (метод %s; вход — на странице /auth)...", method
                )
                await client.start()  # блокируется до закрытия/отмены
                log.info("клиент штатно завершился")
                break
            except asyncio.CancelledError:
                raise
            except Exception as exc:
                log.error("клиент упал: %s — перезапуск через %s с", exc, RESTART_DELAY)
                bridge.connected = False
                try:
                    await asyncio.wait_for(stop_event.wait(), timeout=RESTART_DELAY)
                except asyncio.TimeoutError:
                    continue
                break
            finally:
                try:
                    await client.stop()
                except Exception:
                    pass
                bridge.connected = False

    current_task = asyncio.create_task(client_loop())

    async def switch_method(new_method: str) -> bool:
        """Переключить способ входа на лету: отменить клиент, запустить новый."""
        nonlocal current_task, method, switch_task
        if new_method == method:
            return True
        log.info("переключение способа входа: %s → %s", method, new_method)
        method = new_method
        save_method(new_method)
        qr_provider.reset()
        sms_provider.waiting = False
        current_task.cancel()
        try:
            await current_task
        except asyncio.CancelledError:
            pass
        current_task = asyncio.create_task(client_loop())
        return True

    async def request_sms_code() -> bool:
        """Явный запрос нового SMS-кода: перезапуск в sms-режиме."""
        nonlocal current_task, method, switch_task
        log.info("ручной запрос SMS-кода (перезапуск клиента в sms-режиме)")
        method = "sms"
        save_method("sms")
        current_task.cancel()
        try:
            await current_task
        except asyncio.CancelledError:
            pass
        current_task = asyncio.create_task(client_loop())
        return True

    api.set_controllers(
        get_method=lambda: method,
        switch_method=switch_method,
        request_sms_code=request_sms_code,
    )

    await stop_event.wait()
    current_task.cancel()
    try:
        await current_task
    except asyncio.CancelledError:
        pass
    await runner.cleanup()
    log.info("MaxBot Bridge остановлен")


def main() -> None:
    try:
        asyncio.run(run())
    except KeyboardInterrupt:
        pass


if __name__ == "__main__":
    main()
