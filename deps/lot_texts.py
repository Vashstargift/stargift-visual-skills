"""Мини-тексты лотов для подборок — пишет Opus по ПРАВИЛАМ ДОМА StarGift.

Зачем: Док (рабочая модель) писал blurb'ы сам — выходила сухая энциклопедия,
у четырёх предметов одной персоны тексты дублировались («пятикратный чемпион
НБА» ×4). Фидбек Вашика 24.07: «без нормальных текстов смысл презентации
отпадает — легче наделать скринов».

Решение: отдельный вызов сильной модели (Opus) со всеми фактами лота
(описание с сайта + тип предмета + персона) и правилами дома из
feedback_text_style_rules: люкс-регистр Sotheby's, формула подписи лота
(интересный факт / особенность предмета / для кого), ЗАПРЕТ темы подлинности,
без штампов и эмодзи. Разные предметы одной персоны обязаны получить РАЗНЫЕ
тексты — про предмет, а не переписанную биографию.

Ключ Anthropic берётся с сервера (как в deal-chat-parse и дистиллере уроков).
"""
from __future__ import annotations

import json
import re
import subprocess
import urllib.request

MODEL = "claude-opus-4-8"        # тексты витрины — правило дома «пишет Opus»
FALLBACK_MODEL = "claude-sonnet-5"
CATALOG = "https://stargift.ru/api/catalog-compat.php"

RULES = """Ты пишешь подписи лотов для галереи StarGift (VIP-подарки, автографы).

РЕГИСТР — люкс (Sotheby's, Christie's): спокойно, достойно, короткие фразы.
Без шуток, сленга, капса, восклицаний, эмодзи, английских слов.

ФОРМУЛА подписи — одно из трёх (выбирай, что сильнее для конкретного лота):
(а) интересный конкретный факт о человеке или предмете;
(б) в чём особенность именно этого предмета;
(в) для кого / для какого интерьера.

ЗАПРЕЩЕНО:
- тема подлинности: «подлинный», «подлинность», «сертификат подлинности», «сертифицированный»;
- продающие штампы: «уникальный подарок», «идеально для фанатов», «коллекционная ценность», «порадует», «настоящая находка»;
- слово «коллекционер», эмодзи, английские слова;
- повторять заголовок слайда (имя персоны и тип предмета уже напечатаны выше);
- общие биографические перечисления титулов без конкретики («пятикратный чемпион, входил в сборную звёзд»).

ТРЕБОВАНИЯ:
- 1–2 предложения, 90–240 знаков;
- КОНКРЕТИКА: год, число, событие, деталь предмета — то, что цепляет и запоминается;
- если лотов одной персоны несколько — у КАЖДОГО свой угол (разные факты/разные детали предметов), дублирование недопустимо;
- только факты из данных лота и общеизвестные проверяемые факты о персоне; не выдумывать происхождение предмета, обстоятельства подписания, тиражи."""


def _api_key() -> str:
    out = subprocess.run(
        ["ssh", "-i", "/Users/docbrown/.ssh/id_ed25519", "-o", "BatchMode=yes",
         "stargift@stargift.beget.tech",
         "grep '^ANTHROPIC_API_KEY=' ~/stargift.ru/.env | cut -d= -f2"],
        capture_output=True, text=True, timeout=40)
    key = out.stdout.strip().splitlines()[-1] if out.stdout.strip() else ""
    if not key:
        raise RuntimeError("нет ANTHROPIC_API_KEY")
    return key


def _fetch_cards(product_ids: list) -> dict:
    ids = ",".join(str(i).strip() for i in product_ids if str(i).strip())
    if not ids:
        return {}
    import urllib.parse
    url = f"{CATALOG}?page=1&ids={urllib.parse.quote(ids)}"
    with urllib.request.urlopen(url, timeout=30) as r:
        data = json.loads(r.read())
    return {str(p.get("id")): p for p in (data.get("products") or [])}


def _call(model: str, key: str, prompt: str, max_tokens: int = 2000) -> str:
    payload = {"model": model, "max_tokens": max_tokens,
               "messages": [{"role": "user", "content": prompt}]}
    headers = {"content-type": "application/json", "anthropic-version": "2023-06-01"}
    if key.startswith("sk-ant-oat01-"):
        headers["Authorization"] = "Bearer " + key
        headers["anthropic-beta"] = "oauth-2025-04-20"
    else:
        headers["x-api-key"] = key
    req = urllib.request.Request("https://api.anthropic.com/v1/messages",
                                 data=json.dumps(payload).encode(), headers=headers)
    with urllib.request.urlopen(req, timeout=180) as r:
        return json.load(r)["content"][0]["text"]


def generate_blurbs(items: list) -> dict:
    """items — список dict с product_id (и опц. name/type). Возвращает {product_id: текст}.
    Ошибки не бросает: при сбое возвращает {} — вызывающий оставит свои тексты."""
    pids = [str(it.get("product_id") or "").strip() for it in items if it.get("product_id")]
    if not pids:
        return {}
    try:
        cards = _fetch_cards(pids)
    except Exception:
        cards = {}

    lots = []
    for pid in pids:
        c = cards.get(pid, {})
        full = re.sub(r"<[^>]+>", " ", str(c.get("fullDescription") or ""))
        full = re.sub(r"\s+", " ", full).strip()[:900]
        lots.append({
            "product_id": pid,
            "персона": c.get("title") or "",
            "предмет": c.get("description") or "",
            "цена": c.get("price"),
            "описание_с_сайта": full or "(на сайте описания нет — опирайся на общеизвестные факты о персоне)",
        })

    # Лимит ответа масштабируем от числа лотов: 30 blurb'ов не влезали в 2000
    # токенов — JSON обрезался («Unterminated string», инцидент 28.07).
    max_tok = min(16000, 500 + 350 * len(lots))
    prompt = (RULES + "\n\nЛОТЫ ПОДБОРКИ (JSON):\n"
              + json.dumps(lots, ensure_ascii=False, indent=1)
              + "\n\nНапиши подпись для КАЖДОГО лота. Ответ СТРОГО JSON без пояснений: "
                '{"тексты": [{"product_id": "...", "text": "..."}, ...]}')
    key = _api_key()
    # Каскад: Opus (правило дома «тексты пишет Opus») → Sonnet → Haiku.
    # 429/перегрузка на одной модели не должна ронять подборку целиком.
    import time
    raw = None
    last = None
    for model in (MODEL, FALLBACK_MODEL, "claude-haiku-4-5"):
        for attempt in range(2):
            try:
                raw = _call(model, key, prompt, max_tok)
                break
            except Exception as e:
                last = e
                code = getattr(e, "code", None)
                if code in (429, 529) and attempt == 0:
                    time.sleep(4)
                    continue
                break
        if raw:
            break
    if not raw:
        raise RuntimeError(f"все модели недоступны: {last}")
    raw = re.sub(r"^```(json)?|```$", "", raw.strip(), flags=re.M).strip()
    data = json.loads(raw)
    out = {}
    for row in data.get("тексты", []):
        pid = str(row.get("product_id") or "").strip()
        txt = str(row.get("text") or "").strip()
        if pid and txt:
            out[pid] = txt
    return out
