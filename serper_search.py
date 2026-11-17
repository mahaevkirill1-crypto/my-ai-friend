"""Поиск в интернете через Serper.dev.

Ключ берётся из переменной окружения SERPER_API_KEY (в Render или в .env), в коде его нет.
search_context(http, query) возвращает строку для вставки в промпт или None, если поиск не сработал.
"""
import os
import re
import logging

import httpx

log = logging.getLogger("holo")

SERPER_URL = "https://google.serper.dev/search"
SERPER_TIMEOUT = 10          # секунд на запрос: поиск не должен подвешивать ответ бота
SNIPPET_MAX = 300            # длина одного сниппета в промпте
SEARCH_WEIGHT = 2            # во сколько запросов тарифа обходится сообщение с поиском


# Когда включать поиск: новости, свежие события, курсы, погода, результаты матчей и прямые просьбы «найди / загугли».
# Специально узко, чтобы не искать в интернете на каждый вопрос.
_SEARCH_RE = re.compile(
    r"(новост|свежак"
    r"|что (?:\w+ ){0,3}(?:происходит|случилось|нового)|что нового|как дела в мире"
    r"|(?:последн|свеж|актуальн)\w* (?:новост|событ|данн|информац|курс|цен|вест)"
    r"|курс\w* (?:доллар|евро|юан|биткоин|bitcoin|эфир|валют|рубл|тенге|гривн)"
    r"|котировк|погод"
    r"|результат\w* (?:матч|игр|выбор)|счёт матч|счет матч|кто (?:выиграл|победил)"
    r"|сколько стоит сейчас|какая сейчас цена|цена на .{0,30} сейчас"
    r"|(?:кто|какой|какая|какое|сколько|где|когда)\b.{0,40}\b(?:сейчас|сегодня|вчера|на данный момент|в этом году|в 202\d)"
    r"|(?:найди|найти|поищи|посмотри|проверь|узнай|глянь|загугли|погугли|выйди|зайди|загляни)\w*.{0,30}(?:интернет|в сети|гугл|google|поиск)"
    r"|погугли|загугли|поищи)",
    re.I,
)

def wants_search(text):
    text = str(text or "").strip()
    if len(text) < 6 or len(text) > 800:
        return False
    return bool(_SEARCH_RE.search(text))


# --- упрощение запроса для повторной попытки: «что вчера сказал Илон Маск» -> «Илон Маск новости» ---
_DROP_RE = re.compile(
    r"^(что|как|какие|какой|какая|какое|какую|кто|где|когда|почему|зачем|сколько|расскажи\w*|скажи|покажи|найди|найти|поищи|погугли|загугли|посмотри|проверь|узнай|глянь"
    r"|вчера|сегодня|сейчас|недавно|вчерашн\w*|сегодняшн\w*|последн\w*|свеж\w*|актуальн\w*|нов(?:ое|ого|ый|ая|ые|ых)|только|уже"
    r"|на|в|во|о|об|про|по|и|а|но|ли|же|бы|мне|мой|моя|нам|пожалуйста|плиз|ты|вы|там|это|эти|этот|за|из|от|у|к|с|со|для|можешь|можно|ещё|еще"
    r"|интернет\w*|сети|гугл\w*|информаци\w+|данн\w+|сказал\w*|заявил\w*|объявил\w*|анонсировал\w*|выпустил\w*|написал\w*|ответил\w*"
    r"|случил\w*|произошл\w*|происход\w*|делал\w*)$",
    re.I,
)
_EVENT_RE = re.compile(r"сказал|заявил|объявил|анонсировал|выпустил|написал|ответил|случил|произошл|происход|делал|нового|новое", re.I)

def simplify_query(query):
    """Убирает время, вопросительные и служебные слова. Если вопрос был про событие или слова человека, добавляет «новости».
    Возвращает None, если упростить нечего."""
    q = " ".join(str(query or "").split())
    words = re.findall(r"[\w\-]+", q, re.U)
    keep = [w for w in words if not _DROP_RE.match(w)]
    if not keep:
        return None
    out = " ".join(keep)
    if _EVENT_RE.search(q) and "новост" not in out.lower():
        out += " новости"
    out = out.strip()
    if len(out) < 3 or out.lower() == q.lower():
        return None
    return out[:200]


async def _serper(http, key, query, max_results):
    """Один запрос. Возвращает (текст | None, статус): ok / empty (ничего не нашлось) / error (ключ, сеть, лимит)."""
    headers = {"X-API-KEY": key, "Content-Type": "application/json"}
    body = {"q": query, "num": 5, "hl": "ru", "gl": "ru"}
    try:
        if http is not None:
            resp = await http.post(SERPER_URL, headers=headers, json=body, timeout=SERPER_TIMEOUT)
        else:
            async with httpx.AsyncClient(timeout=SERPER_TIMEOUT) as client:
                resp = await client.post(SERPER_URL, headers=headers, json=body)
    except Exception as e:
        log.warning("serper search: запрос не удался (%s)", type(e).__name__)
        return None, "error"

    if resp.status_code != 200:
        log.warning("serper search: HTTP %s %s", resp.status_code, (resp.text or "")[:150])
        return None, "error"
    try:
        data = resp.json()
    except ValueError:
        log.warning("serper search: ответ не JSON")
        return None, "error"

    lines = []
    for item in (data.get("organic") or []) if isinstance(data, dict) else []:
        if not isinstance(item, dict):
            continue
        title = " ".join(str(item.get("title") or "").split())
        snippet = " ".join(str(item.get("snippet") or "").split())[:SNIPPET_MAX]
        if not title and not snippet:
            continue
        lines.append(f"{len(lines) + 1}. {title}: {snippet}")
        if len(lines) >= max(1, int(max_results)):
            break
    if not lines:
        return None, "empty"
    return f"Результаты поиска по запросу: {query}\n" + "\n".join(lines), "ok"


async def search_context(http, query, max_results=5):
    key = os.getenv("SERPER_API_KEY")
    if not key:
        log.warning("serper search: переменная SERPER_API_KEY не задана")
        return None
    query = " ".join(str(query or "").split())[:300]
    if not query:
        return None

    text, status = await _serper(http, key, query, max_results)
    if status != "empty":
        return text   # нашли, либо ошибка (ключ, сеть, лимит): повторять запрос нет смысла
    # ничего не нашлось: один раз пробуем упрощённую формулировку
    simple = simplify_query(query)
    if not simple:
        return None
    log.info("serper search: по запросу ничего нет, повторяю упрощённо: %r", simple)
    text, _ = await _serper(http, key, simple, max_results)
    return text
