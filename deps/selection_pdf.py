"""selection_pdf — генератор PDF-подборки по фирменному шаблону StarGift.

Воспроизводит макет Keynote-подборки (1920×1080, слайд на экспонат +
финальный слайд-оффер). Модель/вызывающий код заполняет ТОЛЬКО данные
(JSON), макет фиксирован — придумать ничего нельзя.

Варианты товарного слайда (выбираются автоматически по данным):
  A  photo + 1 автограф   — экспонат сверху, автограф снизу (как в образце)
  B  photo без автографа  — одно фото экспоната на всю левую колонку (книги и т.п.)
  C  photo + 2+ автографов — экспонат сверху, ряд автографов снизу

Вход (JSON):
{
  "title": "Подборка — футбол",              # необязателен (метаданные PDF)
  "manager_name": "",                        # необязателен, для финального слайда
  "items": [{
     "person":   "Зинедин Зидан",            # заголовок, часть 1
     "headline": "футболка сборной Франции с автографом",  # часть 2
     "price":    380000,                      # int | null (null/0 — цена не печатается)
     "cert":     "Beckett",                   # null|""|строка ("" -> строка сертификации без бренда)
     "blurb":    "…аргументация…",
     "photo_product": "https://… | /абс/путь", # общий вид экспоната
     "photo_signatures": ["…", "…"]           # 0..4 фото автографов
  }]
}

Рендер: Chrome headless --print-to-pdf (страница 1920×1080 через @page).
CLI:  python selection_pdf.py data.json out.pdf
"""
from __future__ import annotations

import json
import os
import subprocess
import sys
import tempfile
import urllib.request
from pathlib import Path

ASSETS = Path(__file__).parent / "selection_pdf_assets"
CHROME = "/Applications/Google Chrome.app/Contents/MacOS/Google Chrome"

FINAL_SLIDE_TEXT = (
    "Буду рада сделать для вас индивидуальную подборку экспонатов — "
    "достаточно сообщить мне примерный бюджет и интересы человека, "
    "для которого нужен подарок",
    "Оформление на Ваш вкус, подарочная упаковка, доставка по Москве "
    "и помощь в организации доставки по миру входят в стоимость",
)

CSS = """
@page { size: 1920px 1440px; margin: 0; } /* 4:3 (Вашик, 20.07.2026) */
* { margin: 0; padding: 0; box-sizing: border-box; }
body { font-family: "Proxima Nova", "Helvetica Neue", Helvetica, Arial, sans-serif; }
.slide {
  position: relative; width: 1920px; height: 1440px;
  overflow: hidden; background: #fff; page-break-after: always;
}
.slide:last-child { page-break-after: auto; }

/* — левая фото-колонка: оба фото ОДНОГО размера, тонкая золотая рамка — */
.frame { border: 3px solid #9a8253; background: #fff; }
.photo-top {
  position: absolute; left: 60px; top: 60px; width: 853px; height: 640px;
  display: flex; align-items: center; justify-content: center; overflow: hidden;
}
.photo-top img { max-width: 100%; max-height: 100%; object-fit: contain; }
.photo-bottom {
  position: absolute; left: 60px; top: 740px; width: 853px; height: 640px;
  overflow: hidden;
}
.photo-bottom img { width: 100%; height: 100%; object-fit: cover; }
/* вариант B: одно фото на всю колонку — рамка на самом фото, без пустот */
.photo-full {
  position: absolute; left: 60px; top: 60px; width: 853px; height: 1320px;
  display: flex; align-items: center; justify-content: center;
}
.photo-full img {
  max-width: 100%; max-height: 100%; object-fit: contain;
  border: 3px solid #9a8253; background: #fff; box-sizing: border-box;
}
/* вариант C: квадраты деталей/автографов без обрезки, центр зоны нижнего фото */
.sig-row {
  position: absolute; left: 60px; top: 740px; width: 853px; height: 640px;
  display: flex; gap: 18px; align-items: center; justify-content: center;
  align-content: center; flex-wrap: wrap;
}
.sig-row .sig { flex: 0 0 auto; overflow: hidden; }
.sig-row .sig img { width: 100%; height: 100%; object-fit: cover; display: block; }

/* — лого: над текстовой колонкой (форматирование образца №2) — */
/* лого: по центру текстовой колонки, крупное — как в эталоне (Вашик, 20.07) */
.logo { position: absolute; left: 1251px; top: 70px; width: 330px; }
.logo img { width: 100%; }

/* — текстовая колонка (образец №2: Крейг/Казино Рояль) — */
/* ширина: правое поле = левому полю фото (60px) → 853..1860 */
.textcol { position: absolute; left: 973px; top: 430px; width: 887px; }
.name {
  font-weight: 700; font-size: 48px; line-height: 1.3; color: #000;
}
.rule {
  width: 130px; border-top: 2px solid #9a8253; margin-top: 36px;
}
.subtitle {
  margin-top: 34px; font-weight: 200; font-size: 31px; line-height: 1.31;
  color: #1a1a1a;
}
.price-line {
  margin-top: 42px; font-weight: 200; font-size: 31px; line-height: 1.31;
  color: #1a1a1a;
}
/* при персональной скидке: обычная цена — тихой строкой, скидочная — главной */
.price-was {
  margin-top: 42px; font-weight: 200; font-size: 31px; line-height: 1.31;
  color: #1a1a1a;
}
.price-discounted {
  margin-top: 16px; font-weight: 700; font-size: 39px; line-height: 1.25;
  color: #000;
}
.meta {
  margin-top: 18px; font-weight: 200; font-size: 31px; line-height: 1.31;
  color: #1a1a1a;
}
.cert {
  margin-top: 42px; font-weight: 200; font-size: 31px; line-height: 1.31;
  color: #1a1a1a;
}
.blurb {
  margin-top: 42px; font-weight: 200; font-size: 31px; line-height: 1.31;
  color: #1a1a1a;
}
.blurb p + p { margin-top: 40px; }  /* абзацы провенанса — как в образце */
.more {
  margin-top: 46px; font-weight: 200; font-size: 31px; line-height: 1.31;
  color: #1a1a1a;
}
.more a { color: #1a1a1a; }

/* — финальный слайд: левая колонка (лого + текст) центрирована по вертикали — */
.final-left {
  position: absolute; left: 90px; top: 0; width: 500px; height: 1440px;
  display: flex; flex-direction: column; justify-content: center;
}
.final-logo { width: 330px; margin-bottom: 80px; }
.final-logo img { width: 100%; }
.final-text {
  font-weight: 200; font-size: 34px; line-height: 1.4; color: #1a1a1a;
}
.final-text p + p { margin-top: 46px; }
.final-grid {
  position: absolute; left: 630px; top: 60px; width: 1230px; height: 1320px;
  display: grid; grid-template-columns: 1fr 1fr; grid-template-rows: 1fr 1fr;
  gap: 20px;
}
.final-grid .cell { overflow: hidden; }
.final-grid .cell img { width: 100%; height: 100%; object-fit: cover; }
"""


def _fmt_price(price) -> str:
    try:
        p = int(price)
    except (TypeError, ValueError):
        return ""
    if p <= 0:
        return ""
    return "{:,}".format(p).replace(",", " ") + "₽"


def _esc(s: str) -> str:
    return (str(s or "")
            .replace("&", "&amp;").replace("<", "&lt;").replace(">", "&gt;"))


def _cert_line(cert) -> str:
    # Правила дома StarGift (Вашик 22.07.2026): на витрине (презентация клиенту)
    # тема подлинности под запретом — сертификатор даём мелкой спецификацией,
    # без слова «подлинность» (как Rolex не пишет, что часы настоящие).
    if cert is None:
        return ""
    cert = str(cert).strip()
    if cert:
        return f"Сертификат галереи · {cert.upper()}"
    return "Сертификат галереи StarGift"


def _shrink(path: Path, out: Path, max_side: int = 1400) -> Path:
    """Ужать фото до max_side и перекодировать в JPEG q85 — иначе Chrome
    вошьёт оригиналы и PDF раздувается до десятков МБ (Telegram-лимит 50)."""
    try:
        from PIL import Image
    except ImportError:
        return path
    try:
        im = Image.open(path)
        im.thumbnail((max_side, max_side))
        if im.mode != "RGB":
            bg = Image.new("RGB", im.size, (255, 255, 255))
            bg.paste(im, mask=im.split()[-1] if im.mode in ("RGBA", "LA") else None)
            im = bg
        im.save(out, "JPEG", quality=85)
        return out
    except Exception:
        return path


def _localize_photo(src: str, workdir: Path, idx: int) -> str:
    """Download an http(s) photo to workdir (downscaled); pass local paths through."""
    if not src:
        return ""
    small = workdir / f"photo_{idx}.small.jpg"
    if src.startswith(("http://", "https://")):
        ext = os.path.splitext(src.split("?")[0])[1] or ".jpg"
        dst = workdir / f"photo_{idx}{ext}"
        req = urllib.request.Request(src, headers={"User-Agent": "stargift-selection-pdf"})
        with urllib.request.urlopen(req, timeout=60) as r, open(dst, "wb") as f:
            f.write(r.read())
        return _shrink(dst, small).as_uri()
    return _shrink(Path(src).expanduser().resolve(), small).as_uri()


def _item_slide(item: dict, workdir: Path, idx: int) -> str:
    person = _esc(item.get("person"))
    headline = _esc(item.get("headline"))
    price = _fmt_price(item.get("price"))
    dimensions = _esc(item.get("dimensions") or "")

    cert = _cert_line(item.get("cert"))
    blurb = _esc(item.get("blurb"))

    photo = _localize_photo(item.get("photo_product") or "", workdir, idx * 10)
    # Второе фото — «деталь»: автограф(ы), крупный план или разворот книги.
    # Ключ photo_details; старое имя photo_signatures принимается как синоним.
    details_raw = item.get("photo_details") or item.get("photo_signatures") or []
    details = [
        _localize_photo(s, workdir, idx * 10 + 1 + j)
        for j, s in enumerate(details_raw[:4])
        if s
    ]

    if not details:
        # запасной вариант: у экспоната в каталоге всего одно фото
        photos_html = f'<div class="photo-full"><img src="{photo}"></div>'
    elif len(details) == 1:
        photos_html = (
            f'<div class="photo-top frame"><img src="{photo}"></div>'
            f'<div class="photo-bottom frame"><img src="{details[0]}"></div>'
        )
    else:
        # 2–4 детали: квадратные ячейки без обрезки содержимого (кропы
        # автографов квадратные), ряд центрирован в зоне нижнего фото;
        # 4 штуки раскладываются сеткой 2×2.
        n = len(details)
        if n == 4:
            side = 220
        else:
            side = min(620, (853 - 18 * (n - 1)) // n - 6)  # зона 853×640 в 4:3
        cells = "".join(
            f'<div class="sig frame" style="width:{side}px;height:{side}px">'
            f'<img src="{s}"></div>'
            for s in details
        )
        photos_html = (
            f'<div class="photo-top frame"><img src="{photo}"></div>'
            f'<div class="sig-row">{cells}</div>'
        )

    price_discounted = _fmt_price(item.get("price_discounted"))
    if price and price_discounted:
        # обычная цена — тихо, цена со скидкой — главной строкой (без процента)
        price_html = (
            f'<div class="price-was">Цена: {price}</div>'
            f'<div class="price-discounted">Цена с учётом вашей скидки: '
            f'{price_discounted}</div>'
        )
    elif price:
        price_html = f'<div class="price-line">{price}</div>'
    else:
        price_html = ""
    meta_html = f'<div class="meta">Размеры: {dimensions}</div>' if dimensions else ""
    subtitle_html = f'<div class="subtitle">{headline}</div>' if headline else ""
    cert_html = f'<div class="cert">{cert}</div>' if cert else ""
    # Абзацы blurb: провенанс идёт несколькими абзацами (образец Вашика 20.07) —
    # \n\n в исходнике превращаем в <p>, иначе HTML склеил бы всё в кашу.
    raw_blurb = item.get("blurb") or ""
    if raw_blurb.strip():
        paras = [f"<p>{_esc(t.strip())}</p>" for t in raw_blurb.split("\n\n") if t.strip()]
        blurb_html = f'<div class="blurb">{"".join(paras)}</div>'
    else:
        blurb_html = ""
    url = (item.get("url") or "").strip()
    more_html = (f'<div class="more">Больше информации об экспонате: '
                 f'<a href="{_esc(url)}"><u>{person}{" — " + headline if headline else ""}</u></a></div>'
                 if url else "")

    return f"""
<div class="slide">
  {photos_html}
  <div class="logo"><img src="{(ASSETS / 'logo_block.png').as_uri()}"></div>
  <div class="textcol">
    <div class="name">{person}</div>
    {subtitle_html}
    {price_html}
    {cert_html}
    {blurb_html}
    {more_html}
  </div>
</div>"""


def _final_slide(custom_text: str = "") -> str:
    cells = "".join(
        f'<div class="cell"><img src="{(ASSETS / f"pg20-00{i}.jpg").as_uri()}"></div>'
        for i in (0, 1, 2, 3)
    )
    texts = ([t.strip() for t in custom_text.split("\n\n") if t.strip()]
             if custom_text.strip() else FINAL_SLIDE_TEXT)
    paras = "".join(f"<p>{_esc(t)}</p>" for t in texts)
    return f"""
<div class="slide">
  <div class="final-left">
    <div class="final-logo"><img src="{(ASSETS / 'logo_block.png').as_uri()}"></div>
    <div class="final-text">{paras}</div>
  </div>
  <div class="final-grid">{cells}</div>
</div>"""


# Автоподгонка описания: если текст-колонка не влезает по высоте слайда,
# уменьшаем кегль blurb (до 26px), затем — мягкая обрезка по словам с «…».
# Chrome headless исполняет скрипты до печати, поэтому PDF получает уже
# подогнанный текст; резать посреди строки низом слайда больше нельзя.
_FIT_SCRIPT = """
<script>
(function () {
  var LIMIT = 1380;                       // нижняя граница текста (4:3, холст 1440)
  document.querySelectorAll('.slide').forEach(function (slide) {
    var col = slide.querySelector('.textcol');
    var blurb = slide.querySelector('.blurb');
    if (!col || !blurb) return;
    function overflow() {
      return col.getBoundingClientRect().bottom -
             slide.getBoundingClientRect().top > LIMIT;
    }
    var size = 31;
    while (size > 24 && overflow()) {
      size -= 1;
      blurb.style.fontSize = size + 'px';
    }
    var guard = 60;
    while (overflow() && guard-- > 0) {
      var words = blurb.textContent.replace(/…$/, '').trim().split(/\\s+/);
      if (words.length <= 6) break;
      blurb.textContent = words.slice(0, -3).join(' ') + '…';
    }
  });
})();
</script>
"""


def _prefs_css(prefs: dict) -> str:
    """Личные настройки менеджера → CSS-надстройка поверх базового шаблона."""
    extra = []
    if prefs.get("price_bold"):
        extra.append(".price-line { font-weight: 700; font-size: 39px; color: #000; }")
    if not prefs.get("show_link", True):
        extra.append(".more { display: none; }")
    if prefs.get("logo_position") == "right":
        extra.append(".logo { left: auto; right: 60px; width: 260px; opacity: 0.55; }")
    if prefs.get("text_size") == "large":
        extra.append(".subtitle, .blurb, .price-was, .meta, .cert, .more { font-size: 34px; }")
    return "\n".join(extra)


def build_html(data: dict, workdir: Path, prefs: dict = None) -> str:
    prefs = prefs or {}
    slides = "".join(
        _item_slide(item, workdir, i)
        for i, item in enumerate(data.get("items") or [])
    )
    if prefs.get("show_final_slide", True):
        slides += _final_slide(prefs.get("final_text") or "")
    title = _esc(data.get("title") or "Подборка StarGift")
    return (
        f"<!DOCTYPE html><html><head><meta charset='utf-8'>"
        f"<title>{title}</title><style>{CSS}\n{_prefs_css(prefs)}</style></head>"
        f"<body>{slides}{_FIT_SCRIPT}</body></html>"
    )


def render_pdf(data: dict, out_path: str, prefs: dict = None) -> str:
    out = Path(out_path).expanduser().resolve()
    with tempfile.TemporaryDirectory(prefix="selpdf_") as td:
        workdir = Path(td)
        html_file = workdir / "selection.html"
        html_file.write_text(build_html(data, workdir, prefs), encoding="utf-8")
        subprocess.run(
            [
                CHROME, "--headless", "--disable-gpu", "--no-pdf-header-footer",
                "--allow-file-access-from-files",
                f"--print-to-pdf={out}",
                html_file.as_uri(),
            ],
            check=True, capture_output=True, timeout=180,
        )
    return str(out)


if __name__ == "__main__":
    if len(sys.argv) != 3:
        print("usage: python selection_pdf.py data.json out.pdf", file=sys.stderr)
        sys.exit(2)
    with open(sys.argv[1]) as f:
        payload = json.load(f)
    print(render_pdf(payload, sys.argv[2]))
