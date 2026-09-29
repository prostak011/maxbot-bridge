#!/usr/bin/env python3
# courier.py v2 — доставка ответов секретаря в MAX с approval-режимом
#
# Логика:
#   - Чат отладки/утверждения (APPROVAL_CHAT_ID): прямой 🤖-ответ, без черновиков.
#   - Любой другой чат (включая неизвестные): ответ секретаря = черновик.
#     В ОтраБОТка уходит уведомление "🤖 Черновик #N → <чат>: <текст>".
#     Владелец отвечает "ДА N" / "НЕТ N" (или ДА/НЕТ — последний pending).
#   - Команды владельца распознаются детерминированно (regex), не моделью.
#   - TTL черновика 7 дней, протухшие auto-reject.
#
# Команды из MAX проходят обычный путь (Bridge → hook → сессия approval-чата)
# и появляются в transcript_events как user-сообщения — курьер сканирует их
# тем же указателем seq, что и ответы секретаря.

import sqlite3, json, time, re, os, urllib.request

DB = '/config/.openclaw/agents/secretary/agent/openclaw-agent.sqlite'
STATE = '/config/clawd/courier_state.json'
LOG = '/tmp/courier.log'
BRIDGE = 'http://192.168.0.105:8099/send'
TOKEN = 'maxbot-secret-2026'
POLL = 3
KEEP_SESS = 50
DRAFT_TTL = 24 * 3600  # 24 часа (roadmap Ф1.2)

APPROVAL_CHAT_ID = -76638188719558  # «ОтраБОТка»

APPROVAL_RE = re.compile(r'(?:^|\n|\bТекст:\s*)(?:\s*)?(да|ок|нет)\s*#?\s*(\d+)?\s*(?:\n|$)\s*$', re.IGNORECASE)

# ── Фильтр мусорных ответов (26.09.2026) ───────────────────────────────────────
# Агент иногда пишет служебные размышления («NO_REPLY», «No action required») вместо
# ответа человеку. Такой текст НЕ должен превращаться в черновик: владельцу нечего
# решать. Отброшенное пишем в junk_skipped.log для разбора.
#
# ВАЖНО: только ОДНОЗНАЧНЫЕ признаки. Русские формулы вроде «не адресовано
# секретарю» НЕ фильтруем — они встречаются и в нормальных ответах, а потерять
# реальный ответ хуже, чем показать лишний черновик.
JUNK_PATTERNS = [
    r'^\s*NO[_\s-]?REPLY\b',
    r'\bNo\s+(secretary\s+)?action\s+required\b',
    r'\bNo\s+question\s+or\s+request\b',
    r'\bNo\s+secretary\s+action\b',
    r'\bnot\s+addressed\s+to\s+(the\s+)?secretary\b',
    r'\bRoutine\s+group\s+chat\s+(message|question)\b',
    r'\bLogged\s+the\s+context\s+in\s+memory\b',
    r'\bPhoto\s+of\s+.*\s+shared\s+in\s+a\s+group\s+chat\b',
    r'\b(subagent|transcript|checkpoint)\s+has\s+completed\b',
    r'\bLet\s+me\s+collect\s+its\s+results\b',
]
JUNK_RE = re.compile('|'.join(JUNK_PATTERNS), re.IGNORECASE | re.MULTILINE)
JUNK_LOG = '/config/clawd/junk_skipped.log'


def is_junk(text: str) -> bool:
    t = (text or '').strip()
    if not t:
        return True
    return bool(JUNK_RE.search(t))


def note_junk(text: str) -> None:
    try:
        with open(JUNK_LOG, 'a', encoding='utf-8') as fh:
            fh.write(json.dumps({'ts': time.strftime('%Y-%m-%dT%H:%M:%S'),
                                 'text': (text or '')[:400]}, ensure_ascii=False) + '\n')
    except Exception:
        pass

def log(msg):
    line = "%s %s" % (time.strftime('%F %T'), msg)
    print(line, flush=True)
    try:
        with open(LOG, 'a') as f:
            f.write(line + '\n')
    except Exception:
        pass

def load_state():
    try:
        with open(STATE) as f:
            return json.load(f)
    except Exception:
        return {}

def save_state(s):
    try:
        with open(STATE, 'w') as f:
            json.dump(s, f)
    except Exception as e:
        log('state save err: %s' % e)

def send(chat_id, text):
    if not text.startswith('🤖'):
        text = '🤖 ' + text
    if len(text) > 4000:
        text = text[:3990] + ' …'
    payload = json.dumps({'chat_id': int(chat_id), 'text': text}).encode('utf-8')
    req = urllib.request.Request(
        BRIDGE, data=payload,
        headers={'Content-Type': 'application/json', 'X-MaxBot-Token': TOKEN})
    try:
        with urllib.request.urlopen(req, timeout=15) as r:
            log('SEND chat=%s len=%d rc=%s' % (chat_id, len(text), r.status))
            return r.status == 200
    except Exception as e:
        log('SEND ERR chat=%s: %s' % (chat_id, e))
        return False

def text_of(content):
    if isinstance(content, str):
        return content.strip()
    if isinstance(content, list):
        parts = []
        for c in content:
            if isinstance(c, dict) and c.get('type') == 'text':
                parts.append(str(c.get('text', '')))
        return '\n'.join(p for p in parts if p).strip()
    return ''

def parse_events(con, sid, since_seq, sess_meta):
    """Все user/assistant события после since_seq, по порядку.
    sess_meta: {'chat_id':..., 'chat_name':...} — персистентная память сессии
    (заполняется из user-конвертов, восстанавливается между сканами).
    Возвращает [(seq, role, chat_id, chat_name, text)]."""
    con.row_factory = sqlite3.Row
    out = []
    chat_id = sess_meta.get('chat_id')
    chat_name = sess_meta.get('chat_name')
    for r in con.execute(
            "SELECT seq, event_json FROM transcript_events WHERE session_id=? AND seq>? ORDER BY seq ASC",
            (sid, since_seq)):
        try:
            ev = json.loads(r['event_json'])
        except Exception:
            continue
        msg = ev.get('message')
        if not isinstance(msg, dict):
            continue
        role = msg.get('role')
        if role not in ('user', 'assistant'):
            continue
        if role == 'user':
            content = str(msg.get('content', ''))
            m = re.search(r'chat_id:\s*(-?\d+)', content)
            if m:
                chat_id = int(m.group(1))
                sess_meta['chat_id'] = chat_id
            m2 = re.search(r'Чат:\s*([^\n(]+?)(?:\s*\(chat_id:|$)', content)
            if m2:
                chat_name = m2.group(1).strip()
                sess_meta['chat_name'] = chat_name
        out.append((r['seq'], role, chat_id, chat_name, text_of(msg.get('content'))))
    return out

def prune_state(state):
    sent = state.get('sent', {})
    if len(sent) > KEEP_SESS:
        keep = sorted(sent.items(), key=lambda kv: kv[1])[-KEEP_SESS:]
        state['sent'] = dict(keep)
    # TTL черновиков
    now = time.time()
    drafts = state.get('drafts', {})
    stale = [n for n, d in drafts.items()
             if d.get('status') == 'pending' and now - d.get('ts', 0) > DRAFT_TTL]
    for n in stale:
        drafts[n]['status'] = 'expired'
        log('draft #%s expired (TTL)' % n)

def last_pending(state):
    pend = [(int(n), d) for n, d in state.get('drafts', {}).items()
            if d.get('status') == 'pending']
    if not pend:
        return None, None
    n, d = max(pend, key=lambda kv: kv[0])
    return str(n), d

def create_draft(state, chat_id, chat_name, text):
    n = str(state.get('next_draft_num', 1))
    state['next_draft_num'] = int(n) + 1
    state.setdefault('drafts', {})[n] = {
        'chat_id': chat_id,
        'chat_name': chat_name or str(chat_id),
        'text': text[:3800],
        'status': 'pending',
        'ts': time.time(),
    }
    return n

def handle_approval_command(state, text):
    """Обрабатывает ДА/НЕТ от владельца. Возвращает текст-подтверждение."""
    m = APPROVAL_RE.search(text or '')
    if not m:
        return None
    word = m.group(1).lower()
    num = m.group(2)
    drafts = state.get('drafts', {})

    if num is None:
        num, d = last_pending(state)
        if d is None:
            return 'нет черновиков на утверждении'
    else:
        d = drafts.get(num)
        if d is None:
            return 'черновик #%s не найден' % num

    if d.get('status') != 'pending':
        return 'черновик #%s уже обработан (%s)' % (num, d.get('status'))

    if word == 'нет':
        d['status'] = 'rejected'
        return 'черновик #%s отклонён' % num

    # ДА/ОК → отправка в исходный чат
    ok = send(d['chat_id'], d['text'])
    if ok:
        d['status'] = 'approved'
        return 'черновик #%s отправлен в %s' % (num, d.get('chat_name'))
    else:
        d['status'] = 'failed'
        return 'ОШИБКА отправки черновика #%s — попробуйте ещё раз (ДА %s)' % (num, num)

def scan(con, state):
    con.row_factory = sqlite3.Row
    rows = con.execute(
        """SELECT session_id, session_key, updated_at FROM session_windows
           WHERE session_key LIKE '%hook:maxbot%' AND status IN ('done','timeout')
           ORDER BY updated_at ASC""").fetchall()
    for w in rows:
        sid = w['session_id']
        skey = w['session_key'] or ''
        # Сессия approval-чата: hook:maxbot:<APPROVAL_CHAT_ID>
        is_approval = skey.rstrip(':').endswith(str(APPROVAL_CHAT_ID))

        sent_seq = state.get('sent', {}).get(sid, 0)
        sess_meta = state.setdefault('sess_meta', {}).setdefault(sid, {})
        moved = False
        for seq, role, chat_id, chat_name, text in parse_events(con, sid, sent_seq, sess_meta):
            if role == 'user' and is_approval:
                # Команды владельца: ДА/НЕТ [N]
                reply = handle_approval_command(state, text)
                if reply:
                    log('CMD "%s" → %s' % (text[:40], reply))
                    send(APPROVAL_CHAT_ID, reply)
                state.setdefault('sent', {})[sid] = seq
                moved = True
                continue

            if role == 'assistant' and text:
                if is_approval:
                    # Прямой ответ в чат отладки
                    if send(APPROVAL_CHAT_ID, text):
                        state.setdefault('sent', {})[sid] = seq
                        moved = True
                    else:
                        break  # повтор на следующем цикле
                else:
                    # Мусорный текст агента («молчать», служебные размышления) черновиком
                    # НЕ становится: владельцу нечего решать. Сдвигаем указатель.
                    if is_junk(text):
                        state.setdefault('sent', {})[sid] = seq
                        moved = True
                        log('SKIP junk (chat=%s): %s' % (chat_id, text[:60].replace('\n', ' ')))
                        note_junk(text)
                        continue
                    # Черновик: ответ в рабочий/неизвестный чат
                    if chat_id is None:
                        log('skip seq=%s: chat_id не найден' % seq)
                        state.setdefault('sent', {})[sid] = seq
                        moved = True
                        continue
                    n = create_draft(state, chat_id, chat_name=chat_name, text=text)
                    d = state['drafts'][n]
                    notify = ('Черновик #%s → %s:\n%s\n\nОтветьте: ДА %s — отправить, НЕТ %s — отклонить'
                              % (n, d['chat_name'], d['text'][:1500], n, n))
                    if send(APPROVAL_CHAT_ID, notify):
                        state.setdefault('sent', {})[sid] = seq
                        moved = True
                        log('DRAFT #%s создан (chat=%s, len=%d)' % (n, d['chat_id'], len(d['text'])))
                    else:
                        # уведомление не ушло — не фиксируем seq, черновик остаётся pending,
                        # но чтобы не спамить повторами, пометим seq всё же после удачной попытки
                        break
            else:
                # user-событие в не-approval сессии — просто двигаем указатель
                state.setdefault('sent', {})[sid] = seq
                moved = True

        if moved:
            prune_state(state)
            save_state(state)

def main():
    log('courier v2 started (poll=%ss, approval_chat=%s)' % (POLL, APPROVAL_CHAT_ID))
    state = load_state()
    con = sqlite3.connect('file:%s?mode=ro' % DB, uri=True, timeout=3)
    while True:
        try:
            scan(con, state)
        except Exception as e:
            log('scan err: %s' % e)
            try:
                con.close()
            except Exception:
                pass
            time.sleep(5)
            try:
                con = sqlite3.connect('file:%s?mode=ro' % DB, uri=True, timeout=3)
            except Exception:
                pass
        time.sleep(POLL)

if __name__ == '__main__':
    main()
