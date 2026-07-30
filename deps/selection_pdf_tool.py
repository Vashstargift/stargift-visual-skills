"""selection_pdf_tool — MCP-обвязка генератора PDF-подборок (selection_pdf.py).

Поток: бот выбирает экспонаты (catalog_search) и пишет аргументацию → тул
подтягивает карточки из каталога по product_id, собирает данные слайдов,
рендерит PDF по фирменному шаблону и шлёт файл менеджеру в Telegram.

Правила данных (макет фиксирован, модель заполняет только текст):
  person    = title карточки
  headline  = description карточки (краткое «Футболка … с автографом»)
  price     = price карточки; dimensions — если заполнены
  фото      = photos[0] (общий вид) + photo_details (автограф/деталь/разворот);
              по умолчанию photos[1], если есть
  blurb     = пишет модель (2–4 строки), cert — только если реально известна
"""
from __future__ import annotations

import json
import logging
import re
import tempfile
import urllib.parse
import urllib.request
from pathlib import Path

logger = logging.getLogger(__name__)

_CATALOG_API = "https://stargift.ru/api/catalog-compat.php"


def _fetch_by_ids(product_ids: list) -> dict:
    """Карточки каталога по id → {id: product}."""
    ids = ",".join(str(i).strip() for i in product_ids if str(i).strip())
    url = f"{_CATALOG_API}?page=1&ids={urllib.parse.quote(ids)}"
    req = urllib.request.Request(url, headers={"User-Agent": "stargift-doc-bot"})
    with urllib.request.urlopen(req, timeout=60) as resp:
        data = json.loads(resp.read())
    return {p["id"]: p for p in data.get("products", [])}


def fmt_dimensions(dims) -> str:
    """Размеры каталога → строка для слайда.

    В БД dict {'length','width','height'} в МИЛЛИМЕТРАХ (сайт печатает
    «470 × 50 × 490 мм»); для подборки — сантиметры в стиле образца
    («47 × 5 × 49 см»), нули пропускаются. Строка проходит как есть.
    """
    if not dims:
        return ""
    if isinstance(dims, str):
        return dims.strip()
    if isinstance(dims, dict):
        vals = []
        for key in ("length", "width", "height"):
            try:
                v = float(dims.get(key) or 0)
            except (TypeError, ValueError):
                v = 0
            if v > 0:
                cm = v / 10
                vals.append(str(int(cm)) if cm == int(cm) else f"{cm:.1f}")
        return " × ".join(vals) + " см" if vals else ""
    return ""


_MULTI_SIG_RE = re.compile(r"(\d+)[-\s]*м?я?\s*автограф", re.IGNORECASE)


def default_details(product: dict) -> list:
    """Фото-детали по умолчанию из карточки.

    «с N автографами» → photos[1:N+1] (кропы подписей); иначе photos[1:2]
    (у автографных это кроп подписи, у книг — разворот/деталь).
    """
    photos = product.get("photos") or []
    if len(photos) < 2:
        return []
    m = _MULTI_SIG_RE.search(product.get("description") or "")
    if m:
        n = max(1, min(4, int(m.group(1))))
        return photos[1:1 + n]
    return photos[1:2]


def apply_discount(payload: dict, percent) -> dict:
    """Персональная скидка клиента. На слайде: обычная цена («Цена: …», тихой
    строкой) + главная строка «Цена с учётом вашей скидки: …» (процент НЕ
    пишется). percent — число 1..90; вне диапазона — без изменений."""
    try:
        d = float(percent)
    except (TypeError, ValueError):
        return payload
    if not (0 < d < 91):
        return payload
    for slide in payload.get("items", []):
        try:
            p = int(slide.get("price") or 0)
        except (TypeError, ValueError):
            continue
        if p > 0:
            slide["price_discounted"] = int(round(p * (100 - d) / 100))
    return payload


def build_payload(items: list, products: dict, title: str = "") -> dict:
    """Собрать вход selection_pdf из аргументов тула + карточек каталога.

    items: [{product_id, blurb, cert?, photo_details?}] — из каталога, ЛИБО
    кастомная позиция без product_id: {person, headline?, price?, dimensions?,
    cert?, blurb, photo_product (URL/путь/"last"), photo_details?} — для
    экспонатов, которых нет на сайте (фото менеджер присылает боту).
    Возвращает payload; позиции без карточки и без person пропускаются
    (перечисляются в payload['_skipped']).
    """
    slides, skipped = [], []
    for it in items:
        pid = str(it.get("product_id") or "").strip()
        if not pid and (it.get("person") or "").strip():
            # кастомная позиция — все данные заданы вручную
            slides.append({
                "person": it.get("person") or "",
                "headline": it.get("headline") or "",
                "price": it.get("price"),
                "dimensions": fmt_dimensions(it.get("dimensions")),
                "cert": it.get("cert"),
                "blurb": (it.get("blurb") or "").strip(),
                "photo_product": it.get("photo_product") or "",
                "photo_details": it.get("photo_details") or [],
            })
            continue
        product = products.get(pid)
        if not product:
            skipped.append(pid or "<без id>")
            continue
        photos = product.get("photos") or []
        details = it.get("photo_details") or default_details(product)
        # photo_product у каталожной позиции можно ПЕРЕОПРЕДЕЛИТЬ (локальный путь
        # или URL) — так в подборку встаёт сгенерированное оформление в раме
        # (exhibit_photo_frame) без потери ссылки на карточку (Вашик 30.07).
        main_photo = (it.get("photo_product") or "").strip() or (photos[0] if photos else "")
        slides.append({
            "person": product.get("title") or "",
            "headline": product.get("description") or "",
            "price": product.get("price"),
            "dimensions": fmt_dimensions(product.get("dimensions")),
            "cert": it.get("cert"),
            "blurb": (it.get("blurb") or "").strip(),
            "photo_product": main_photo,
            "photo_details": details,
            "url": f"https://stargift.ru/product/{pid}/",
        })
    return {
        "title": title or "Подборка StarGift",
        "items": slides,
        "_skipped": skipped,
    }


def _send_document(chat_id: str, pdf_path: str, caption: str) -> bool:
    from selection_sender import _telegram_token
    token = _telegram_token()
    if not token:
        logger.error("make_selection_pdf: нет TELEGRAM_BOT_TOKEN")
        return False
    boundary = "----stargiftpdf"
    filename = Path(pdf_path).name
    body = b""
    for name, value in (("chat_id", str(chat_id)), ("caption", caption[:1000])):
        body += (f"--{boundary}\r\nContent-Disposition: form-data; "
                 f"name=\"{name}\"\r\n\r\n{value}\r\n").encode()
    body += (f"--{boundary}\r\nContent-Disposition: form-data; name=\"document\"; "
             f"filename=\"{filename}\"\r\nContent-Type: application/pdf\r\n\r\n").encode()
    body += Path(pdf_path).read_bytes() + f"\r\n--{boundary}--\r\n".encode()
    req = urllib.request.Request(
        f"https://api.telegram.org/bot{token}/sendDocument",
        data=body,
        headers={"Content-Type": f"multipart/form-data; boundary={boundary}"},
    )
    try:
        resp = json.loads(urllib.request.urlopen(req, timeout=120).read())
        return bool(resp.get("ok"))
    except Exception as exc:
        logger.error("make_selection_pdf: sendDocument failed: %s", exc)
        return False


def _resolve_last_photos(items: list) -> str:
    """Заменить photo_product/'last' на путь свежего фото из входящих TG.

    Возвращает текст предупреждения (пустой, если всё разрешилось)."""
    needs = [it for it in items if (it.get("photo_product") or "").strip().lower() == "last"]
    if not needs:
        return ""
    try:
        import interior_fit
        path = interior_fit.latest_incoming_image(set())
    except Exception:
        path = None
    if not path:
        for it in needs:
            it["photo_product"] = ""
        return ("Свежее присланное фото не найдено (пришли фото сообщением и повтори). ")
    for it in needs:
        it["photo_product"] = path
    return ""


def make_and_send(chat_id: str, items: list, title: str = "",
                  discount_percent=0, style_overrides: dict = None) -> str:
    """Главная точка входа для MCP-тула. Возвращает текст-подтверждение."""
    if not items:
        return "Пустой список позиций — нечего собирать."
    warn = _resolve_last_photos(items)
    catalog_ids = [it.get("product_id") for it in items if it.get("product_id")]
    try:
        products = _fetch_by_ids(catalog_ids) if catalog_ids else {}
    except Exception as exc:
        return f"Не удалось получить карточки каталога: {exc}"

    # Мини-тексты лотов пишет Opus по правилам дома (Вашик 24.07): свои blurb'ы
    # Дока выходили сухой энциклопедией и дублировались у лотов одной персоны.
    # Уже написанный менеджером/Доком осмысленный текст (60+ знаков) не трогаем.
    try:
        import lot_texts
        need = [it for it in items
                if it.get("product_id") and len((it.get("blurb") or "").strip()) < 60]
        if need:
            fresh = lot_texts.generate_blurbs(need)
            for it in items:
                pid = str(it.get("product_id") or "")
                if pid in fresh:
                    it["blurb"] = fresh[pid]
    except Exception as exc:
        logger.warning("lot_texts: не удалось сгенерировать тексты: %s", exc)

    payload = apply_discount(build_payload(items, products, title), discount_percent)
    skipped = payload.pop("_skipped")
    if not payload["items"]:
        return f"Ни одна позиция не найдена в каталоге (ids: {', '.join(skipped)})."

    import selection_pdf
    import selection_prefs
    # личные настройки менеджера + разовые правки этого запроса поверх
    prefs = selection_prefs.get_prefs(chat_id)
    if style_overrides:
        clean, _ = selection_prefs.validate(style_overrides)
        prefs.update(clean)
    safe = re.sub(r"[^\w\-]+", "_", (title or "podborka"), flags=re.UNICODE)[:40]
    out = str(Path(tempfile.gettempdir()) / f"stargift_{safe}.pdf")
    try:
        selection_pdf.render_pdf(payload, out, prefs)
    except Exception as exc:
        return f"Ошибка рендера PDF: {exc}"

    caption = payload["title"]
    ok = _send_document(chat_id, out, caption)
    if not ok:
        return "PDF собран, но отправить в Telegram не удалось."

    # Редактируемая версия для менеджеров (Вашик, 20.07.2026): pptx-черновик →
    # родной Keynote (.key) через Keynote.app; если конвертация недоступна — шлём pptx.
    pptx_note = ""
    try:
        import selection_pptx
        pptx_out = Path(tempfile.gettempdir()) / f"stargift_{safe}.pptx"
        selection_pptx.build_pptx(payload, pptx_out, prefs)
        key_out = Path(tempfile.gettempdir()) / f"stargift_{safe}.key"
        if key_out.exists():
            key_out.unlink()
        # Защита от 413: не пытаемся слать редактируемую версию тяжелее 45 МБ —
        # PDF уже ушёл, а из-за неотправки Keynote Док дробил всю презентацию.
        LIMIT = 45 * 1024 * 1024
        # Личный формат менеджера: Настя (28.07) — Keynote подменяет ей шрифт,
        # нужен обычный PPTX без конвертации (pref editable_format).
        want_pptx = prefs.get("editable_format") == "pptx"
        converted = False if want_pptx else selection_pptx.convert_to_key(str(pptx_out), str(key_out))
        if converted and key_out.stat().st_size <= LIMIT and \
                _send_document(chat_id, str(key_out), caption + " — редактируемая версия (Keynote)"):
            pptx_note = " + редактируемый Keynote"
        elif pptx_out.stat().st_size <= LIMIT and \
                _send_document(chat_id, str(pptx_out), caption + " — редактируемая версия (PowerPoint)"):
            pptx_note = " + редактируемый PowerPoint"
        else:
            pptx_note = " (редактируемая версия слишком тяжёлая для отправки — PDF выше полный)"
    except Exception as exc:
        pptx_note = f" (редактируемая версия не собралась: {exc})"

    note = f" (пропущены не найденные в каталоге: {', '.join(skipped)})" if skipped else ""
    return f"{warn}PDF-подборка «{payload['title']}» из {len(payload['items'])} поз. отправлена{pptx_note}{note}."
