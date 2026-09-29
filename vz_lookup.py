#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""MaxBot: сводка по инструменту из трёх баз.

Запуск (обязательно venv-питон контейнера OpenClaw, в нём есть openpyxl):
    /config/clawd/.venv/bin/python /share/kb/bin/vz_lookup.py "пластина CNMG 120408"

Что делает: по строке запроса собирает ТРИ блока фактуры —
  ЗАПАС   — /share/kb/xlsx/Выгрузка ГТМС от 15.09.26.xlsx   (остатки по цехам и ячейкам)
  АНАЛОГИ — /share/kb/xlsx/Артикулы_анализ.xlsx              (по посадке и типу обработки)
  ВЗ      — /share/kb/xlsx/отчет_ВЗ_18.09.2026.xlsx          (номер, автор, вариант, кол-во)

Принципы (приняты владельцем 26.09.2026):
  * Дата отгрузки из базы НЕ публикуется — в ней 76% просроченных значений.
    Печатается ориентировочный срок только для «планировалось на <дата> (будущая),
    требует подтверждения». Основной срок — «уточняется».
  * Каждая цифра сопровождается датой актуальности своей базы.
  * Если позиции нет нигде — выдаётся МАРШРУТ ПОИСКА, а не «вернусь с ответом».
  * Ничего не выдумывается: пусто — значит «в базе нет».

Формат: текстовый отчёт (≤25 строк) + машинный JSON на --json.
"""

from __future__ import annotations

import json
import os
import re
import sys
import time
from datetime import date, datetime

KB = "/share/kb"
XLSX = f"{KB}/xlsx"
CACHE = f"{KB}/cache/vz_index.json"
LOG = f"{KB}/bin/vz_lookup.log"

F_ART = f"{XLSX}/Артикулы_анализ.xlsx"
F_GTMS = f"{XLSX}/Выгрузка ГТМС от 15.09.26.xlsx"
F_VZ = f"{XLSX}/отчет_ВЗ_18.09.2026.xlsx"

# Штампы актуальности берём из имени файла: «...ГТМС от 15.09.26.xlsx», «отчет_ВЗ_18.09.2026.xlsx»
STAMP_GTMS = "15.09.2026"
STAMP_ART = "19.09.2026"
STAMP_VZ = "18.09.2026"

# ── Маршруты поиска: у кого искать, если позиции нет в базе ────────────────────
ROUTES = [
    ("резервы, разовый инструмент, шаблоны МЦ/РЦ/ЦЦ", "Протасова Е.К. (оператор склада)"),
    ("склад 355", "Кукуева С.В. (оператор склада 355)"),
    ("склад 655", "Смирнова Т. (=Жупанова) (склад 655)"),
    ("354: выдача, кладовщик, техрешение", "Савина Е.В. (инженер 354 + кладовщик)"),
    ("инженер 355, аналоги", "Золотарев В.С."),
    ("инженер 354, аналоги", "Марков А."),
    ("инженер 655, аналоги", "Логинов М.Б."),
    ("ВЗ, сроки, оплата, поставщик", "Пушкина (снабжение)"),
    ("выдача/организация выдачи", "Марков А."),
]

# ── Токенизация запроса ────────────────────────────────────────────────────────
RE_SPLIT = re.compile(r"[^\wЀ-ӿ]+", re.UNICODE)
STOP = {
    "пластина", "пластины", "пластин", "резец", "резцы", "фреза", "фрезы", "сверло",
    "сверла", "ключ", "инструмент", "инструмента", "метчик", "державка", "валик",
    "вкладыш", "нож", "пила", "для", "на", "и", "в", "с", "к", "по", "мм", "шт",
    "для", "деталь", "детали", "станок", "станка", "цех", "цеха", "запрос", "прошу",
    "есть", "нет", "нужно", "найти", "подскажите", "подскажи", "вопрос", "please",
}


def norm(s) -> str:
    return re.sub(r"\s+", " ", str(s or "")).strip().lower()


def tokens(q: str) -> list[str]:
    q = norm(q)
    # артикул целиком: цифры+дефис, напр. 9306-2053, 9566-1110-44
    arts = re.findall(r"\d[\d\-\.]{2,}", q)
    # латинские токены с цифрами: CNMG, 120408, GESAC
    lat = re.findall(r"[a-zа-я]{2,}\d[\w\-]*", q)
    lat += re.findall(r"\d+[a-zа-я][\w\-]*", q)
    words = [w for w in RE_SPLIT.split(q) if len(w) >= 3 and w not in STOP and not w.isdigit()]
    out = []
    for t in arts + lat + words:
        t = t.strip("-.")
        if len(t) >= 2 and t not in out:
            out.append(t)
    return out[:12]


def load_xlsx(path: str, sheet: str | None = None) -> list[dict]:
    import openpyxl

    wb = openpyxl.load_workbook(path, read_only=True, data_only=True)
    ws = wb[sheet] if sheet else wb[wb.sheetnames[0]]
    it = ws.iter_rows(values_only=True)
    hdr = [str(h).strip() if h is not None else "" for h in next(it)]
    rows = []
    for r in it:
        if all(v is None for v in r):
            continue
        rows.append({hdr[i]: r[i] for i in range(min(len(hdr), len(r)))})
    wb.close()
    return rows


def build_index() -> dict:
    idx = {"built": datetime.now().isoformat(timespec="seconds"), "art": [], "gtms": [], "vz": []}
    for r in load_xlsx(F_ART):
        idx["art"].append({
            "a": r.get("Артикул"), "d": r.get("Описание"), "t": r.get("Тип обработки"),
            "m": r.get("Тип обрабатываемого материала"), "p": r.get("Посадка"),
            "g": r.get("Аналогичная группа"), "s": r.get("Основной поставщик"),
        })
    for r in load_xlsx(F_GTMS):
        try:
            qty = float(str(r.get("Запас") or 0).replace(",", "."))
        except Exception:
            qty = 0.0
        idx["gtms"].append({
            "sk": r.get("Склад"), "ms": r.get("Место складирования"), "a": r.get("Артикул"),
            "d": r.get("Описание"), "q": qty, "st": r.get("Состояние артикула"),
        })
    for r in load_xlsx(F_VZ):
        try:
            qty = float(str(r.get("Количество") or 0).replace(",", "."))
        except Exception:
            qty = 0.0
        idx["vz"].append({
            "o": r.get("Заказ на внутреннее потребление"), "n": r.get("Номенклатура"),
            "au": r.get("Заказ на внутреннее потребление.Автор"),
            "dt": r.get("Дата отгрузки"), "v": r.get("Вариант обеспечения"), "q": qty,
            "cm": r.get("Заказ на внутреннее потребление.Комментарий"),
        })
    os.makedirs(os.path.dirname(CACHE), exist_ok=True)
    tmp = CACHE + ".tmp"
    with open(tmp, "w", encoding="utf-8") as fh:
        json.dump(idx, fh, ensure_ascii=False)
    os.replace(tmp, CACHE)
    return idx


def get_index() -> dict:
    if os.path.exists(CACHE):
        try:
            with open(CACHE, encoding="utf-8") as fh:
                return json.load(fh)
        except Exception:
            pass
    return build_index()


def hit(text, toks: list[str]) -> int:
    t = norm(text)
    if not t:
        return 0
    sc = 0
    for tok in toks:
        if len(tok) >= 5 and tok in t:
            sc += 3
        elif len(tok) >= 3 and tok in t:
            sc += 2
    return sc


def lookup(idx: dict, q: str) -> dict:
    toks = tokens(q)
    res: dict = {"query": q, "tokens": toks, "art_found": [], "analogs": [],
                 "stock": {}, "vz": [], "planned": [], "stamp": {
                     "gtms": STAMP_GTMS, "art": STAMP_ART, "vz": STAMP_VZ}}

    if not toks:
        return res

    # 1) остатки (ГТМС) — по артикулу или описанию
    stock_rows = []
    for r in idx["gtms"]:
        s = hit(r.get("a"), toks) * 3 + hit(r.get("d"), toks)
        if s > 0:
            stock_rows.append(r)
    by_chat: dict = {}
    for r in stock_rows:
        sk = str(r.get("sk") or "?").strip()
        cell = by_chat.setdefault(sk, {"q": 0.0, "ms": []})
        cell["q"] += r.get("q") or 0.0
        if r.get("q") and r.get("ms") and len(cell["ms"]) < 3:
            cell["ms"].append(f"{r.get('ms')}={int(r['q'])}")
    res["stock"] = {k: v for k, v in sorted(by_chat.items())}

    # 2) внутренние заказы (ВЗ) — по номенклатуре
    today = date.today()
    vz_rows = []
    for r in idx["vz"]:
        s = hit(r.get("n"), toks) * 3 + hit(r.get("cm"), toks)
        if s > 0:
            vz_rows.append(r)
    vz_rows.sort(key=lambda r: -hit(r.get("n"), toks))
    res["vz"] = vz_rows[:6]
    for r in vz_rows[:6]:  # будущие даты — как планировалось, с оговоркой
        d = parse_date(r.get("dt"))
        if d and d >= today:
            res["planned"].append(r)

    # 3) справочник артикулов — по артикулу
    scored = []
    for r in idx["art"]:
        s = hit(r.get("a"), toks) * 3 + hit(r.get("d"), toks) * 2
        if s > 0:
            scored.append((s, r))
    scored.sort(key=lambda x: -x[0])
    res["art_found"] = [r for _, r in scored[:3]]

    # 3b) не нашли по артикулу (CNMG, GESAC — это торговая марка, не артикул):
    #     берём описания из ВЗ/остатков и ищем по ним справочник.
    if not res["art_found"]:
        desc_toks: list[str] = []
        for r in res["vz"][:3] + stock_rows[:3]:
            for tk in tokens(str(r.get("n") or r.get("d") or "")):
                if len(tk) >= 4 and tk not in desc_toks and tk not in toks:
                    desc_toks.append(tk)
        desc_toks = desc_toks[:6]
        if desc_toks:
            scored2 = []
            for r in idx["art"]:
                s = hit(r.get("d"), desc_toks) * 2 + hit(r.get("a"), desc_toks) * 3
                if s >= 3:
                    scored2.append((s, r))
            scored2.sort(key=lambda x: -x[0])
            res["art_found"] = [r for _, r in scored2[:3]]
            res["art_by_desc"] = desc_toks

    # 4) аналоги: по посадке и группе найденной строки
    seen = set()
    for _, r in scored[:2] if not res.get("art_by_desc") else [
            (0, r) for r in res["art_found"]]:
        keys = [k for k in (r.get("p"), r.get("g"), r.get("t")) if k and str(k) != "None"]
        for k in keys:
            k = str(k).strip()
            if len(k) < 3:
                continue
            for a in idx["art"]:
                aid = str(a.get("a") or "")
                if aid in seen or aid == str(r.get("a") or ""):
                    continue
                if hit(a.get("p"), [k]) >= 3 or hit(a.get("g"), [k]) >= 3:
                    seen.add(aid)
                    res["analogs"].append(a)
                    if len(res["analogs"]) >= 8:
                        break
            if len(res["analogs"]) >= 8:
                break
        if len(res["analogs"]) >= 8:
            break
    return res


def parse_date(v):
    s = str(v or "")[:10]
    for f in ("%d.%m.%Y", "%Y-%m-%d", "%d.%m.%y"):
        try:
            return datetime.strptime(s, f).date()
        except Exception:
            pass
    return None


def fmt(res: dict) -> str:
    L: list[str] = []
    q = res.get("query", "")
    L.append(f"ЗАПРОС: {q}")
    L.append(f"ТОКЕНЫ: {', '.join(res.get('tokens') or []) or '—'}")
    st = res.get("stamp", {})

    # --- ЗАПАС ---
    stock = res.get("stock") or {}
    if stock:
        L.append("")
        L.append(f"ЗАПАС (ГТМС, данные на {st.get('gtms')}):")
        for sk, v in stock.items():
            cells = ("; " + ", ".join(v["ms"])) if v.get("ms") else ""
            L.append(f"  {sk}: {v['q']:.0f} шт{cells}")
    else:
        L.append("")
        L.append(f"ЗАПАС (ГТМС на {st.get('gtms')}): в базе нет")

    # --- АНАЛОГИ ---
    an = res.get("analogs") or []
    if an:
        L.append("")
        L.append(f"АНАЛОГИ (справочник на {st.get('art')}) — посадка/группа, требует подтверждения:")
        for a in an[:6]:
            L.append(f"  {a.get('a')} | {str(a.get('d') or '')[:58]} | посадка {a.get('p') or '—'} | тип {a.get('t') or '—'}")
    else:
        L.append("")
        L.append("АНАЛОГИ: по посадке не найдены")

    # --- ВЗ ---
    vz = res.get("vz") or []
    L.append("")
    if vz:
        L.append(f"ВНУТРЕННИЕ ЗАКАЗЫ (отчёт ВЗ на {st.get('vz')}):")
        for r in vz[:5]:
            L.append(f"  {r.get('o')}")
            L.append(f"     номенклатура: {str(r.get('n') or '')[:70]}")
            L.append(f"     автор: {r.get('au') or '—'} | вариант: {r.get('v') or '—'} | кол-во: {r.get('q'):.0f}")
        L.append("  Срок отгрузки: УТОЧНЯЕТСЯ (в базе 76% дат просрочены — не публикуем)")
    else:
        L.append("ВНУТРЕННИЕ ЗАКАЗЫ: по этой позиции не заведено")

    planned = res.get("planned") or []
    if planned:
        L.append("  Планировалось на: " + ", ".join(
            f"{parse_date(p.get('dt'))} (требует подтверждения)" for p in planned[:3]))

    # --- МАРШРУТ ---
    if not stock and not vz:
        L.append("")
        L.append("ПОЗИЦИИ НЕТ НИ В ОСТАТКАХ, НИ В ЗАКАЗАХ → маршрут поиска:")
        for what, who in ROUTES:
            L.append(f"  {what} → {who}")
        L.append("  ВЗ не заведён — потребность не оформлена (сообщить хозяину).")
    return "\n".join(L)


def logline(q: str, res: dict) -> None:
    try:
        os.makedirs(os.path.dirname(LOG), exist_ok=True)
        with open(LOG, "a", encoding="utf-8") as fh:
            fh.write(json.dumps({
                "ts": datetime.now().isoformat(timespec="seconds"), "q": q,
                "stock": list((res.get("stock") or {}).keys()),
                "vz": len(res.get("vz") or []), "an": len(res.get("analogs") or []),
            }, ensure_ascii=False) + "\n")
    except Exception:
        pass


def main() -> int:
    args = [a for a in sys.argv[1:] if not a.startswith("--")]
    as_json = "--json" in sys.argv
    rebuild = "--rebuild" in sys.argv
    if not args:
        print(__doc__)
        return 2
    q = " ".join(args)
    t0 = time.time()
    if rebuild and os.path.exists(CACHE):
        try:
            os.remove(CACHE)
        except Exception:
            pass
    try:
        idx = get_index()
    except Exception as e:
        print(f"ОШИБКА чтения баз: {e}")
        return 1
    res = lookup(idx, q)
    res["elapsed_sec"] = round(time.time() - t0, 1)
    logline(q, res)
    if as_json:
        print(json.dumps(res, ensure_ascii=False, indent=1, default=str))
    else:
        print(fmt(res))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
