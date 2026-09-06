# maxbot-bridge

Репозиторий Home Assistant-аддонов. Содержит **MaxBot Bridge** — userbot
мессенджера MAX (pymax) для Raspberry Pi 4 / HAOS: слушает все чаты, шлёт
конверты (текст + фото, имена, названия чатов) в OpenClaw, принимает ответы
через HTTP API.

Полная документация — в [maxbot_bridge/DOCS.md](maxbot_bridge/DOCS.md).

## Установка

Add-on Store → три точки → «Репозитории» → добавить этот репозиторий →
установить **MaxBot Bridge**.

## CI

`.github/workflows/builder.yaml` собирает multi-arch образы (aarch64 + amd64)
и публикует их в GHCR через `home-assistant/builder` — на малинке ничего
не собирается.
