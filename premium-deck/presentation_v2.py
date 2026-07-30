"""Премиальная презентация по эталону v2 (Культовое кино) для ТГ-бота.

Источник шаблонов — ЕДИНЫЙ: ~/relictum/17_stargift/constructor.html (SG_API).
Схема: данные (CSV-выгрузка сайта или items от бота) → headless-конструктор
строит standalone HTML → Chromium печатает PDF 1920×1080 → _send_document.

Зависимости: playwright (system python3), Chromium playwright'а.
"""
from __future__ import annotations

import csv as _csv
import io
import json
import logging
import os
import re
import tempfile
import time
from pathlib import Path

logger = logging.getLogger("presentation_v2")

CONSTRUCTOR = os.path.expanduser("~/relictum/17_stargift/constructor.html")
# состояние последних презентаций по чатам — чтобы «поправь слайд 4» работало
# в новой сессии бота (get_last_presentation в crm_server)
STATE_DIR = Path(os.path.expanduser("~/.hermes/profiles/staff/workspace/presentations"))


# ---------- входящие CSV из Telegram ----------

def _document_dirs() -> list:
    home = os.environ.get("HERMES_HOME", "").rstrip("/")
    cands = []
    if home:
        cands += [os.path.join(home, "cache", "documents"),
                  os.path.join(home, "document_cache")]
    cwd = os.getcwd()
    if "/.hermes/profiles/" in cwd + "/":
        cands += [os.path.join(cwd, "cache", "documents"),
                  os.path.join(cwd, "document_cache")]
    cands += [os.path.expanduser("~/.hermes/profiles/director/cache/documents"),
              os.path.expanduser("~/.hermes/profiles/staff/cache/documents")]
    return [c for c in cands if os.path.isdir(c)]


def latest_incoming_csv(max_age: float = 3600.0) -> str | None:
    """Свежий присланный боту .csv (не старше часа) или None."""
    best, best_m = None, 0.0
    now = time.time()
    for d in _document_dirs():
        try:
            for f in os.listdir(d):
                if not f.lower().endswith(".csv"):
                    continue
                p = os.path.join(d, f)
                m = os.path.getmtime(p)
                if now - m <= max_age and m > best_m:
                    best, best_m = p, m
        except Exception:
            continue
    return best


def _norm_img(src: str) -> str:
    """Нормализовать фото лота: https-URL как есть (eBay s-l500→s-l1600),
    'last' → свежее присланное боту фото, локальный путь → file:// URI
    (браузер-рендер и PPTX-сборщик оба понимают file://)."""
    src = (src or "").strip()
    if src.lower() == "last":
        try:
            import interior_fit
            p = interior_fit.latest_incoming_image(set())
        except Exception:
            p = None
        return ("file://" + p) if p else ""
    if src.startswith("/") and os.path.isfile(src):
        return "file://" + src
    if src.startswith("file://"):
        return src
    return src.replace("s-l500", "s-l1600")


# ---------- разбор CSV-выгрузки сайта ----------

_COLMAP = {
    "название": "name", "краткое описание": "desc", "цена": "price",
    "в наличии": "instock", "url на сайте": "url", "url главного фото": "img",
}


def parse_catalog_csv(text: str) -> list:
    """CSV сайта (';', utf-8-sig) → лоты конструктора."""
    text = text.lstrip("﻿")
    rows = list(_csv.DictReader(io.StringIO(text), delimiter=";"))
    lots = []
    for r in rows:
        row = { _COLMAP.get((k or "").strip().lower(), k): (v or "").strip()
                for k, v in r.items() }
        name = row.get("name", "")
        if not name:
            continue
        img = row.get("img", "")
        img = img.replace("s-l500", "s-l1600")  # eBay: всегда крупное фото
        price = re.sub(r"[^\d]", "", row.get("price", "")) or "0"
        instock = row.get("instock", "").lower() in ("да", "1", "true", "есть", "yes")
        # раздел: подписант до « — », иначе первое слово-пара
        group = name.split("—")[0].strip() if "—" in name else "Собрание"
        lots.append({
            "name": name,
            "desc": row.get("desc", ""),
            "price": int(price),
            "cert": "",
            "img": img,
            "group": group,
            "instock": instock,
            "url": row.get("url", ""),
        })
    return lots


# ---------- сборка HTML через конструктор (единый источник шаблонов) ----------

def _run_off_loop(fn, *args, **kw):
    """Playwright Sync API падает внутри asyncio-цикла (MCP-сервер гоняет тулы в
    цикле) — при живом цикле выполняем в отдельном потоке, иначе напрямую."""
    import asyncio
    try:
        asyncio.get_running_loop()
    except RuntimeError:
        return fn(*args, **kw)
    import concurrent.futures
    with concurrent.futures.ThreadPoolExecutor(max_workers=1) as ex:
        return ex.submit(fn, *args, **kw).result()


def build_html(theme: dict, lots: list) -> str:
    html, _ = build_html_and_slides(theme, lots)
    return html


def build_html_and_slides(theme: dict, lots: list, sections: list = None,
                          omit_slides: list = None):
    """standalone HTML + разрешённые слайды (для PPTX) одной сессией конструктора.
    omit_slides — список служебных слайдов, которые НЕ включать:
    "contents" (состав), "instock" (в наличии), "index" (полный перечень),
    "contacts" (последний слайд)."""
    from playwright.sync_api import sync_playwright
    if not os.path.isfile(CONSTRUCTOR):
        raise RuntimeError("Конструктор не найден: " + CONSTRUCTOR)
    data = {"theme": theme, "lots": lots}
    if sections:
        data["sections"] = sections
    if omit_slides:
        drop = {str(s).strip().lower() for s in omit_slides}
        opts = {}
        for name in ("contents", "instock", "index", "contacts", "framing", "delivery"):
            if name in drop:
                opts[name] = False
        if opts:
            data["options"] = opts
    with sync_playwright() as p:
        b = p.chromium.launch()
        try:
            pg = b.new_page(viewport={"width": 1920, "height": 1080})
            pg.goto("file://" + CONSTRUCTOR)
            pg.wait_for_timeout(1200)
            has_api = pg.evaluate("typeof window.SG_API==='object' && typeof window.SG_API.build==='function'")
            if not has_api:
                raise RuntimeError("В конструкторе нет SG_API.build — обнови constructor.html")
            html = pg.evaluate("data => window.SG_API.build(data)", data)
            if not html or "<section" not in html:
                raise RuntimeError("SG_API.build вернул пустой результат")
            resolved = None
            try:
                # resolveSlides читает состояние UI — применяем данные как ИИ-ответ
                if pg.evaluate("typeof window.SG_API.resolveSlides==='function' && typeof applyAIResult==='function'"):
                    pg.evaluate("data => applyAIResult(data)", data)
                    pg.wait_for_timeout(600)
                    resolved = pg.evaluate("() => window.SG_API.resolveSlides()")
            except Exception:
                resolved = None
            return html, resolved
        finally:
            b.close()


def render_pdf(html: str, out_pdf: str) -> None:
    from playwright.sync_api import sync_playwright
    tmp = Path(tempfile.gettempdir()) / ("sg_deck_%d.html" % os.getpid())
    tmp.write_text(html, encoding="utf-8")
    try:
        with sync_playwright() as p:
            b = p.chromium.launch()
            try:
                pg = b.new_page(viewport={"width": 1920, "height": 1080})
                pg.goto("file://" + str(tmp))
                # дождаться сетевых фото (CSV-режим тянет фото с сайта)
                try:
                    pg.wait_for_load_state("networkidle", timeout=45000)
                except Exception:
                    pass
                pg.wait_for_timeout(1500)
                pg.emulate_media(media="print")
                pg.pdf(path=out_pdf, width="1920px", height="1080px",
                       print_background=True)
            finally:
                b.close()
    finally:
        try:
            tmp.unlink()
        except Exception:
            pass


# ---------- точка входа для MCP-тула ----------

def make_and_send(chat_id: str, title: str, csv_text: str = "", items: list = None,
                  kicker: str = "", subtitle: str = "", cover_url: str = "",
                  sections: list = None, cover_credit: str = "",
                  omit_slides: list = None,
                  manager_name: str = "", manager_contact: str = "",
                  mood_imgs: list = None, mood_lots_bg: str = "",
                  mood_title: str = "", manifest: str = "") -> str:
    if csv_text == "last":
        p = latest_incoming_csv()
        if not p:
            return ("Свежий CSV-файл не найден (пришли выгрузку .csv сообщением "
                    "и повтори).")
        csv_text = Path(p).read_text(encoding="utf-8-sig", errors="replace")

    lots = []
    if csv_text:
        try:
            lots = parse_catalog_csv(csv_text)
        except Exception as exc:
            return f"Не удалось разобрать CSV: {exc}"
    if items:
        for it in items:
            lot = {
                "name": it.get("name", ""),
                "size": it.get("size", ""),
                "desc": it.get("desc", "") or it.get("blurb", ""),
                "price": int(it.get("price") or 0),
                "cert": it.get("cert", ""),
                "img": _norm_img((it.get("img") or it.get("photo_url") or "")),
                "group": it.get("group", "") or "Собрание",
                "instock": bool(it.get("instock")),
                "gallery": it.get("gallery", ""),
                "url": it.get("url", ""),
            }
            # двухуровневая подпись: имя подписанта крупно + краткий тип подзаголовком
            if "person" in it:
                lot["person"] = it.get("person", "")
                lot["type"] = it.get("type", "")
            lots.append(lot)
    lots = [l for l in lots if l["name"]]
    if not lots:
        return "Нет ни одного лота: передай csv_text (или csv_text='last') либо items."

    theme = {
        "title": title or "Подборка Stargift",
        "kicker": (kicker or "ПОДБОРКА STARGIFT").upper(),
        "sub": subtitle or "",
        "cover": cover_url or (lots[0]["img"] if lots else ""),
        "credit": cover_credit or "",
        "managerName": manager_name or "",
        "managerContact": manager_contact or "",
        # архивные кадры для «кадров настроения» и фона «кадра-метафоры»
        "moodImgs": list(mood_imgs or []),
        "moodLotsBg": mood_lots_bg or "",
        "moodTitle": mood_title or "",
        "moodCredit": cover_credit or "",
        "manifest": manifest or "",
    }
    try:
        html, resolved = _run_off_loop(build_html_and_slides, theme, lots,
                                       sections=sections, omit_slides=omit_slides)
    except Exception as exc:
        logger.exception("presentation_v2 build failed")
        return f"Ошибка сборки презентации: {exc}"

    safe = re.sub(r"[^\w\-]+", "_", title or "preza", flags=re.UNICODE)[:40]
    out_pdf = str(Path(tempfile.gettempdir()) / f"stargift_v2_{safe}.pdf")
    try:
        _run_off_loop(render_pdf, html, out_pdf)
    except Exception as exc:
        logger.exception("presentation_v2 pdf failed")
        return f"HTML собран, но PDF не отрендерился: {exc}"

    from selection_pdf_tool import _send_document
    ok = _send_document(str(chat_id), out_pdf, title or "Презентация Stargift")
    if not ok:
        return "Презентация собрана, но отправить в Telegram не удалось."

    # редактируемая версия: PPTX (+ родной Keynote, если конвертация доступна)
    edit_note = ""
    if resolved and resolved.get("slides"):
        try:
            import sys as _sys
            _sys.path.insert(0, os.path.join(os.path.dirname(CONSTRUCTOR), "tools"))
            import presentation_pptx
            out_pptx = str(Path(tempfile.gettempdir()) / f"stargift_v2_{safe}.pptx")
            presentation_pptx.build_pptx(resolved, out_pptx)
            sent_key = False
            try:
                import selection_pptx
                out_key = str(Path(tempfile.gettempdir()) / f"stargift_v2_{safe}.key")
                if Path(out_key).exists():
                    Path(out_key).unlink()
                if selection_pptx.convert_to_key(out_pptx, out_key) and \
                        _send_document(str(chat_id), out_key, (title or "Презентация") + " — редактируемая версия (Keynote)"):
                    sent_key = True
                    edit_note = " + редактируемый Keynote"
            except Exception:
                pass
            if not sent_key and _send_document(str(chat_id), out_pptx, (title or "Презентация") + " — редактируемая версия (PowerPoint)"):
                edit_note = " + редактируемый PowerPoint"
        except Exception as exc:
            logger.exception("presentation_v2 pptx failed")
            edit_note = f" (редактируемая версия не собралась: {str(exc)[:80]})"

    try:
        _save_state(chat_id, theme, sections, lots, resolved, omit_slides)
    except Exception:
        logger.exception("presentation_v2: не сохранилось состояние")

    n_groups = len({l['group'] for l in lots})
    total = sum(l["price"] for l in lots)
    return (f"Премиальная презентация «{theme['title']}» отправлена: {len(lots)} лотов, "
            f"{n_groups} раздел(а), сумма {total:,} ₽".replace(",", " ") + edit_note + ".")


def _slides_summary(resolved) -> list:
    """Человекочитаемая карта слайдов: номер, тип, заголовок, лоты."""
    out = []
    for i, s in enumerate((resolved or {}).get("slides") or []):
        p = s.get("params") or {}
        names = [(l.get("person") or l.get("name") or "?")
                 for l in (s.get("lots") or []) if l]
        out.append({"slide": i + 1, "type": s.get("type", ""),
                    "heading": p.get("heading") or p.get("title") or "",
                    "lots": names})
    return out


def _save_state(chat_id, theme, sections, lots, resolved, omit_slides=None) -> None:
    STATE_DIR.mkdir(parents=True, exist_ok=True)
    state = {
        "saved_at": time.strftime("%Y-%m-%d %H:%M"),
        "theme": theme, "sections": sections or [], "lots": lots,
        "omit_slides": list(omit_slides or []),
        "slides_summary": _slides_summary(resolved),
    }
    blob = json.dumps(state, ensure_ascii=False, indent=1)
    (STATE_DIR / f"{chat_id}_last.json").write_text(blob, encoding="utf-8")
    # история — последние 10 на чат
    safe = re.sub(r"[^\w\-]+", "_", theme.get("title", "preza"), flags=re.UNICODE)[:40]
    (STATE_DIR / f"{chat_id}_{time.strftime('%Y%m%d-%H%M%S')}_{safe}.json").write_text(
        blob, encoding="utf-8")
    hist = sorted(STATE_DIR.glob(f"{chat_id}_2*.json"))
    for old in hist[:-10]:
        old.unlink(missing_ok=True)


def load_last_state(chat_id: str) -> dict | None:
    p = STATE_DIR / f"{str(chat_id)}_last.json"
    if not p.is_file():
        return None
    return json.loads(p.read_text(encoding="utf-8"))
