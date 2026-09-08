# Changelog

## 0.3.3
- `webhook_verify_ssl` (default false): отключение проверки TLS-сертификата webhook.
  Фикс `CERTIFICATE_VERIFY_FAILED` при self-signed сертификате OpenClaw (nginx :18789) —
  сообщения из MAX не доходили до webhook.
- `httpx.AsyncClient(verify=...)` в `post_webhook`.

## 0.3.2
- `Authorization: Bearer` для webhook (замена X-MaxBot-Token).
- Дефолтные webhook_url/token в config.yaml.

## 0.3.1
- Нормализация телефона: `8996...` → `+7996...`, убирает пробелы, скобки, тире.
- `max_phone` не обязателен для QR-входа (`str?` в schema).
- Cooldown для SMS-кнопки: 5 минут между запросами (серверный + клиентский таймер).
- `send_text`: логирует обе ошибки при TypeError (original + fallback).
- UI: `qr_waiting` сбрасывается после успешного входа.
- Удалён мёртвый код `switch_task`.

## 0.3.0
- **Ручной QR-вход без polling**: вместо автоматического цикла poll-запросов — 2 запроса к MAX:
  `request_qr()` и `confirm_qr()`.
- Кнопки «Запросить QR» и «Подтвердить сканирование» на странице `/auth`.
- Новые эндпоинты: `POST /auth/request_qr`, `POST /auth/confirm_qr`, `/auth/status`.
