"""framing_tool — оформление фото экспоната «в раму» + правки фото по просьбам менеджеров.

Рецепт отработан на футболке Месси 21.07.2026 (см. reference_framing_generation_recipe):
  1. РЕАЛЬНОЕ фото товара НЕ перерисовываем — Higgsfield (nano_banana_2) получает его
     editing-промптом «вещь менять нельзя, только окружение» + референс нашего
     оформления (карточка Мбаппе), шильд просим оставить ПУСТЫМ.
  2. Пустую золотую пластину находим детектором (короткие золотые ряды в нижней
     трети; длинные — это окантовка паспарту, отбрасываются).
  3. Текст шильда гравируем PIL-ом фирменной Proxima Nova (кроп ×6 для чёткости):
     «PERSONALLY SIGNED BY / ИМЯ ЛАТИНИЦЕЙ».

Прямая генерация «нарисуй похожую вещь» ЗАПРЕЩЕНА — перевирает товар.
"""
from __future__ import annotations

import io
import logging
import os
import time
import urllib.request
from pathlib import Path

logger = logging.getLogger(__name__)

FRAMING_REF = "/Users/docbrown/stargift-framing-tests/ref-framing-mbappe.jpg"
STYLES_FILE = "/Users/docbrown/hermes-doc/framing_styles.json"


def approved_styles() -> dict:
    """Утверждённые Вашиком рамы/паспарту — Док НЕ выдумывает оформление."""
    import json
    try:
        return json.load(open(STYLES_FILE))
    except Exception:
        return {"default": "brand", "styles": {}}


def list_styles_text() -> str:
    """Человекочитаемый список рам для ответа менеджеру."""
    st = approved_styles()
    lines = []
    for key, v in st.get("styles", {}).items():
        lines.append(f"• {v['title']} (для: {', '.join(v.get('for', []))})")
    return "\n".join(lines)


def _resolve_style(frame_style: str, style_notes: str) -> tuple:
    """(prompt-описание рамы, путь реф-фото). Рама — ТОЛЬКО из утверждённого
    списка (правило Вашика 30.07: не выдумывать рамы и паспарту)."""
    import os as _os
    st = approved_styles()
    key = (frame_style or "").strip() or st.get("default", "brand")
    v = st.get("styles", {}).get(key) or st.get("styles", {}).get(st.get("default", "brand")) or {}
    prompt = v.get("prompt", "тонкая чёрная рама, тёмно-синее паспарту с золотой окантовкой")
    refs = v.get("refs") or ([v["ref"]] if v.get("ref") else [])
    ref = next((r for r in refs if r and _os.path.exists(r)), FRAMING_REF)
    extra = (style_notes or "").strip()
    if extra:
        prompt = prompt + ". Дополнительно: " + extra
    shield = "silver" if (v.get("shield") == "silver" or "серебр" in extra.lower()) else "gold"
    return prompt, ref, bool(v.get("packshot")), shield
OUT_DIR = Path("/Users/docbrown/stargift-framing-tests/out")

FONT_REGULAR = "/Users/docbrown/Library/Fonts/proximanova_regular.ttf"
FONT_BOLD = "/Users/docbrown/Library/Fonts/proximanova_bold.otf"


def _ensure_hermes_home():
    """MCP-подпроцесс не получает HERMES_HOME, но живёт в каталоге профиля."""
    if os.environ.get("HERMES_HOME"):
        return
    cwd = os.getcwd()
    if "/.hermes/profiles/" in cwd:
        os.environ["HERMES_HOME"] = cwd
    else:
        os.environ["HERMES_HOME"] = os.path.expanduser("~/.hermes/profiles/staff")


def _png_bytes(path: str, maxside: int = 1400) -> bytes:
    from PIL import Image
    im = Image.open(path).convert("RGB")
    im.thumbnail((maxside, maxside), Image.LANCZOS)
    b = io.BytesIO()
    im.save(b, "PNG")
    return b.getvalue()


def _higgsfield_frame(photo_path: str, style_notes: str = "", frame_style: str = "") -> bytes:
    """Оформить РЕАЛЬНОЕ фото в раму (вещь не трогается). → JPEG bytes.
    Рама берётся ТОЛЬКО из утверждённого справочника framing_styles.json."""
    _ensure_hermes_home()
    import sys
    sys.path.insert(0, "/Users/docbrown/hermes-doc")
    import higgsfield_client as hf

    style, ref_path, is_packshot, shield_color = _resolve_style(frame_style, style_notes)
    if is_packshot:
        # Packshot (Вашик 30.07): предмет на некрасивом фоне → нейтральный
        # студийный фон, БЕЗ рамы и таблички. Эталон — бутса CR7.
        prompt = (
            "ЭТО РЕАЛЬНАЯ ФОТОГРАФИЯ ТОВАРА (первый референс) — сам предмет МЕНЯТЬ НЕЛЬЗЯ: "
            "сохрани пиксельно точно все его детали, цвета, надписи, автограф и наклейки. "
            "НИЧЕГО не перерисовывай на предмете. Задача — только ФОН: полностью замени "
            f"окружение на {style} (второй референс показывает пример подачи). "
            "БЕЗ рамы, БЕЗ паспарту, БЕЗ таблички — чистый каталожный packshot анфас.")
    else:
        prompt = (
            "ЭТО РЕАЛЬНАЯ ФОТОГРАФИЯ ТОВАРА (первый референс) — сам предмет МЕНЯТЬ НЕЛЬЗЯ: "
            "сохрани пиксельно точно все его детали, цвета, надписи, автограф и наклейки. "
            "НИЧЕГО не перерисовывай на предмете. Фото/предмет показывай ЦЕЛИКОМ — "
            "НЕ ОБРЕЗАЙ края кадра (инцидент 30.07: кроп фото Пачино-Деппа); окно "
            "паспарту подстрой под пропорции фото. ПЛОСКИЙ экспонат (фотография, "
            "документ, лист с автографом) оформляй как ФОТО В ПАСПАРТУ — плоско, "
            "окно по формату листа; НЕ как объёмный текстиль в глубоком коробе. "
            "Задача — только ОКРУЖЕНИЕ: убери "
            f"посторонние предметы фона и оформи предмет в наше оформление: {style}. "
            "ВТОРОЙ референс показывает ТОЛЬКО СТИЛЬ рамы и паспарту — его содержимое "
            "(предмет, таблички, надписи) НЕ переноси и не копируй. В итоговом "
            "оформлении РОВНО ОДНА табличка: внизу по центру, пустая "
            f"{'СЕРЕБРЯНАЯ' if shield_color == 'silver' else 'золотая'}, "
            "БЕЗ надписей — СТРОГО ПРЯМОУГОЛЬНАЯ с прямыми углами, без фигурных "
            "вырезов и скруглений. Никаких других табличек и надписей на раме. "
            "Каталожное фото анфас."
            # белый внешний фон — только если пожелания не задают свой
            + ("" if "фон" in style_notes.lower() else " Белый внешний фон."))

    m1 = hf.upload_image(_png_bytes(photo_path), "product.png")
    m2 = hf.upload_image(_png_bytes(ref_path), "framing.png")
    params = {"model": "nano_banana_2", "prompt": prompt, "aspect_ratio": "4:3",
              "medias": [{"value": m1, "role": "image"}, {"value": m2, "role": "image"}]}
    struct = hf._call_tool("generate_image", {"params": params})
    job = hf.extract_job_id(struct)
    if not job:
        raise hf.HiggsfieldError(f"generate_image: нет job id в {str(struct)[:200]}")
    for _ in range(hf._POLL_TRIES):
        st = hf._call_tool("job_status", {"jobId": job, "sync": True})
        status, url = hf.extract_generation(st)
        if status == "completed" and url:
            req = urllib.request.Request(url, headers={"User-Agent": "stargift-bot"})
            with urllib.request.urlopen(req, timeout=120) as r:
                return r.read()
        if status in ("failed", "canceled", "nsfw", "ip_detected"):
            raise hf.HiggsfieldError(f"генерация: status={status}")
    raise hf.HiggsfieldError("генерация не завершилась за отведённое время")


def _find_empty_plate(im, zone=(0.20, 0.80, 0.66), plate_color="gold") -> tuple | None:
    """Пустая золотая пластина. zone=(x0, x1, y0) в долях кадра — где искать.
    Окантовка паспарту — длинные золотые ряды на всю ширину — отбрасывается
    фильтром длины ряда. Для композитов зону сужаем до правого-нижнего угла:
    детектор цеплялся за золотую рамку окна сценария (инцидент 30.07)."""
    W, H = im.size
    px = im.load()
    zx0, zx1, zy0 = zone

    def is_gold(c):
        # окно широкое: на тёмном фоне пластина даёт яркие блики (252,226,165)
        r, g, b = c
        if plate_color == "silver":
            # серебро: светлый металлик с низкой насыщенностью (Вашик 30.07)
            return 150 < r < 245 and 150 < g < 245 and 150 < b < 245 and (max(c) - min(c)) < 26
        return 140 < r and 110 < g < 238 and 40 < b < 200 and r > g > b and (r - b) > 45

    rows = {}
    for y in range(int(H * zy0), H):
        run, best, bl, start = 0, 0, None, None
        for x in range(int(W * zx0), int(W * zx1)):
            if is_gold(px[x, y]):
                if start is None:
                    start = x
                run += 1
                if run > best:
                    best, bl = run, (start, x)
            else:
                run, start = 0, None
        # пластина 60–300px; окантовка тянется куда шире
        if 60 <= best <= int(W * 0.30):
            rows[y] = bl
    if len(rows) < 15:
        return None
    # самый длинный вертикально-связный блок строк со стабильной шириной
    ys = sorted(rows)
    blocks, cur = [], [ys[0]]
    for y in ys[1:]:
        if y - cur[-1] <= 2:
            cur.append(y)
        else:
            blocks.append(cur)
            cur = [y]
    blocks.append(cur)
    block = max(blocks, key=len)
    if len(block) < 15:
        return None
    l = min(rows[y][0] for y in block)
    r = max(rows[y][1] for y in block)
    return (l + 2, block[0] + 2, r - 2, block[-1] - 2)


def _engrave_shield(jpeg_bytes: bytes, person_latin: str, zone=(0.20, 0.80, 0.66),
                    plate_box=None, plate_color="gold") -> tuple[bytes, bool]:
    """Гравировка «PERSONALLY SIGNED BY / ИМЯ» на пустой пластине (найденной
    детектором или заданной явно plate_box). → (jpeg, гравировка_удалась)."""
    from PIL import Image, ImageDraw, ImageFont
    im = Image.open(io.BytesIO(jpeg_bytes)).convert("RGB")
    box = tuple(plate_box) if plate_box else _find_empty_plate(im, zone, plate_color)
    if not box:
        return jpeg_bytes, False
    bw, bh = box[2] - box[0], box[3] - box[1]
    SCALE = 6
    plate = im.crop(box).resize((bw * SCALE, bh * SCALE), Image.LANCZOS)
    pw, ph = plate.size
    d = ImageDraw.Draw(plate)
    dark = (52, 46, 36)

    def fit(text, fontpath, target_w, start, tracking_ratio):
        size = start
        while size > 8:
            f = ImageFont.truetype(fontpath, size)
            tr = max(1, int(size * tracking_ratio))
            widths = [d.textbbox((0, 0), ch, font=f)[2] for ch in text]
            total = sum(widths) + tr * (len(text) - 1)
            if total <= target_w:
                return f, tr, widths, total
            size -= 2
        return None

    def draw(text, spec, y):
        f, tr, widths, total = spec
        x = pw // 2 - total // 2
        for ch, w in zip(text, widths):
            d.text((x, y), ch, font=f, fill=dark)
            x += w + tr

    name = person_latin.strip().upper()
    s1 = fit("PERSONALLY SIGNED BY", FONT_REGULAR, int(pw * 0.88), int(ph * 0.28), 0.35)
    s2 = fit(name, FONT_BOLD, int(pw * 0.94), int(ph * 0.52), 0.12)
    if not s1 or not s2:
        return jpeg_bytes, False
    h1, h2 = s1[0].size, s2[0].size
    gap = int(ph * 0.13)
    top = (ph - (h1 + gap + h2)) // 2
    draw("PERSONALLY SIGNED BY", s1, top)
    draw(name, s2, top + h1 + gap)
    im.paste(plate.resize((bw, bh), Image.LANCZOS), (box[0], box[1]))
    out = io.BytesIO()
    im.save(out, "JPEG", quality=92)
    return out.getvalue(), True


# Гибрид (Вашик 30.07): генерация даёт ТОЛЬКО сцену (рама/бархат/окна/свет),
# окна — хромакей-зелёные; реальные фото предмета и постера вклеиваются локально
# попиксельно → тексты и автографы идеальны по построению.
COMPOSITE_PROMPT = (
 "EDIT the FIRST reference photo (a framed cinema composite display on an easel). "
 "KEEP the frame profile and texture, the velvet passepartout, the golden inner "
 "frame around the left window, the thin golden border of the right window, the "
 "golden plaque position and the easel. STRAIGHTEN the display to a PERFECTLY "
 "frontal orthographic view: no tilt, no keystone, no perspective skew. "
 "REPLACE the CONTENT of each of the TWO SEPARATE windows with a SOLID UNIFORM "
 "FLAT PURE BRIGHT GREEN panel (#00FF00): one green panel INSIDE the left golden "
 "frame only, one green panel INSIDE the right window border only. The velvet "
 "between and around the windows must STAY VELVET — nothing else in the scene may "
 "be green. The plaque must be EMPTY without any text. Photorealistic."
)


def _find_green_windows(im):
    """Два зелёных хромакей-окна → (box_left, box_right) в пикселях кадра."""
    W, H = im.size
    small = im.resize((300, int(300 * H / W)))
    sw, sh = small.size
    px = small.load()
    pts = []
    for y in range(sh):
        for x in range(sw):
            r, g, b = px[x, y][:3]
            if g > 130 and g > r + 45 and g > b + 45:
                pts.append((x, y))
    if len(pts) < 200:
        return None, None
    xs = sorted(p[0] for p in pts)
    split = xs[len(xs) // 2]  # медиана по x делит два окна

    def rect(sel):
        """Устойчивый прямоугольник: медианные края зелёных строк — подтёки
        зелёного за окно (инцидент 30.07) не растягивают bbox."""
        if len(sel) < 80: return None
        from statistics import median
        rows = {}
        for x, y in sel:
            a = rows.setdefault(y, [x, x])
            a[0] = min(a[0], x); a[1] = max(a[1], x)
        widths = [b - a + 1 for a, b in rows.values()]
        med_w = median(widths)
        good = {y: ab for y, ab in rows.items() if ab[1] - ab[0] + 1 >= 0.75 * med_w}
        if len(good) < 10: return None
        ys2 = sorted(good)
        l = median(a for a, b in good.values())
        r = median(b for a, b in good.values())
        kx, ky = W / sw, H / sh
        return (int(l * kx), int(ys2[0] * ky), int((r + 1) * kx), int((ys2[-1] + 1) * ky))

    left = rect([p for p in pts if p[0] <= split])
    right = rect([p for p in pts if p[0] > split])
    return left, right


def _paste_into_window(base, box, photo_path):
    """Вклеить реальное фото в окно (cover-кроп по пропорции окна, лёгкий inset)."""
    from PIL import Image, ImageOps
    if not box: return
    l, t, r, b = box
    ins = max(4, (r - l) // 80)
    l, t, r, b = l + ins, t + ins, r - ins, b - ins
    w, h = r - l, b - t
    if w < 20 or h < 20: return
    img = Image.open(photo_path).convert("RGB")
    fitted = ImageOps.fit(img, (w, h), Image.LANCZOS, centering=(0.5, 0.5))
    base.paste(fitted, (l, t))


def frame_cinema_composite(item_path: str, poster_path: str, person_latin: str = "",
                           style_notes: str = "") -> dict:
    """Кино-композит НА ШАБЛОНЕ (Вашик 30.07): фиксированная сцена (рама, бархат,
    золотые окна, мольберт) + реальные фото предмета и постера вклеиваются
    попиксельно + локальная гравировка шильда. Генерации в конвейере НЕТ —
    тексты и автографы идеальны по построению, результат мгновенный.
    Сцена-шаблон обновляется вручную (framing_styles.json → cinema-composite)."""
    import json
    st = approved_styles().get("styles", {}).get("cinema-composite", {})
    tpl = st.get("scene_template", "")
    if not tpl or not os.path.exists(tpl):
        return {"error": "нет сцены-шаблона композита (framing_styles.json → cinema-composite.scene_template)"}
    from PIL import Image
    base = Image.open(tpl).convert("RGB")
    _paste_into_window(base, tuple(st.get("win_left", ())), item_path)
    _paste_into_window(base, tuple(st.get("win_right", ())), poster_path)
    buf = io.BytesIO()
    base.save(buf, "JPEG", quality=92)
    raw = buf.getvalue()
    final, engraved = (_engrave_shield(raw, person_latin, plate_box=st.get("plate_box"))
                       if person_latin.strip() else (raw, False))
    OUT_DIR.mkdir(parents=True, exist_ok=True)
    stamp = time.strftime("%Y%m%d-%H%M%S")
    out = OUT_DIR / f"framed-{stamp}.jpg"
    out.write_bytes(final)
    (OUT_DIR / f"framed-{stamp}.json").write_text(json.dumps(
        {"source": item_path, "poster": poster_path, "person": person_latin,
         "notes": style_notes, "frame_style": "cinema-composite"}, ensure_ascii=False))
    return {"path": str(out), "shield_engraved": engraved}


def _auto_crop_sheet(path):
    """Вырезать сам экспонат (светлый лист/фото) со снимка на тёмном столе.
    Менеджеры фотографируют экспонат на поверхности — без этого фон стола
    попадает внутрь рамы (инцидент 31.07, фото Фалдо от Насти).
    Возвращает PIL.Image (исходник, если светлой области не нашлось)."""
    from PIL import Image
    im = Image.open(path).convert("RGB")
    W, H = im.size
    small = im.resize((240, max(1, int(240 * H / W))))
    sw, sh = small.size
    px = small.load()
    # порог: экспонат заметно светлее фона стола
    vals = sorted(sum(px[x, y]) / 3 for y in range(sh) for x in range(sw))
    if not vals:
        return im
    dark, light = vals[len(vals) // 10], vals[-len(vals) // 10]
    if light - dark < 55:          # однородный кадр — резать нечего
        return im
    thr = dark + (light - dark) * 0.55
    xs, ys = [], []
    for y in range(sh):
        for x in range(sw):
            if sum(px[x, y]) / 3 > thr:
                xs.append(x); ys.append(y)
    if len(xs) < 200:
        return im
    xs.sort(); ys.sort()
    lo = len(xs) // 100                     # отбрасываем блики-выбросы
    kx, ky = W / sw, H / sh
    box = (max(0, int(xs[lo] * kx) - 4), max(0, int(ys[lo] * ky) - 4),
           min(W, int(xs[-1 - lo] * kx) + 4), min(H, int(ys[-1 - lo] * ky) + 4))
    if (box[2] - box[0]) < W * 0.25 or (box[3] - box[1]) < H * 0.25:
        return im
    return im.crop(box)


def latest_render() -> str | None:
    """Путь к самому свежему сгенерированному оформлению (для «поставь последнее»)."""
    jpgs = sorted(OUT_DIR.glob("framed-*.jpg"), key=lambda f: f.stat().st_mtime)
    return str(jpgs[-1]) if jpgs else None


def _last_meta() -> dict | None:
    """Метаданные последней генерации (для правок реплаем: «на чёрном фоне»)."""
    import json
    metas = sorted(OUT_DIR.glob("framed-*.json"))
    if not metas:
        return None
    try:
        return json.loads(metas[-1].read_text())
    except Exception:
        return None


def frame_exhibit_photo(photo_path: str, person_latin: str, style_notes: str = "", frame_style: str = "") -> dict:
    """Полный пайплайн: фото товара → оформление в раму + гравировка шильда.

    photo_path="last" — правка ПОСЛЕДНЕЙ генерации: берём её ИСХОДНОЕ фото товара
    (не рендер — повторная прогонка рендера деградирует) и склеиваем пожелания.

    → {"path": сохранённый jpg, "shield_engraved": bool}
    """
    import json
    if photo_path == "last":
        meta = _last_meta()
        if not meta:
            return {"error": "предыдущих генераций не нашёл — нужно фото товара"}
        photo_path = meta["source"]
        person_latin = person_latin or meta.get("person", "")
        frame_style = frame_style or meta.get("frame_style", "")
        prev = meta.get("notes", "")
        style_notes = (prev + ". " + style_notes).strip(". ") if prev else style_notes
    _cfg0 = approved_styles().get("styles", {}).get((frame_style or "").strip(), {})
    if _cfg0.get("scene_template") and _cfg0.get("win_left"):
        # ШАБЛОННАЯ сборка (как кино-композит): сцена фиксированная, фото
        # вклеивается локально contain-ом в окно, шильд гравируется по plate_box.
        # Мгновенно, бесплатно, без генераторных фантазий (glass-float 31.07).
        import json as _json
        from PIL import Image as _Img
        base = _Img.open(_cfg0["scene_template"]).convert("RGB")
        # вырезаем сам экспонат: менеджеры снимают лист на тёмном столе, иначе
        # фон стола попадает внутрь рамы (инцидент 31.07, фото Фалдо)
        img = _auto_crop_sheet(photo_path)
        l, t, r, b = _cfg0["win_left"]
        w, h = r - l, b - t
        fill = float(_cfg0.get("win_fill") or 0.82)   # фото «парит», вокруг стекло
        iw, ih = img.size
        k = min(w * fill / iw, h * fill / ih)
        nw, nh = int(iw * k), int(ih * k)
        img = img.resize((nw, nh), _Img.LANCZOS)
        px, py = l + (w - nw) // 2, t + (h - nh) // 2
        base.paste(_Img.new("RGB", (nw + 10, nh + 10), (178, 178, 178)), (px - 5, py - 5))
        base.paste(img, (px, py))
        buf = io.BytesIO()
        base.save(buf, "JPEG", quality=92)
        raw = buf.getvalue()
        _pc0 = "silver" if _cfg0.get("shield") == "silver" else "gold"
        final, engraved = (_engrave_shield(raw, person_latin, plate_box=_cfg0.get("plate_box"),
                                           plate_color=_pc0)
                           if person_latin.strip() else (raw, False))
        OUT_DIR.mkdir(parents=True, exist_ok=True)
        stamp = time.strftime("%Y%m%d-%H%M%S")
        out = OUT_DIR / f"framed-{stamp}.jpg"
        out.write_bytes(final)
        (OUT_DIR / f"framed-{stamp}.json").write_text(_json.dumps(
            {"source": photo_path, "person": person_latin, "notes": style_notes,
             "frame_style": frame_style}, ensure_ascii=False))
        return {"path": str(out), "shield_engraved": engraved}
    raw = _higgsfield_frame(photo_path, style_notes, frame_style)
    _ps = approved_styles().get("styles", {}).get((frame_style or "").strip(), {}).get("packshot")
    # Зона пластины — только нижняя полоса паспарту: тёплые тона на самом фото
    # (пиджак Пачино, 30.07) проходили золотой фильтр и гравировка ложилась на кадр.
    # Стиль может задать свою зону (plate_zone в framing_styles.json) — например,
    # без паспарту шильд на нижней ПЛАНКЕ рамы (y от 0.90).
    _cfg = approved_styles().get("styles", {}).get((frame_style or "").strip(), {})
    _zone = tuple(_cfg.get("plate_zone") or (0.25, 0.75, 0.78))
    _pc = "silver" if (_cfg.get("shield") == "silver" or "серебр" in (style_notes or "").lower()) else "gold"
    final, engraved = (_engrave_shield(raw, person_latin, zone=_zone, plate_color=_pc)
                       if person_latin.strip() and not _ps else (raw, False))
    # Трим пустых полей студийного фона вокруг рамы (30.07: из-за полей фото
    # на мультислайдах выглядели разномасштабными). Packshot не тримим.
    if not _ps:
        try:
            from PIL import Image as _I, ImageChops as _IC
            _im = _I.open(io.BytesIO(final)).convert("RGB")
            _bg = _I.new("RGB", _im.size, _im.getpixel((3, 3)))
            _bbox = _IC.difference(_im, _bg).point(lambda x: 255 if x > 18 else 0).getbbox()
            if _bbox:
                _pad = max(8, _im.size[0] // 60)
                _bbox = (max(0, _bbox[0]-_pad), max(0, _bbox[1]-_pad),
                         min(_im.size[0], _bbox[2]+_pad), min(_im.size[1], _bbox[3]+_pad))
                _buf = io.BytesIO()
                _im.crop(_bbox).save(_buf, "JPEG", quality=92)
                final = _buf.getvalue()
        except Exception:
            pass
    OUT_DIR.mkdir(parents=True, exist_ok=True)
    stamp = time.strftime("%Y%m%d-%H%M%S")
    out = OUT_DIR / f"framed-{stamp}.jpg"
    out.write_bytes(final)
    (OUT_DIR / f"framed-{stamp}.json").write_text(json.dumps(
        {"source": photo_path, "person": person_latin, "notes": style_notes,
         "frame_style": frame_style},
        ensure_ascii=False))
    return {"path": str(out), "shield_engraved": engraved}


# ---------------------------------------------------------------------------
# Отправка фото в чат от имени бота ТЕКУЩЕГО профиля (не только staff)
# ---------------------------------------------------------------------------

def _bot_token() -> str:
    _ensure_hermes_home()
    env_path = os.path.join(os.environ["HERMES_HOME"], ".env")
    try:
        with open(env_path, encoding="utf-8") as fh:
            for line in fh:
                if line.startswith("TELEGRAM_BOT_TOKEN="):
                    return line.split("=", 1)[1].strip()
    except Exception:
        pass
    return ""


def send_photo(chat_id: str, photo_path: str, caption: str = "") -> bool:
    token = _bot_token()
    if not token:
        return False
    boundary = "----stargiftframing"
    data = Path(photo_path).read_bytes()
    body = io.BytesIO()

    def field(name, value):
        body.write(f"--{boundary}\r\nContent-Disposition: form-data; name=\"{name}\"\r\n\r\n{value}\r\n".encode())

    field("chat_id", str(chat_id))
    if caption:
        field("caption", caption)
    body.write(f"--{boundary}\r\nContent-Disposition: form-data; name=\"photo\"; "
               f"filename=\"framed.jpg\"\r\nContent-Type: image/jpeg\r\n\r\n".encode())
    body.write(data)
    body.write(f"\r\n--{boundary}--\r\n".encode())
    req = urllib.request.Request(
        f"https://api.telegram.org/bot{token}/sendPhoto",
        data=body.getvalue(),
        headers={"Content-Type": f"multipart/form-data; boundary={boundary}"})
    try:
        with urllib.request.urlopen(req, timeout=60) as r:
            import json
            ok = bool(json.load(r).get("ok"))
    except Exception as exc:
        logger.error("framing send_photo failed: %s", exc)
        return False
    if ok:
        _remember_in_transcript(chat_id, photo_path, caption)
    return ok


def _remember_in_transcript(chat_id: str, photo_path: str, caption: str):
    """Вписать отправленное фото в ПАМЯТЬ ДИАЛОГА бота — иначе бот «не видит»,
    что сам отправил (фото уходит Bot API мимо его сессии), и на реплаи менеджера
    отвечает «не вижу фото». Best effort: сбой записи не ломает отправку."""
    try:
        import json as _json
        import sqlite3
        import time as _time
        home = os.environ.get("HERMES_HOME", "")
        sess_file = os.path.join(home, "sessions", "sessions.json")
        data = _json.load(open(sess_file))
        entry = data.get(f"agent:main:telegram:dm:{chat_id}")
        if not entry:
            return
        sid = entry.get("session_id")
        if not sid:
            return
        db = sqlite3.connect(os.path.join(home, "state.db"), timeout=10)
        db.execute(
            "INSERT INTO messages (session_id, role, content, timestamp, active) VALUES (?,?,?,?,1)",
            (sid, "assistant",
             f"[Отправил в чат фото оформления: {photo_path}. Подпись: {caption or '—'}. "
             f"Правки к нему — exhibit_photo_frame(photo_path=\"last\"); в карточку — "
             f"catalog_photo_update(photo_path=\"last\").]",
             _time.time()))
        db.commit()
        db.close()
    except Exception as exc:
        logger.warning("transcript memo failed: %s", exc)
