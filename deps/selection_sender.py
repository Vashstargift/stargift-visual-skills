"""Shared album-sending logic for StarGift selection features.

Used by:
  - mcp/crm_server.py  (send_selection tool, catalog_search tool)
  - plugins/stargift_selection (deterministic pre_llm_call plugin)

Public API:
  send_album(chat_id, query, limit=5, max_price=0) -> str
  _fetch_products(query, limit, max_price) -> list
  _fmt_price(raw) -> str
  _telegram_token() -> str
"""
import json
import re
import urllib.request
import urllib.parse


def _fmt_price(raw) -> str:
    """Format integer rubles as '150 000 ₽', or 'цена по запросу' for null/zero."""
    try:
        p = int(raw)
        if p <= 0:
            return "цена по запросу"
        return f"{p:,}".replace(",", " ") + " ₽"
    except (ValueError, TypeError):
        return "цена по запросу"


def _fetch_products(query: str, limit: int, max_price: int, category: str = "") -> list:
    """Fetch products from catalog-compat.php; returns list of product dicts (may be empty).

    category — точное имя категории каталога («Женщине», «Руководителю», «Музыка»…):
    сужает выборку на стороне бекенда; query при этом может быть пустым.
    Each item guaranteed to be a dict; photos field is a list (may be empty).
    Raises on network / parse errors — callers should wrap in try/except.
    """
    fetch_limit = max(limit, 100)
    qs: dict = {"search": query, "limit": fetch_limit, "page": 1}
    if category:
        qs["category"] = category
    if max_price > 0:
        qs["max_price"] = max_price
    params = urllib.parse.urlencode(qs)
    url = f"https://stargift.ru/api/catalog-compat.php?{params}"
    req = urllib.request.Request(url, headers={"Accept": "application/json"})
    with urllib.request.urlopen(req, timeout=20) as resp:
        raw = resp.read().decode("utf-8")
    data = json.loads(raw)
    if not isinstance(data, dict) or "products" not in data:
        raise ValueError("Неожиданный формат ответа каталога.")
    products = data["products"]
    # Normalise: ensure photos is always a list
    for p in products:
        if not isinstance(p.get("photos"), list):
            p["photos"] = []

    # Релевантность ПЕРВЫМ ключом (22.07): бекенд матчит и по длинным текстам
    # карточек, поэтому «Федерер» тянет Каку/Путина/шахматы — мусор разбавлял
    # limit и настоящих Федереров «выдавало оч мало» (Вашик). Совпадение слова
    # запроса в ИМЕНИ экспоната > в типе > только в тексте.
    raw_words = [w.strip(",.«»\"") for w in query.split() if len(w.strip(",.«»\"")) >= 3]
    qwords = [w.lower() for w in raw_words]
    # Персона («Федерер», «Лионель Месси») — слово с Заглавной: имя экспоната важнее.
    # Тема («зарубежный футбол», «теннис») — имя НЕ важнее категории, иначе 8 журналов
    # «Футбол и хоккей» забивают всю тему (кейс Вашика 22.07).
    person_mode = any(w[:1].isupper() for w in raw_words)

    def _relevance(p):
        title = (p.get("title") or "").lower()
        desc = (p.get("description") or "").lower()
        # Категории/теги — полноценный матч (22.07): у бутс Месси «футбол»
        # только в категории — раньше это считалось шумом, и тема «футбол»
        # выдавала одни футболки (подстрочный матч «футбол»⊂«футболка»).
        cats = p.get("categories")
        cats_s = " ".join(cats).lower() if isinstance(cats, list) else str(cats or "").lower()
        tags = p.get("tags")
        tags_s = " ".join(tags).lower() if isinstance(tags, list) else str(tags or "").lower()
        if not qwords:
            return 0
        if any(w in title for w in qwords):
            return 0
        if any(w in desc or w in cats_s or w in tags_s for w in qwords):
            return 0 if not person_mode else 1
        return 2  # матч только по длинному тексту/переводам — почти всегда шум

    # Приоритет выдачи (Вашик, 20.07.2026), стабильно внутри корзин:
    # 1) в наличии В ГАЛЕРЕЕ → 2) в наличии без галереи → 3) под заказ;
    # и с ЦЕНОЙ раньше, чем без цены. Частная коллекция (avail=0) — в хвост.
    # Статусы наличия в каталоге разнобойные: '1'/'instock'/'framed'/'9' = в наличии.
    def _rank(p):
        av = str(p.get("availability") or "").strip()
        gal = (p.get("gallery") or "").strip()
        if av == "0":
            bucket = 4  # частная коллекция — не продаётся
        elif av in ("preorder", "outofstock"):
            bucket = 2
        elif gal:
            bucket = 0  # живой экспонат в галерее — верх выдачи
        else:
            bucket = 1
        has_price = 0 if float(p.get("price") or 0) > 0 else 1
        return (_relevance(p), bucket, has_price)

    products = sorted(products, key=_rank)
    # Есть совпадения по ИМЕНИ экспоната → текстовый шум (rel=2: Хасбулатов в
    # выдаче Федерера) выбрасываем совсем. Тематические запросы («теннис») без
    # именных совпадений сохраняют всё — там категория матчится через тексты.
    strong = [p for p in products if _relevance(p) == 0]
    if strong:
        products = [p for p in products if _relevance(p) <= 1]
    if person_mode and len(strong) >= limit:
        products = strong

    # Темы: однотипное схлопываем (8 журналов «Футбол и хоккей» разных годов = 1
    # позиция) и ПЕРЕМЕШИВАЕМ ТИПЫ round-robin (футболка/бутса/мяч/фото…) —
    # тема не должна выдавать 8 футболок подряд (Вашик, 22.07).
    if not person_mode:
        seen = set()
        dedup = []
        for p in products:
            key = ((p.get("title") or "").strip().lower(),
                   re.sub(r"\d+", "", (p.get("description") or "").lower()).strip())
            if key in seen:
                continue
            seen.add(key)
            dedup.append(p)
        by_type: dict = {}
        for p in dedup:
            t = (((p.get("description") or "").split() or [""])[0]).lower().rstrip(".,")
            by_type.setdefault(t, []).append(p)
        mixed = []
        queues = list(by_type.values())
        while any(queues):
            for q_ in queues:
                if q_:
                    mixed.append(q_.pop(0))
        products = mixed

    # Честность о полноте (Вашик, 22.07): вызывающий видит, сколько подходило
    # ВСЕГО (до среза limit) — чтобы сказать «это всё» или «есть ещё, прислать?».
    class _ProductList(list):
        total_matched = 0

    out = _ProductList(products[:limit])
    out.total_matched = len(products)
    return out


# ── Theme-relevant, diverse selections ──────────────────────────────────────
# Selections used to be "first N search hits": that ignored the curated top
# exhibits AND, once made tops-aware, over-corrected — off-theme tops (e.g. top
# musicians) leaked into an "oil-industry executive" brief. The model below is
# strictly theme-first, three tiers:
#   1. best on-theme exhibits   2. weaker on-theme   3. general tops (backfill only)
# A top only breaks ties WITHIN the same relevance score; off-theme tops are
# dropped from tiers 1–2. Everything is deduped by person/subject.

_TOPS_URL = "https://stargift.ru/api/catalog-tops.php"

# Categories too generic to identify an exhibit (used only as a last-resort key).
_GENERIC_CATS = {
    "все ru", "эксклюзив", "новинки", "новинка", "вещи", "основные экспонаты",
    "дороже 1 млн.р.", "до 1 млн.р.", "новый год", "8 марта", "23 февраля",
    "14 февраля", "день рождения",
}


def _diversity_key(p: dict) -> str:
    """Stable key identifying the *person/subject* of a card, for de-duplication.

    Leading title segment before the first comma / period / parenthesis / dash
    (that's the person's name in StarGift titles). Falls back to the first
    non-generic category, then the raw title.
    """
    title = (p.get("title") or "").strip().lower()
    seg = re.split(r"[,.()–—\-]", title, maxsplit=1)[0].strip()
    if seg:
        return seg
    cats = p.get("categories")
    if isinstance(cats, str):
        cats = [c.strip() for c in re.split(r"[;,]", cats) if c.strip()]
    if isinstance(cats, list):
        for c in cats:
            cl = (c or "").strip().lower()
            if cl and cl not in _GENERIC_CATS:
                return cl
    return title or str(p.get("id") or "")


def _full_title_key(p: dict) -> str:
    """Dedup key for by-name searches — the whole title, so distinct items of the
    SAME person ('Месси. Футболка' vs 'Месси. Мяч') are both kept; only exact
    duplicate listings collapse."""
    return (p.get("title") or "").strip().lower()


def _diversify(products: list, limit: int, key=_diversity_key) -> list:
    """Keep at most one card per `key`, preserving order, up to `limit`. Default key
    is the person/subject (variety across people); by-name searches pass the full
    title instead (variety across that person's exhibits)."""
    seen = set()
    out = []
    for p in products:
        k = key(p)
        if k in seen:
            continue
        seen.add(k)
        out.append(p)
        if len(out) >= limit:
            break
    return out


# Theme words that carry no exhibit meaning — dropped before scoring/searching.
_THEME_STOP = {
    "для", "про", "тему", "теме", "сфере", "сфера", "сфер", "области", "сферы",
    "руб", "рублей", "штук", "штуки", "экспонат", "экспонаты", "вариант",
    "варианты", "подарок", "подарки", "подобрать", "подбери", "подбор",
    "подборка", "подборку", "клиенту", "клиента", "пожалуйста", "мне", "нам",
    "что", "подарить", "идея", "хочу", "нужен", "нужна", "нужно", "который",
    "которая", "работает", "занимается", "сделай", "дай", "его", "очень",
}


def _stem(word: str) -> str:
    """Crude Russian stemmer (project convention): keep first max(4, len-3) chars,
    so inflected forms collapse: 'руководителю'/'руководитель' → 'руководите'."""
    word = word.lower()
    return word[: max(4, len(word) - 3)]


def _content_words(theme: str) -> list:
    """Original (un-stemmed) meaningful words of the theme — used as search queries."""
    out = []
    for tok in re.split(r"[^а-яёa-z]+", theme.lower()):
        if len(tok) >= 4 and tok not in _THEME_STOP and tok not in out:
            out.append(tok)
    return out


# Russian case endings start with a vowel or ь/й — used to tell an inflected form
# of a NAME ('Месси'→'Месси') from a different word that merely shares a prefix
# ('Месси' vs 'Мессинг', where 'инг'… actually starts with и — so we match on the
# FULL name and require the continuation to be a real ending, see _name_hit).
_INFLECT_INITIAL = set("аеёиоуыэюяьй")


def _theme_terms(theme: str) -> list:
    """Classify each meaningful theme word for relevance matching, returning
    (kind, value) pairs:
      • NAME  — a Capitalized word ('Месси') → matched TIGHTLY: the word itself or
                an inflected form, so it never leaks into 'Мессинг'/'Мессершмитт'.
      • TOPIC — a lowercase word ('нефть', 'спорт') → matched LOOSELY by stem-prefix,
                so 'нефть' still reaches 'нефтепровода'.
    """
    terms = []
    seen = set()
    for w in re.findall(r"[A-Za-zА-ЯЁа-яё]+", theme):
        lw = w.lower()
        if len(lw) < 3 or lw in _THEME_STOP or lw in seen:
            continue
        seen.add(lw)
        if w[:1].isupper():
            terms.append(("name", lw))
        elif len(lw) >= 4:
            terms.append(("topic", _stem(lw)))
    return terms


def _name_hit(name: str, word: str) -> bool:
    """A blob word matches a NAME term only if it IS the name or an inflected form
    (name + an ending that starts with a vowel/ь/й). 'Мессинг' (name+'инг') is
    rejected because 'инг' is a derivational, not an inflectional, continuation —
    enforced by also requiring the tail to be short (≤3 chars)."""
    if word == name:
        return True
    if word.startswith(name):
        tail = word[len(name):]
        return 1 <= len(tail) <= 3 and tail[0] in _INFLECT_INITIAL
    return False


def _is_name_query(terms: list) -> bool:
    """True if the theme names a specific person (any NAME term) — such a search is
    answered with its matches only, not padded with unrelated curated tops."""
    return any(kind == "name" for kind, _ in terms)


def _text_blob(p: dict, keys) -> str:
    """Lowercased concatenation of the given product fields (lists flattened)."""
    parts = []
    for k in keys:
        v = p.get(k)
        if isinstance(v, list):
            parts.extend(str(x) for x in v)
        elif v:
            parts.append(str(v))
    return " ".join(parts).lower()


def _term_hit(kind: str, val: str, words: set) -> bool:
    """Whether a theme term matches any word: NAME tightly, TOPIC by stem-prefix."""
    if kind == "name":
        return any(_name_hit(val, w) for w in words)
    return any(w.startswith(val) for w in words)


def _relevance(p: dict, terms: list) -> int:
    """How well a product matches the theme. Hits in name/categories/tags count
    double; hits in the description count single. 0 = off-theme.

    TOPIC terms match at a WORD START (stem is a prefix): 'спор' (спорт) matches
    'спортивная' but not 'па-спор-тная'. NAME terms match tightly (see _name_hit):
    'Месси' does not match 'Мессинг'."""
    if not terms:
        return 0
    strong = set(re.findall(r"[а-яёa-z]+", _text_blob(p, ("title", "categories", "tags", "recipients", "occasions"))))
    weak = set(re.findall(r"[а-яёa-z]+", _text_blob(p, ("description", "fullDescription"))))
    score = 0
    for kind, val in terms:
        if _term_hit(kind, val, strong):
            score += 2
        elif kind == "topic" and _term_hit(kind, val, weak):
            # Topics may match in the description; NAMES may not — otherwise an
            # exhibit that merely mentions the person in passing ('соперник Месси'
            # on a Mbappé card) would leak into a by-name search.
            score += 1
    return score


def _rank_by_theme(products: list, terms: list, top_ids: list) -> list:
    """Rank ON-THEME products only (score > 0): best theme match first, then weaker
    theme matches. A curated top breaks ties WITHIN the same relevance score — it
    never jumps ahead of a more relevant exhibit, and off-theme tops are dropped."""
    top_set = {str(pid) for pid in top_ids}
    scored = []
    for i, p in enumerate(products):
        sc = _relevance(p, terms)
        if sc <= 0:
            continue
        is_top = 0 if str(p.get("id")) in top_set else 1
        scored.append((-sc, is_top, i, p))
    scored.sort(key=lambda t: (t[0], t[1], t[2]))
    return [t[3] for t in scored]


def _fetch_tops() -> list:
    """Fetch curated top exhibits from catalog-tops.php.
    Returns a list of dicts (product_id, title, categories, sort_order). May raise."""
    req = urllib.request.Request(_TOPS_URL, headers={"Accept": "application/json"})
    with urllib.request.urlopen(req, timeout=15) as resp:
        data = json.loads(resp.read().decode("utf-8"))
    if isinstance(data, dict):
        data = data.get("products") or data.get("tops") or next(
            (v for v in data.values() if isinstance(v, list)), []
        )
    return data if isinstance(data, list) else []


def _resolve_top_product(top: dict, max_price: int = 0) -> dict:
    """Resolve a top entry (product_id + title) to a full product dict WITH photos,
    by searching catalog-compat for the person and matching the id. None if not found."""
    pid = str(top.get("product_id") or "")
    if not pid:
        return None
    title = top.get("title") or ""
    query = re.split(r"[,.()]", title, maxsplit=1)[0].strip() or title
    try:
        hits = _fetch_products(query, 30, max_price)
    except Exception:
        return None
    for p in hits:
        if str(p.get("id")) == pid:
            return p
    return None


# ── Смежная добивка из таблицы «НАЛИЧИЕ ТОПЫ» (Вашик, 20.07.2026) ──────────
# Если по теме мало вещей — добираем топами СМЕЖНОЙ категории (футбол мало →
# баскетбол/бокс из Спорта), затем универсальными «мужскими» (Аль Пачино, Гагарин).
_THEME_CATEGORY_WORDS = {
    "Спорт": ("футбол", "хокке", "теннис", "баскет", "бокс", "мма", "гонк", "гольф",
              "спорт", "олимп", "матч", "чемпион"),
    "Кино": ("кино", "фильм", "актер", "актёр", "актрис", "режисс", "голливуд", "сериал"),
    "Музыка": ("музык", "рок", "джаз", "групп", "гитар", "концерт", "певец", "певиц"),
}
_UNIVERSAL_BACKFILL = ["Аль Пачино", "Юрий Гагарин"]  # универсальные мужские


def _theme_category(theme: str, terms: list) -> str:
    low = theme.lower()
    for cat, words in _THEME_CATEGORY_WORDS.items():
        if any(w in low for w in words):
            return cat
    # тема = имя персоны → категория персоны из таблицы топов
    try:
        import tops_sheet
        for kind, val in terms:
            if kind == "name":
                c = tops_sheet.category(val)
                if c != "Другое":
                    return c
    except Exception:
        pass
    return ""


def _sheet_backfill(category: str, exclude_keys: set, max_price: int, need: int) -> list:
    """Добрать позиции топ-персон из таблицы: сперва та же категория, затем универсальные."""
    picked = []
    try:
        import tops_sheet
        persons = tops_sheet.parse_tops(tops_sheet.download_sheet())
    except Exception:
        persons = []
    ordered = []
    if category:
        ordered += [p["person"] for p in sorted(persons, key=lambda x: -x["total"])
                    if tops_sheet.category(p["person"]) == category]
    ordered += [u for u in _UNIVERSAL_BACKFILL if u not in ordered]
    for person in ordered:
        if len(picked) >= need:
            break
        key = person.strip().lower()
        if any(key.split()[-1] in k for k in exclude_keys):
            continue  # эта персона уже в подборке
        try:
            cands = _fetch_products(person, 3, max_price)
        except Exception:
            continue
        best = next((c for c in cands if c.get("photos")), None)
        if not best:
            continue
        k = _diversity_key(best)
        if k in exclude_keys:
            continue
        exclude_keys.add(k)
        picked.append(best)
    return picked


def _gather_selection(theme: str, limit: int, max_price: int) -> list:
    """Build a themed selection in three tiers:
      1. Best on-theme exhibits (highest relevance to the theme).
      2. Weaker on-theme exhibits (lower relevance).
      3. General curated tops — ONLY to backfill when the theme is too sparse.
    Off-theme exhibits (incl. off-theme tops) never appear in tiers 1–2.
    Everything deduped by person/subject."""
    terms = _theme_terms(theme)

    # Candidate pool: full theme query + each content word (catalog search is loose,
    # so a single compound query misses items — union of per-word searches recalls more).
    pool = {}
    queries = [theme] + _content_words(theme)
    for q in queries:
        try:
            for p in _fetch_products(q, 30, max_price):
                pid = str(p.get("id"))
                if pid and pid not in pool:
                    pool[pid] = p
        except Exception:
            continue
    products = list(pool.values())

    # Строчные имена («месси, роналду») классифицируются по регистру как TOPIC —
    # это давало рыхлый матч (стем «месс» ловил Мессинга) и tier-3 добивку топами
    # (Леннон в футбольной подборке; кейс Насти 20.07). Повышаем topic→NAME, если
    # слово темы встречается в найденных карточках как Заглавное слово (= персона).
    if products:
        cap_words = set()
        for pr in products:
            for w in re.findall(r"[A-ZА-ЯЁ][a-zа-яё]+", str(pr.get("title") or "")):
                cap_words.add(w.lower())
        theme_words = [w.lower() for w in re.findall(r"[A-Za-zА-ЯЁа-яё]+", theme)
                       if len(w) >= 4 and w.lower() not in _THEME_STOP]
        upgraded = []
        for kind, val in terms:
            if kind == "topic":
                src = next((tw for tw in theme_words
                            if tw == val or _stem(tw) == val or tw.startswith(val)), None)
                if src and src in cap_words:
                    upgraded.append(("name", src))
                    continue
            upgraded.append((kind, val))
        terms = upgraded

    try:
        tops = _fetch_tops()
    except Exception:
        tops = []
    top_ids = [str(t.get("product_id")) for t in tops if t.get("product_id")]

    # Tiers 1–2: on-theme, ranked by relevance (tops only break ties within a score).
    ranked = _rank_by_theme(products, terms, top_ids)
    # By-name search → keep distinct items of that person; topic → variety of people.
    dedup_key = _full_title_key if _is_name_query(terms) else _diversity_key
    picked = _diversify(ranked, limit, key=dedup_key)

    # Tier 3 (Вашик, 20.07): если по теме мало вещей — добираем СМЕЖНЫМИ топами
    # из таблицы «НАЛИЧИЕ ТОПЫ» (та же категория: футбол мало → баскетбол/бокс),
    # затем универсальными мужскими (Аль Пачино, Гагарин). Случайные общие топы
    # сайта (Леннон в футболе) больше не подмешиваются.
    if len(picked) < limit:
        have_keys = {_diversity_key(p) for p in picked}
        cat = _theme_category(theme, terms)
        picked += _sheet_backfill(cat, have_keys, max_price, limit - len(picked))

    return picked[:limit]


# Related-term expansion for sparse/abstract themes → richer, deterministic selections.
# Keys matched by prefix against the theme (lowercased). Order = search priority.
_RELATED_TERMS = {
    "виноделие": ["вино", "коньяк", "шампанское", "дегустац", "винтаж"],
    "вино":      ["вино", "коньяк", "шампанское", "дегустац"],
    "охот":      ["охота", "ружь", "птиц", "зверь"],
    "рыбал":     ["рыбал", "рыба", "удочк"],
    "музык":     ["музык", "рок", "джаз", "гитар", "битлз"],
    "кино":      ["кино", "фильм", "режисс", "актёр", "актрис"],
    "спорт":     ["футбол", "хоккей", "бокс", "теннис", "баскетбол"],
    "авто":      ["автомобил", "гонк", "формула"],
    "космос":    ["гагарин", "космонавт", "космос"],
    "литератур": ["книга", "автор", "писател", "поэт"],
    "искусств":  ["картин", "художник", "скульптур"],
}


def _related_terms(theme: str) -> list:
    low = theme.lower().strip()
    for key, terms in _RELATED_TERMS.items():
        if low.startswith(key) or key.startswith(low):
            return terms
    return []


def _gather_products(theme: str, limit: int, max_price: int) -> list:
    """Direct theme search; if sparse, expand with related terms (dedupe by id)."""
    seen = set()
    out = []
    def add(prods):
        for p in prods:
            pid = p.get("id")
            if pid and pid not in seen:
                seen.add(pid)
                out.append(p)
    try:
        add(_fetch_products(theme, max(limit, 10), max_price))
    except Exception:
        pass
    if len(out) < limit:
        for term in _related_terms(theme):
            if len(out) >= limit:
                break
            try:
                add(_fetch_products(term, max(limit, 10), max_price))
            except Exception:
                continue
    return out[:limit]


def _telegram_token() -> str:
    """Read TELEGRAM_BOT_TOKEN from the staff profile .env file. Returns empty string if missing."""
    env_path = "/Users/docbrown/.hermes/profiles/staff/.env"
    try:
        with open(env_path, encoding="utf-8") as fh:
            for line in fh:
                line = line.strip()
                if line.startswith("TELEGRAM_BOT_TOKEN="):
                    return line.split("=", 1)[1].strip()
    except Exception:
        pass
    return ""


def _fetch_by_ids_ordered(product_ids: list) -> list:
    """Карточки по точным id, в ЗАДАННОМ порядке (кураторская подборка)."""
    ids = ",".join(str(i).strip() for i in product_ids if str(i).strip())
    if not ids:
        return []
    url = f"https://stargift.ru/api/catalog-compat.php?page=1&ids={urllib.parse.quote(ids)}"
    req = urllib.request.Request(url, headers={"Accept": "application/json"})
    with urllib.request.urlopen(req, timeout=30) as resp:
        data = json.loads(resp.read())
    by_id = {str(p.get("id")): p for p in (data.get("products") or [])}
    out = []
    for i in product_ids:
        p = by_id.get(str(i).strip())
        if p:
            if not isinstance(p.get("photos"), list):
                p["photos"] = []
            out.append(p)
    return out


def send_album(chat_id: str, query: str, limit: int = 8, max_price: int = 0,
               min_price: int = 0, in_stock_only: bool = False,
               category: str = "", product_ids: list = None) -> str:
    """Fetch catalog products and send a Telegram photo album to chat_id.

    Handles:
      - 2–10 items  → sendMediaGroup
      - 1 item      → sendPhoto
      - 0 items     → returns informative string without sending

    Returns a short Russian status string. Never raises.
    """
    try:
        token = _telegram_token()
        if not token:
            return "Ошибка: не удалось прочитать TELEGRAM_BOT_TOKEN из staff .env."

        try:
            if product_ids:
                # Кураторский режим: бот сам отобрал экспонаты мозгом — шлём ровно их.
                products = _fetch_by_ids_ordered(product_ids)[:10]
            elif category:
                products = _fetch_products(query or "", min(limit, 10), max_price, category=category)
            else:
                products = _gather_selection(query, min(limit, 10), max_price)
            # Фильтры брифа (Беликова, 22.07): «от 400к» → без дешёвки;
            # «в наличии» → под заказ и частные коллекции не предлагаем.
            if min_price > 0:
                products = [x for x in products
                            if float(x.get("price") or 0) <= 0 or float(x.get("price") or 0) >= min_price]
            if in_stock_only:
                products = [x for x in products
                            if str(x.get("availability") or "").strip() not in ("preorder", "outofstock", "0")]
        except Exception as e:
            return f"Ошибка при обращении к каталогу: {e}"

        if not products:
            return f"По запросу «{query}» ничего не нашёл — нечего отправить."

        # Честность о полноте выдачи (Вашик, 22.07): сколько ещё подходит по теме.
        more_note = ""
        try:
            probe = _fetch_products(query, limit, max_price)
            if min_price > 0:
                probe = [x for x in probe
                         if float(x.get("price") or 0) <= 0 or float(x.get("price") or 0) >= min_price]
            if in_stock_only:
                probe = [x for x in probe
                         if str(x.get("availability") or "").strip() not in ("preorder", "outofstock", "0")]
            total = max(getattr(probe, "total_matched", len(probe)), len(products))
            if len(products) < limit:
                more_note = f" Подходящих под критерии всего {len(products)} — это все."
            elif total > len(products):
                more_note = (f" На эту тему есть ещё подходящие экспонаты (~{total - len(products)}+) — "
                             f"спроси менеджера, прислать ли ещё.")
        except Exception:
            pass

        sent, last_err = _send_products(token, chat_id, products)
        if sent:
            return f"Отправил {sent} фото-карточек (по одному сообщению на экспонат).{more_note}"
        if last_err:
            return f"Не удалось отправить карточки: {last_err}"
        return f"По запросу «{query}» нашёл товары, но у них нет фото — нечего отправить."

    except Exception as e:
        return f"Внутренняя ошибка send_album: {e}"


def select_and_send(chat_id: str, query: str, limit: int = 5, max_price: int = 0):
    """Like send_album, but RETURNS (status_str, products_sent) so the caller can
    write an informed, item-specific intro instead of generic filler. Never raises."""
    try:
        token = _telegram_token()
        if not token:
            return ("Ошибка: не удалось прочитать TELEGRAM_BOT_TOKEN из staff .env.", [])
        try:
            if product_ids:
                # Кураторский режим: бот сам отобрал экспонаты мозгом — шлём ровно их.
                products = _fetch_by_ids_ordered(product_ids)[:10]
            elif category:
                products = _fetch_products(query or "", min(limit, 10), max_price, category=category)
            else:
                products = _gather_selection(query, min(limit, 10), max_price)
            # Фильтры брифа (Беликова, 22.07): «от 400к» → без дешёвки;
            # «в наличии» → под заказ и частные коллекции не предлагаем.
            if min_price > 0:
                products = [x for x in products
                            if float(x.get("price") or 0) <= 0 or float(x.get("price") or 0) >= min_price]
            if in_stock_only:
                products = [x for x in products
                            if str(x.get("availability") or "").strip() not in ("preorder", "outofstock", "0")]
        except Exception as e:
            return (f"Ошибка при обращении к каталогу: {e}", [])
        if not products:
            return (f"По запросу «{query}» ничего не нашёл — нечего отправить.", [])
        # Честность о полноте выдачи (Вашик, 22.07): сколько ещё подходит по теме.
        more_note = ""
        try:
            probe = _fetch_products(query, limit, max_price)
            if min_price > 0:
                probe = [x for x in probe
                         if float(x.get("price") or 0) <= 0 or float(x.get("price") or 0) >= min_price]
            if in_stock_only:
                probe = [x for x in probe
                         if str(x.get("availability") or "").strip() not in ("preorder", "outofstock", "0")]
            total = max(getattr(probe, "total_matched", len(probe)), len(products))
            if len(products) < limit:
                more_note = f" Подходящих под критерии всего {len(products)} — это все."
            elif total > len(products):
                more_note = (f" На эту тему есть ещё подходящие экспонаты (~{total - len(products)}+) — "
                             f"спроси менеджера, прислать ли ещё.")
        except Exception:
            pass

        sent, last_err = _send_products(token, chat_id, products)
        if sent:
            # products with photos, in send order, are the ones actually delivered
            delivered = [p for p in products if p.get("photos")][:sent]
            return (f"Отправил {sent} фото-карточек (по одному сообщению на экспонат).", delivered)
        if last_err:
            return (f"Не удалось отправить карточки: {last_err}", [])
        return (f"По запросу «{query}» нашёл товары, но у них нет фото — нечего отправить.", [])
    except Exception as e:
        return (f"Внутренняя ошибка select_and_send: {e}", [])


def _build_caption(p: dict) -> str:
    name = (p.get("title") or "").strip()
    ptype = (p.get("description") or "").strip()
    price_str = _fmt_price(p.get("price"))
    link = f"https://stargift.ru/product/{p.get('id') or ''}/"
    cap = f"«{name}»"
    if ptype:
        cap += f" — {ptype}"
    cap += f"\nЦена: {price_str}"
    # Наличие обязательно (Вашик, 20.07): галерея или «под заказ»
    av = str(p.get("availability") or "").strip()
    gal = (p.get("gallery") or "").strip()
    if av in ("preorder", "outofstock"):
        cap += "\nПод заказ"
    elif gal:
        cap += f"\nВ наличии: галерея «{gal}»"
    elif av and av != "0":
        cap += "\nВ наличии"
    cap += f"\n{link}"
    return cap[:997] + "…" if len(cap) > 1000 else cap


def _send_products(token: str, chat_id: str, products: list):
    """Send each product as its own Telegram photo message. Returns (sent_count, last_err)."""
    tg_url = f"https://api.telegram.org/bot{token}/sendPhoto"
    sent = 0
    last_err = None
    for p in products:
        photos = p.get("photos") or []
        if not photos:
            # Позиция без фото — не теряем молча (кейс Бекхэма 20.07): шлём текстом.
            try:
                _payload = {"chat_id": chat_id,
                            "text": "📄 Без фото на сайте:\n" + _build_caption(p)}
                _req = urllib.request.Request(
                    f"https://api.telegram.org/bot{token}/sendMessage",
                    data=json.dumps(_payload, ensure_ascii=False).encode("utf-8"),
                    headers={"Content-Type": "application/json"}, method="POST",
                )
                with urllib.request.urlopen(_req, timeout=30) as _resp:
                    if json.loads(_resp.read().decode("utf-8")).get("ok"):
                        sent += 1
            except Exception as e:
                last_err = e
            continue
        try:
            payload = {"chat_id": chat_id, "photo": photos[0], "caption": _build_caption(p)}
            body_bytes = json.dumps(payload, ensure_ascii=False).encode("utf-8")
            req = urllib.request.Request(
                tg_url, data=body_bytes,
                headers={"Content-Type": "application/json"}, method="POST",
            )
            with urllib.request.urlopen(req, timeout=30) as resp:
                result = json.loads(resp.read().decode("utf-8"))
            if result.get("ok"):
                sent += 1
            else:
                last_err = result.get("description") or json.dumps(result, ensure_ascii=False)
        except Exception as e:
            last_err = str(e)
        if sent >= 10:
            break
    return sent, last_err


def send_cards_by_names(chat_id: str, names: list) -> str:
    """Send specific products (chosen by the model) as one-photo-per-message cards.
    `names` — list of exact product names/queries; each is resolved to its top catalog match.
    Used after the model explores related terms and curates the relevant exhibits.
    Never raises.
    """
    try:
        token = _telegram_token()
        if not token:
            return "Ошибка: не удалось прочитать TELEGRAM_BOT_TOKEN."
        if not names:
            return "Не передан список товаров."
        seen = set()
        products = []
        for nm in names[:10]:
            try:
                hits = _fetch_products(str(nm), 1, 0)
            except Exception:
                hits = []
            if hits:
                p = hits[0]
                pid = p.get("id")
                if pid and pid not in seen:
                    seen.add(pid)
                    products.append(p)
        if not products:
            return "Не нашёл указанные товары в каталоге."
        # Честность о полноте выдачи (Вашик, 22.07): сколько ещё подходит по теме.
        more_note = ""
        try:
            probe = _fetch_products(query, limit, max_price)
            if min_price > 0:
                probe = [x for x in probe
                         if float(x.get("price") or 0) <= 0 or float(x.get("price") or 0) >= min_price]
            if in_stock_only:
                probe = [x for x in probe
                         if str(x.get("availability") or "").strip() not in ("preorder", "outofstock", "0")]
            total = max(getattr(probe, "total_matched", len(probe)), len(products))
            if len(products) < limit:
                more_note = f" Подходящих под критерии всего {len(products)} — это все."
            elif total > len(products):
                more_note = (f" На эту тему есть ещё подходящие экспонаты (~{total - len(products)}+) — "
                             f"спроси менеджера, прислать ли ещё.")
        except Exception:
            pass

        sent, last_err = _send_products(token, chat_id, products)
        if sent:
            return f"Отправил {sent} фото-карточек (по одному сообщению на экспонат).{more_note}"
        return f"Не удалось отправить: {last_err or 'нет фото у выбранных товаров'}."
    except Exception as e:
        return f"Внутренняя ошибка send_cards: {e}"
