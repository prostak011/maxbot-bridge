# -*- coding: utf-8 -*-
"""HTTP API аддона (aiohttp, порт 8099).

Эндпоинты:
  GET  /health        — статус (для watchdog HA и диагностики)
  GET  /auth          — HTML-страница ввода SMS-кода
  GET  /auth/status   — {"need_code": bool, "connected": bool}
  POST /auth/code     — {"code": "1234"} → передать код в pymax
  GET  /chats         — список чатов (ID + названия) для настройки
  GET  /names         — выгрузка people.json / chats.json
  POST /send          — {"chat_id": int, "text": str} → отправка в MAX (для OpenClaw)
  POST /learn         — {"type": "person"|"chat", "id": int, "name": str}

Защищённые токеном (/send, /learn): заголовок X-MaxBot-Token = webhook_token.
Страница /auth и /health открыты в локальной сети (UI ввода кода).
"""

from __future__ import annotations

import logging
from typing import Any

from aiohttp import web

log = logging.getLogger("maxbot.http")

AUTH_PAGE = """<!DOCTYPE html>
<html lang="ru"><head><meta charset="utf-8">
<title>MaxBot Bridge — вход</title>
<style>
 body{font-family:-apple-system,Segoe UI,Roboto,sans-serif;max-width:480px;margin:48px auto;padding:0 16px;color:#222}
 input,button{font-size:18px;padding:10px;border-radius:8px;border:1px solid #bbb;width:100%;box-sizing:border-box}
 button{margin-top:12px;background:#0b6bcb;color:#fff;border:none;cursor:pointer}
 #msg{margin-top:16px;padding:12px;border-radius:8px;display:none}
 .ok{background:#e6f6e6}.err{background:#fde8e8}
</style></head><body>
<h2>MaxBot Bridge</h2>
<p>Введите код из SMS, отправленный на номер <b>__PHONE__</b>.</p>
<form id="f"><input id="code" placeholder="Код из SMS" autofocus autocomplete="one-time-code">
<button type="submit">Войти</button></form>
<div id="msg"></div>
<script>
 f.onsubmit=async e=>{e.preventDefault();
  const r=await fetch('/auth/code',{method:'POST',headers:{'Content-Type':'application/json'},
    body:JSON.stringify({code:document.getElementById('code').value})});
  const j=await r.json();const m=document.getElementById('msg');
  m.style.display='block';m.className=j.ok?'ok':'err';m.textContent=j.message;}
</script></body></html>"""


class HttpApi:
    def __init__(self, settings: Any, names: Any, sms_provider: Any, bridge: Any) -> None:
        self.settings = settings
        self.names = names
        self.sms = sms_provider
        self.bridge = bridge
        self.app = web.Application()
        self.app.router.add_get("/health", self.health)
        self.app.router.add_get("/auth", self.auth_page)
        self.app.router.add_get("/auth/status", self.auth_status)
        self.app.router.add_post("/auth/code", self.auth_code)
        self.app.router.add_get("/chats", self.chats)
        self.app.router.add_get("/names", self.names_dump)
        self.app.router.add_post("/send", self.send)
        self.app.router.add_post("/learn", self.learn)

    # ------------------------------------------------------------------ #
    def _authorized(self, request: web.Request) -> bool:
        token = self.settings.webhook_token
        if not token:
            return True
        return request.headers.get("X-MaxBot-Token", "") == token

    # ------------------------------------------------------------------ #
    async def health(self, request: web.Request) -> web.Response:
        client = self.bridge.client
        connected = False
        try:
            connected = bool(getattr(client, "is_connected", False))
        except Exception:
            pass
        me = getattr(self.bridge, "me", None)
        contact = getattr(me, "contact", None) if me else None
        return web.json_response(
            {
                "status": "ok",
                "connected": connected,
                "account": getattr(contact, "id", None) or getattr(me, "id", None),
                "webhook_ready": self.settings.webhook_ready,
                "need_code": self.sms.need_code,
            }
        )

    async def auth_page(self, request: web.Request) -> web.Response:
        html = AUTH_PAGE.replace("__PHONE__", self.settings.max_phone or "(не задан)")
        return web.Response(text=html, content_type="text/html")

    async def auth_status(self, request: web.Request) -> web.Response:
        client = self.bridge.client
        connected = bool(getattr(client, "is_connected", False)) if client else False
        return web.json_response({"need_code": self.sms.need_code, "connected": connected})

    async def auth_code(self, request: web.Request) -> web.Response:
        try:
            data = await request.json()
        except Exception:
            return web.json_response({"ok": False, "message": "неверный JSON"}, status=400)
        code = str((data or {}).get("code", "")).strip()
        if not code:
            return web.json_response({"ok": False, "message": "код пуст"}, status=400)
        await self.sms.set_code(code)
        return web.json_response({"ok": True, "message": "Код принят, входим..."})

    async def chats(self, request: web.Request) -> web.Response:
        result = []
        client = self.bridge.client
        for chat in (getattr(client, "chats", None) or []):
            cid = getattr(chat, "id", None)
            if cid is None:
                continue
            result.append(
                {"id": cid, "title": getattr(chat, "title", None) or self.names.chat(cid)}
            )
        # Добавляем выученные ранее, которых нет в живом кеше
        known_ids = {c["id"] for c in result}
        for cid, entry in self.names.chats.items():
            try:
                cid_int = int(cid)
            except ValueError:
                continue
            if cid_int not in known_ids:
                result.append({"id": cid_int, "title": entry["title"], "cached": True})
        return web.json_response({"chats": result})

    async def names_dump(self, request: web.Request) -> web.Response:
        return web.json_response(self.names.export())

    async def send(self, request: web.Request) -> web.Response:
        if not self._authorized(request):
            return web.json_response({"ok": False, "error": "unauthorized"}, status=401)
        try:
            data = await request.json()
        except Exception:
            return web.json_response({"ok": False, "error": "неверный JSON"}, status=400)
        chat_id = (data or {}).get("chat_id")
        text = (data or {}).get("text")
        if chat_id is None or not text:
            return web.json_response(
                {"ok": False, "error": "нужны chat_id и text"}, status=400
            )
        ok = await self.bridge.send_text(int(chat_id), str(text))
        return web.json_response({"ok": ok}, status=200 if ok else 502)

    async def learn(self, request: web.Request) -> web.Response:
        if not self._authorized(request):
            return web.json_response({"ok": False, "error": "unauthorized"}, status=401)
        try:
            data = await request.json()
        except Exception:
            return web.json_response({"ok": False, "error": "неверный JSON"}, status=400)
        kind = (data or {}).get("type")
        uid = (data or {}).get("id")
        name = (data or {}).get("name")
        if kind not in ("person", "chat") or uid is None or not name:
            return web.json_response(
                {"ok": False, "error": "нужны type(person|chat), id, name"}, status=400
            )
        if kind == "person":
            self.names.learn_person(uid, str(name), source="api")
        else:
            self.names.learn_chat(uid, str(name), source="api")
        return web.json_response({"ok": True})

    # ------------------------------------------------------------------ #
    async def start(self, port: int) -> web.AppRunner:
        runner = web.AppRunner(self.app)
        await runner.setup()
        site = web.TCPSite(runner, "0.0.0.0", port)
        await site.start()
        log.info("HTTP API поднят на порту %d", port)
        return runner
