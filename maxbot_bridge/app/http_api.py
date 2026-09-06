# -*- coding: utf-8 -*-
"""HTTP API аддона (aiohttp, порт 8099).

Эндпоинты:
  GET  /health         — статус (для watchdog HA и диагностики)
  GET  /auth           — страница входа: QR (по умолчанию) + SMS с табами
  GET  /auth/status    — {mode, connected, need_code, qr_url, qr_updated}
  GET  /auth/qr.svg    — QR-картинка текущей ссылки (segno, SVG)
  POST /auth/code      — {"code": "1234"} → передать код в pymax (SMS-режим)
  POST /auth/method    — {"method": "qr"|"sms"} → переключить способ на лету
  POST /auth/request_code — перезапустить клиент в sms-режиме (новый запрос кода)
  GET  /chats          — список чатов (ID + названия) для настройки
  GET  /names          — выгрузка people.json / chats.json
  POST /send           — {"chat_id": int, "text": str} → отправка в MAX (для OpenClaw)
  POST /learn          — {"type": "person"|"chat", "id": int, "name": str}

Защищённые токеном (/send, /learn): заголовок X-MaxBot-Token = webhook_token.
Страница /auth и /health открыты в локальной сети (UI ввода кода).
"""

from __future__ import annotations

import io
import logging
from typing import Any, Awaitable, Callable

import segno
from aiohttp import web

log = logging.getLogger("maxbot.http")

AUTH_PAGE = """<!DOCTYPE html>
<html lang="ru"><head><meta charset="utf-8">
<title>MaxBot Bridge — вход</title>
<meta name="viewport" content="width=device-width, initial-scale=1">
<style>
 body{font-family:-apple-system,Segoe UI,Roboto,sans-serif;max-width:520px;margin:40px auto;padding:0 16px;color:#222}
 h2{margin-bottom:4px} .sub{color:#777;margin-top:0}
 .tabs{display:flex;gap:8px;margin:20px 0 12px}
 .tab{flex:1;padding:10px;border:1px solid #ccc;border-radius:8px;background:#f5f5f5;cursor:pointer;text-align:center;font-size:15px}
 .tab.active{background:#0b6bcb;color:#fff;border-color:#0b6bcb}
 .panel{display:none;border:1px solid #e3e3e3;border-radius:10px;padding:18px}
 .panel.active{display:block}
 .qrbox{display:flex;justify-content:center;padding:10px;background:#fff;border-radius:8px}
 .qrbox img{width:240px;height:240px}
 .hint{color:#666;font-size:14px;line-height:1.45;margin-top:10px}
 input,button{font-size:16px;padding:10px;border-radius:8px;border:1px solid #bbb;width:100%;box-sizing:border-box;margin-top:8px}
 button{background:#0b6bcb;color:#fff;border:none;cursor:pointer}
 button.secondary{background:#666}
 button:disabled{opacity:.5;cursor:default}
 #msg{margin-top:14px;padding:12px;border-radius:8px;display:none}
 .ok{background:#e6f6e6}.err{background:#fde8e8}.warn{background:#fff7e0}
 .logged{border:1px solid #bfe3bf;border-radius:10px;padding:18px;background:#f2fbf2;display:none}
</style></head><body>
<h2>MaxBot Bridge</h2>
<p class="sub" id="phonehint"></p>

<div class="logged" id="logged">
  <b>✅ Вход уже выполнен</b><div id="whoami" class="hint"></div>
</div>

<div id="loginblocks">
<div class="tabs">
  <div class="tab active" id="tab-qr" onclick="showTab('qr')">Вход по QR</div>
  <div class="tab" id="tab-sms" onclick="showTab('sms')">Вход по SMS</div>
</div>

<div class="panel active" id="panel-qr">
  <div class="qrbox"><img id="qrimg" alt="QR не получен" src="/auth/qr.svg"></div>
  <div class="hint">
    1. Откройте приложение <b>MAX</b> на телефоне<br>
    2. <b>Настройки → Устройства / Привязать устройство</b><br>
    3. Наведите камеру на QR-код<br>
    4. Подтвердите вход
  </div>
</div>

<div class="panel" id="panel-sms">
  <button class="secondary" id="reqbtn" onclick="requestCode()">Запросить SMS-код</button>
  <div class="hint" id="smsstatus">Код запрашивается при старте. Если SMS не приходит —
  подождите 5–10 минут (лимит запросов MAX) и нажмите кнопку.</div>
  <form id="f"><input id="code" placeholder="Код" autocomplete="one-time-code" inputmode="numeric">
  <button type="submit">Войти по SMS</button></form>
</div>

<div id="msg"></div>
</div>

<script>
let mode='qr';
function showTab(m){mode=m;
 ['qr','sms'].forEach(k=>{document.getElementById('tab-'+k).classList.toggle('active',k===m);
 document.getElementById('panel-'+k).classList.toggle('active',k===m);});}
function msg(text,cls){const m=document.getElementById('msg');
 m.style.display='block';m.className=cls;m.textContent=text;}
async function poll(){try{
 const r=await fetch('/auth/status');const j=await r.json();
 document.getElementById('logged').style.display=j.connected?'block':'none';
 document.getElementById('loginblocks').style.display=j.connected?'none':'block';
 if(j.connected&&j.account){document.getElementById('whoami').textContent='ID аккаунта: '+j.account;}
 document.getElementById('phonehint').textContent=j.phone?('Номер: '+j.phone):'';
 document.getElementById('smsstatus').textContent=j.need_code?'Код запрошен — введите его из SMS ниже.'
   :'Код запрашивается при старте. Если SMS не приходит — подождите 5–10 минут и нажмите кнопку.';
 document.getElementById('reqbtn').disabled=j.need_code;
 if(mode==='qr'){const img=document.getElementById('qrimg');
   if(j.qr_url){img.src='/auth/qr.svg?v='+Math.floor(j.qr_updated||0);}
 }}
 catch(e){}}
async function requestCode(){document.getElementById('reqbtn').disabled=true;msg('Запрашиваю новый код...','warn');
 try{const r=await fetch('/auth/request_code',{method:'POST'});const j=await r.json();
  msg(j.ok?'Новый код запрошен. Проверьте SMS/приложение MAX (код из СТАРОЙ SMS не подойдёт).':('Ошибка: '+j.error),j.ok?'ok':'err');}
 catch(e){msg('Ошибка сети: '+e,'err');}}
document.getElementById('f').onsubmit=async e=>{e.preventDefault();
 const r=await fetch('/auth/code',{method:'POST',headers:{'Content-Type':'application/json'},
  body:JSON.stringify({code:document.getElementById('code').value})});
 const j=await r.json();msg(j.message,j.ok?'ok':'err');if(j.ok)document.getElementById('code').value='';};
poll();setInterval(poll,2500);
</script></body></html>"""


class HttpApi:
    def __init__(self, settings: Any, names: Any, sms_provider: Any, bridge: Any) -> None:
        self.settings = settings
        self.names = names
        self.sms = sms_provider
        self.bridge = bridge
        # Контроллеры способа входа (назначаются из main.run)
        self._get_method: Callable[[], str] = lambda: "qr"
        self._switch_method: Callable[[str], Awaitable[bool]] | None = None
        self._request_sms_code: Callable[[], Awaitable[bool]] | None = None

        self.app = web.Application()
        self.app.router.add_get("/health", self.health)
        self.app.router.add_get("/auth", self.auth_page)
        self.app.router.add_get("/auth/status", self.auth_status)
        self.app.router.add_get("/auth/qr.svg", self.auth_qr_svg)
        self.app.router.add_post("/auth/code", self.auth_code)
        self.app.router.add_post("/auth/method", self.auth_method)
        self.app.router.add_post("/auth/request_code", self.auth_request_code)
        self.app.router.add_get("/chats", self.chats)
        self.app.router.add_get("/names", self.names_dump)
        self.app.router.add_post("/send", self.send)
        self.app.router.add_post("/learn", self.learn)

    # ------------------------------------------------------------------ #
    def set_controllers(
        self,
        get_method: Callable[[], str],
        switch_method: Callable[[str], Awaitable[bool]],
        request_sms_code: Callable[[], Awaitable[bool]],
    ) -> None:
        self._get_method = get_method
        self._switch_method = switch_method
        self._request_sms_code = request_sms_code

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
                "auth_method": self._get_method(),
                "webhook_ready": self.settings.webhook_ready,
                "need_code": self.sms.need_code,
            }
        )

    async def auth_page(self, request: web.Request) -> web.Response:
        return web.Response(text=AUTH_PAGE, content_type="text/html")

    async def auth_status(self, request: web.Request) -> web.Response:
        client = self.bridge.client
        connected = bool(getattr(client, "is_connected", False)) if client else False
        me = getattr(self.bridge, "me", None)
        contact = getattr(me, "contact", None) if me else None
        account = getattr(contact, "id", None) or getattr(me, "id", None)
        qr_provider = getattr(self.bridge, "qr_provider", None)
        return web.json_response(
            {
                "mode": self._get_method(),
                "connected": connected,
                "account": account,
                "phone": self.settings.max_phone or None,
                "need_code": self.sms.need_code,
                "qr_url": getattr(qr_provider, "qr_url", None) or "",
                "qr_updated": int(getattr(qr_provider, "updated_at", 0.0)),
            }
        )

    async def auth_qr_svg(self, request: web.Request) -> web.Response:
        """SVG-QR текущей ссылки (segno, без зависимостей)."""
        qr_provider = getattr(self.bridge, "qr_provider", None)
        url = getattr(qr_provider, "qr_url", None) if qr_provider else None
        if not url:
            return web.Response(status=404, text="QR ещё не получен")
        try:
            buf = io.BytesIO()
            segno.make(url, error="m").save(buf, kind="svg", scale=8, border=2)
            return web.Response(
                body=buf.getvalue(), content_type="image/svg+xml",
                headers={"Cache-Control": "no-store"},
            )
        except Exception as exc:
            log.error("ошибка генерации QR: %s", exc)
            return web.Response(status=500, text="ошибка генерации QR")

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

    async def auth_method(self, request: web.Request) -> web.Response:
        """Переключить способ входа на лету (qr ↔ sms)."""
        try:
            data = await request.json()
        except Exception:
            return web.json_response({"ok": False, "error": "неверный JSON"}, status=400)
        method = str((data or {}).get("method", "")).lower()
        if method not in {"qr", "sms"}:
            return web.json_response(
                {"ok": False, "error": "method должен быть qr или sms"}, status=400
            )
        if method == self._get_method():
            return web.json_response({"ok": True, "message": f"метод уже {method}"})
        if self._switch_method is None:
            return web.json_response(
                {"ok": False, "error": "контроллер не готов"}, status=503
            )
        ok = await self._switch_method(method)
        return web.json_response(
            {"ok": ok, "message": f"переключено на {method}" if ok else "не удалось"}
        )

    async def auth_request_code(self, request: web.Request) -> web.Response:
        """Явный запрос нового SMS-кода (перезапуск клиента в sms-режиме)."""
        if self._request_sms_code is None:
            return web.json_response(
                {"ok": False, "error": "контроллер не готов"}, status=503
            )
        ok = await self._request_sms_code()
        return web.json_response(
            {"ok": ok, "message": "Новый SMS-код запрошен" if ok else "не удалось"}
        )

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
