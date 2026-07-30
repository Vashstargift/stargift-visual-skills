"""Личные настройки шаблона подборок per менеджер (Вашик, 20.07.2026):
«если менеджер просит поправить вне шаблона — бот обязан; если просит ЛИЧНО
сменить основной шаблон — подхватывает и запоминает». Хранение: JSON по tg_id.

Поддерживаемые опции (ключи и значения валидируются — мусор не сохраняется):
  price_bold: true|false     — цена жирным (по умолчанию как в эталоне: светлая)
  show_link: true|false      — строка «Больше информации об экспонате»
  show_final_slide: true|false — финальный слайд-оффер
  logo_position: "center"|"right"  — лого по центру колонки (эталон) или в правом углу
  text_size: "normal"|"large"      — кегль основного текста (31px / 34px)
  final_text: "строка"       — свой текст финального слайда (напр. с именем менеджера)
  editable_format: "key"|"pptx" — формат редактируемой версии (Настя 28.07: у неё
                             Keynote подменяет шрифт — ей нужен обычный PPTX)
"""
import json
import os

_PATH = "/Users/docbrown/hermes-doc/selection_prefs.json"

_VALID = {
    "price_bold": (True, False),
    "show_link": (True, False),
    "show_final_slide": (True, False),
    "logo_position": ("center", "right"),
    "text_size": ("normal", "large"),
    "final_text": str,  # любая непустая строка ≤600
    "editable_format": ("key", "pptx"),
}

DEFAULTS = {
    "price_bold": False,
    "show_link": True,
    "show_final_slide": True,
    "logo_position": "center",
    "text_size": "normal",
    "final_text": "",
    "editable_format": "key",
}


def _load_all() -> dict:
    try:
        with open(_PATH) as f:
            return json.load(f)
    except Exception:
        return {}


def get_prefs(tg_id: str) -> dict:
    """Действующие настройки менеджера (дефолты + его сохранённые)."""
    prefs = dict(DEFAULTS)
    prefs.update(_load_all().get(str(tg_id), {}))
    return prefs


def validate(changes: dict) -> tuple:
    """(чистые_изменения, ошибки[])"""
    clean, errors = {}, []
    for k, v in (changes or {}).items():
        rule = _VALID.get(k)
        if rule is None:
            errors.append(f"неизвестная опция «{k}» (есть: {', '.join(_VALID)})")
        elif rule is str:
            v = str(v).strip()
            if 0 < len(v) <= 600:
                clean[k] = v
            else:
                errors.append(f"«{k}»: строка 1–600 символов")
        elif v in rule:
            clean[k] = v
        else:
            errors.append(f"«{k}»: допустимо {rule}, получено {v!r}")
    return clean, errors


def set_prefs(tg_id: str, changes: dict) -> dict:
    """Сохранить личные настройки менеджера (мердж). Возвращает действующие."""
    clean, _ = validate(changes)
    allp = _load_all()
    cur = allp.setdefault(str(tg_id), {})
    cur.update(clean)
    tmp = _PATH + ".tmp"
    with open(tmp, "w") as f:
        json.dump(allp, f, ensure_ascii=False, indent=1)
    os.replace(tmp, _PATH)
    return get_prefs(tg_id)


def reset_prefs(tg_id: str) -> dict:
    allp = _load_all()
    allp.pop(str(tg_id), None)
    tmp = _PATH + ".tmp"
    with open(tmp, "w") as f:
        json.dump(allp, f, ensure_ascii=False, indent=1)
    os.replace(tmp, _PATH)
    return get_prefs(tg_id)
