import os
import httpx
import logging

log = logging.getLogger("extras")

SEARCH_WEIGHT = 2

def wants_search(text: str) -> bool:
    """Определяет, нужен ли поиск в интернете."""
    if not text:
        return False
    t = text.lower()
    triggers = [
        "найди", "поищи", "погугли", "загугли", "новости", "последние", 
        "курс", "погода", "актуальн", "что происходит", "кто такой", 
        "сколько стоит", "в интернете"
    ]
    return any(w in t for w in triggers)

async def search_context(http: httpx.AsyncClient, query: str, max_results: int = 4) -> str:
    """Делает запрос к API срезAI и возвращает текст с результатами."""
    api_key = os.getenv("SREZAI_API_KEY")
    if not api_key:
        log.warning("SREZAI_API_KEY не задан, поиск пропущен")
        return ""

    url = "https://api.srezai.ru/v1/search"  # Проверь точный URL в их документации!
    headers = {
        "Authorization": f"Bearer {api_key}",
        "Content-Type": "application/json"
    }
    payload = {
        "query": query,
        "num": max_results,
        "excerpts": True
    }

    try:
        r = await http.post(url, headers=headers, json=payload, timeout=15.0)
        r.raise_for_status()
        data = r.json()
        
        results = data.get("results", [])
        if not results:
            return ""

        parts = []
        for i, item in enumerate(results[:max_results], 1):
            title = item.get("title", "Без названия")
            snippet = item.get("snippet") or item.get("excerpt") or item.get("content", "")
            link = item.get("url", "")
            parts.append(f"{i}. {title}\n{snippet}\nИсточник: {link}")
        
        return "\n\n".join(parts)
    except Exception as e:
        log.warning(f"srezai search: сервис недоступен ({e})")
        return ""

def docx_to_text(b64: str, limit: int):
    return None, "Чтение Word отключено."

def xlsx_to_text(b64: str, limit: int):
    return None, "Чтение Excel отключено."
