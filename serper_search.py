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
    r"(новост"
    r"|что (?:сейчас |там )?(?:происходит|случилось|нового)|что нового"
    r"|(?:последн|свеж|актуальн)\w* (?:новост|событ|данн|информац|курс|цен)"
    r"|курс\w* (?:доллар|евро|юан|биткоин|bitcoin|эфир|валют|рубл)"
    r"|котировк|погод"
    r"|результат\w* (?:матч|игр|выбор)|счёт матч|счет матч|кто (?:выиграл|победил)"
    r"|сколько стоит сейчас|какая сейчас цена|цена на .{0,30} сейчас"
    r"|погугли|загугли|поищи|найди в (?:интернете|сети|гугле|google)|посмотри в (?:интернете|сети))",
    re.I,
)

def wants_search(text):
    text = str(text or "").strip()
    if len(text) < 6 or len(text) > 800:
        return False
    return bool(_SEARCH_RE.search(text))


async def search_context(http, query, max_results=5):
    key = os.getenv("SERPER_API_KEY")
    if not key:
        log.warning("serper search: переменная SERPER_API_KEY не задана")
        return None
    query = " ".join(str(query or "").split())[:300]
    if not query:
        return None

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
        return None

    if resp.status_code != 200:
        log.warning("serper search: HTTP %s %s", resp.status_code, (resp.text or "")[:150])
        return None
    try:
        data = resp.json()
    except ValueError:
        log.warning("serper search: ответ не JSON")
        return None

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
        return None
    return f"Результаты поиска по запросу: {query}\n" + "\n".join(lines)
