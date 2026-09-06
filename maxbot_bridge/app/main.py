# -*- coding: utf-8 -*-
"""MaxBot Bridge — точка входа.

Поднимает:
  1. HTTP API (порт 8099): /auth для SMS-кода, /send /learn для OpenClaw.
  2. pymax Client (TCP): слушает все чаты, конверты → вебхук.

Первый запуск: если сессии нет, pymax запросит SMS-код через WebSmsCodeProvider —
пользователь вводит код на странице http://<ha>:8099/auth.
"""

from __future__ import annotations

import asyncio
import logging
import signal
import sys
from pathlib import Path

from .auth import OptionsPasswordProvider, WebSmsCodeProvider
from .bridge import Bridge
from .http_api import HttpApi
from .names import NameStore
from .settings import DATA_DIR, load_settings

log = logging.getLogger("maxbot")


def setup_logging(level: str) -> None:
    logging.basicConfig(
        level=level,
        format="%(asctime)s | %(levelname)-7s | %(name)s | %(message)s",
        stream=sys.stdout,
    )


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

    if not settings.max_phone:
        log.error("max_phone не задан — заполните опции аддона и перезапустите")

    (DATA_DIR / "cache").mkdir(parents=True, exist_ok=True)

    names = NameStore(DATA_DIR)
    bridge = Bridge(settings, names)
    sms_provider = WebSmsCodeProvider()
    api = HttpApi(settings, names, sms_provider, bridge)
    runner = await api.start(settings.http_port)

    # pymax импортируем внутри: аддон должен стартовать даже при сбое импорта,
    # чтобы страница /auth и логи были доступны
    try:
        from pymax import Client
        from pymax.api.session.enums import DeviceType  # noqa: F401 — проверка пути
    except Exception as exc:  # pragma: no cover
        log.error("pymax не импортируется: %s — проверьте requirements", exc)
        await asyncio.Event().wait()  # держим HTTP API живым для диагностики
        return

    def build_client() -> Client:
        extra_kwargs: dict = {}
        if settings.max_2fa_password:
            extra_kwargs["password_provider"] = OptionsPasswordProvider(
                settings.max_2fa_password
            )
        return Client(
            phone=settings.max_phone,
            work_dir=str(Path(settings.work_dir)),
            session_name="main.db",
            sms_code_provider=sms_provider,
            **extra_kwargs,
        )

    stop_event = asyncio.Event()

    def request_stop() -> None:
        log.info("получен сигнал остановки")
        stop_event.set()

    loop = asyncio.get_running_loop()
    for sig in (signal.SIGINT, signal.SIGTERM):
        try:
            loop.add_signal_handler(sig, request_stop)
        except NotImplementedError:  # pragma: no cover
            pass

    while not stop_event.is_set():
        client = build_client()

        @client.on_start()
        async def on_start(c) -> None:  # type: ignore[no-untyped-def]
            bridge.connected = True
            await bridge.bootstrap_chats(c)

        @client.on_disconnect()
        async def on_disconnect(exc, reconnect: bool, delay: float) -> None:  # type: ignore[no-untyped-def]
            bridge.connected = False
            log.warning("соединение потеряно: %s (reconnect=%s, delay=%s)", exc, reconnect, delay)

        @client.on_message()
        async def on_message(message, c) -> None:  # type: ignore[no-untyped-def]
            await bridge.handle_message(message, c)

        try:
            log.info("подключаемся к MAX (если сессии нет — ждём SMS-код на /auth)...")
            await client.start()  # блокируется до штатного закрытия/отмены
        except asyncio.CancelledError:
            raise
        except Exception as exc:
            log.error("клиент упал: %s — перезапуск через 10 с", exc)
            await asyncio.sleep(10)
        finally:
            bridge.connected = False
            try:
                await client.stop()
            except Exception:
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
