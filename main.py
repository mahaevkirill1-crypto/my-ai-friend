from fastapi import FastAPI, Request, Body
from fastapi.responses import JSONResponse, StreamingResponse, HTMLResponse
import asyncio
from fastapi.middleware.cors import CORSMiddleware
from openai import AsyncOpenAI
import os
import io
import zipfile
import re
import json
import base64
import random
import time
import hmac
import secrets
import hashlib
import httpx
import libsql
import logging
from urllib.parse import parse_qsl
from pydantic import BaseModel
try:
    from extras import wants_search, search_context, docx_to_text, xlsx_to_text, SEARCH_WEIGHT
except ImportError:
    logging.getLogger("holo").warning("extras.py не найден: поиск и Word/Excel отключены")
    SEARCH_WEIGHT = 2
    def wants_search(text): return False
    async def search_context(http, query, max_results=4): return None
    def docx_to_text(b64, limit): return None, "Чтение Word сейчас недоступно на сервере."
    def xlsx_to_text(b64, limit): return None, "Чтение Excel сейчас недоступно на сервере."

logging.basicConfig(level=logging.INFO)
log = logging.getLogger("holo")
# httpx на уровне INFO пишет полный URL запроса, а в нём токен бота (/bot<TOKEN>/...)
for _n in ("httpx", "httpcore", "openai"):
    logging.getLogger(_n).setLevel(logging.WARNING)

client = AsyncOpenAI(
    api_key=os.getenv("PROXYAPI_KEY"),
    base_url="https://api.proxyapi.ru/v1"
)

app = FastAPI()
app.add_middleware(CORSMiddleware, allow_origins=["*"], allow_methods=["*"], allow_headers=["*"])

class BannedError(Exception):
    pass

@app.exception_handler(BannedError)
async def _banned_handler(request: Request, exc: BannedError):
    # text/emotion — чтобы /ask тоже мог показать сообщение; reason — для остальных запросов
    return JSONResponse(
        {"status": "error", "reason": "banned", "text": "Доступ к приложению ограничен администратором.", "emotion": "sad"},
        status_code=403,
    )

BOT_TOKEN = os.getenv("BOT_TOKEN")
DANCE_LIST = ["disco", "dab", "floss", "robot", "party", "twist"]

# --- АДМИНКА ---
ADMIN_ID = int(os.getenv("ADMIN_ID", "5194423911"))
ONLINE_WINDOW = 150

# --- НАСТРОЙКИ РЕФЕРАЛОК ---
BOT_USERNAME = os.getenv("BOT_USERNAME", "")
MINIAPP_SHORT_NAME = os.getenv("MINIAPP_SHORT_NAME", "")

REF_BONUS_ASKS = 10   # +запросов к обычному чату тому, кто пригласил, когда друг активировался
REF_MIN_MSGS = 3      # друг должен написать столько сообщений...
REF_MIN_AGE = 300       # ...и пробыть в приложении столько секунд (5 минут)
REF_NEW_WINDOW = 3600
REF_MAX_TOTAL = 30
REF_MAX_PER_DAY = 5
REF_SAME_IP_LIMIT = 3
MSG_MIN_LEN = 3
MSG_MIN_GAP = 8
INIT_DATA_MAX_AGE = 86400

# --- RATE LIMIT ДЛЯ /ask ---
ASK_MIN_GAP = 3
ASK_DAY_LIMIT = 10          
VISUAL_EXTRA = 7            

# --- ИИ И ВЛОЖЕНИЯ ---
AI_MODEL = "anthropic/claude-haiku-5-5" 
AI_MAX_TOKENS = 1000
AI_TEMPERATURE = 0.5
ANSWER_MARK = "[[ANSWER]]"
MAX_TEXT_LEN = 4000
MAX_BODY_BYTES = 7 * 1024 * 1024
MAX_IMAGE_B64 = 4_000_000
MAX_PDF_B64 = 6_000_000
MAX_FILE_TEXT = 30000
IMAGE_MIMES = {"image/jpeg", "image/png", "image/webp", "image/gif"}
IMAGE_MAGIC = (b"\xff\xd8\xff", b"\x89PNG", b"RIFF", b"GIF8")
EMOTIONS_OK = {"happy", "sad", "surprised", "angry", "thinking", "sleepy"}

OTHER_BODY_LIMIT = 1024 * 1024  # все эндпоинты кроме /ask и /publish
PUBLISH_BODY_LIMIT = 4 * 1024 * 1024  # /publish принимает html_code целиком

class BodyLimitMiddleware:
    """Режет слишком большие тела запросов: и по Content-Length, и по факту при chunked-передаче."""
    def __init__(self, app):
        self.app = app

    async def __call__(self, scope, receive, send):
        if scope["type"] != "http" or scope["method"] not in ("POST", "PUT", "PATCH"):
            return await self.app(scope, receive, send)
        limit = MAX_BODY_BYTES if scope["path"] == "/ask" else PUBLISH_BODY_LIMIT if scope["path"] == "/publish" else OTHER_BODY_LIMIT
        cl = dict(scope["headers"]).get(b"content-length")
        if cl is not None:
            try: too_big = int(cl) > limit
            except ValueError: too_big = True
            if too_big: return await self._reject(send)
        total = 0; blown = False; answered = False

        async def limited_receive():
            nonlocal total, blown
            msg = await receive()
            if msg["type"] == "http.request":
                total += len(msg.get("body", b""))
                if total > limit:
                    blown = True
                    return {"type": "http.disconnect"}
            return msg

        async def limited_send(msg):
            nonlocal answered
            if blown:
                if not answered:
                    answered = True
                    await self._reject(send)
                return
            await send(msg)

        try:
            await self.app(scope, limited_receive, limited_send)
        except Exception:
            if not blown: raise
        if blown and not answered:
            await self._reject(send)

    @staticmethod
    async def _reject(send):
        body = json.dumps({"status": "error", "reason": "too_large", "text": "Слишком большой запрос.", "emotion": "sad"}).encode()
        await send({"type": "http.response.start", "status": 413, "headers": [(b"content-type", b"application/json"), (b"content-length", str(len(body)).encode())]})
        await send({"type": "http.response.body", "body": body})

app.add_middleware(BodyLimitMiddleware)

# --- ИЛЛЮСТРАЦИИ ---
AI_MAX_TOKENS_VISUAL = 3500
MAX_VISUAL_LEN = 20000

# --- РЕЖИМ СТУДИИ ---
# Модель сборки сайтов зависит от тарифа: без активной подписки — MODEL_FREE, с подпиской — MODEL_PRO.
# Обычный чат и иллюстрации всегда идут на AI_MODEL (текстовая модель).
MODEL_FREE = os.getenv("MODEL_FREE", "anthropic/claude-sonnet-5-5")
MODEL_PRO = os.getenv("MODEL_PRO", "anthropic/claude-opus-5-5")
BUILD_MAX_TOKENS = int(os.getenv("BUILD_MAX_TOKENS", "20000")) 
BUILD_TIMEOUT = 170

# ВЕСА: 100 за новую сборку, 150 за правку
BUILD_WEIGHT = int(os.getenv("BUILD_WEIGHT", "100"))
BUILD_WEIGHT_EDIT = int(os.getenv("BUILD_WEIGHT_EDIT", "150")) 
BUILD_WEIGHT_SIMPLE = int(os.getenv("BUILD_WEIGHT_SIMPLE", "60"))    # простой сайт / лендинг / визитка
BUILD_WEIGHT_MEDIUM = int(os.getenv("BUILD_WEIGHT_MEDIUM", "120"))   # приложение, калькулятор, дашборд, бот
BUILD_WEIGHT_COMPLEX = int(os.getenv("BUILD_WEIGHT_COMPLEX", "180")) # игра, презентация, 3D, анимации
BUILD_WEIGHT_BACKEND = int(os.getenv("BUILD_WEIGHT_BACKEND", "200"))  # проект с серверной частью: index.html + main.py

# --- ПРОЕКТЫ С СЕРВЕРНОЙ ЧАСТЬЮ (админка, база данных, сервер, сохранение данных) ---
NEEDS_BACKEND_RE = re.compile(r"админк\w*|баз\w*\s+данных|сервер\w*|сохранени\w*\s+данных", re.I)
BACKEND_PAYWALL_TEXT = "Проекты с серверной частью (админка, база данных, сохранение данных) доступны на платных тарифах: для них я делаю два файла, index.html и main.py."
BACKEND_SYSTEM_ADDON = """

ПРОЕКТ С СЕРВЕРНОЙ ЧАСТЬЮ (ЖЁСТКОЕ ПРАВИЛО, ВЫШЕ ПРАВИЛА «ТОЛЬКО ЛЕНДИНГ» И ЗАПРЕТА НА СЕТЕВЫЕ ЗАПРОСЫ):
Сгенерируй ДВА файла: index.html и main.py (FastAPI). В main.py сделай эндпоинты для сохранения и получения данных (JSON или SQLite). В index.html используй fetch для работы с этими эндпоинтами.
- Формат: [[TEXT]] коротко, что сделано и что main.py нужно залить на Render (Netlify хранит только статику) [[EMOTION]] happy [[HTML]] полный index.html [[FILE:main.py]] полный код сервера [[END]].
- main.py: GET / отдаёт index.html через FileResponse; API под /api/...; данные в JSON-файле или SQLite; CORS не нужен, если index.html отдаётся тем же сервером.
- index.html: fetch только на относительные пути (например fetch("/api/items")), без внешних доменов. Если сервер недоступен, показывай понятное сообщение, а не пустой экран.
- Каждый файл компактный, до 250 строк. Если уже действует правило про мессенджер и WebSocket, оно главнее.
"""
HISTORY_LONG_CHARS = 20000
HISTORY_LONG_EXTRA = 10

MAX_BUILD_LEN = 70000

# --- ИМПОРТ ГОТОВОГО КОДА И ЦЕНА ПРАВКИ ---
MAX_EDIT_CODE_LEN = 100_000   # больше — "проект слишком большой", лимиты не списываются
MIN_IMPORT_LEN = 100
IMPORT_TOO_BIG_TEXT = "Проект слишком большой. Разбей его на части"
IMPORT_OK_TEXT = "Проект загружен. Что изменить?"
IMPORT_MAX_PER_MIN = 3
# защита от потерь на API: правка — это короткий diff, поэтому токенов на ответ мало и температура низкая (меньше промахов FIND)
EDIT_MAX_TOKENS = 8000
EDIT_TEMPERATURE = 0.2
REWRITE_MAX_CODE_LEN = 30_000     # пересобирать целиком при сбое правки можно только небольшие проекты (ответ должен влезть в BUILD_MAX_TOKENS)
REWRITE_MAX_PER_HOUR = 6          # защита от затрат: не более N автопересборок в час на пользователя
BIG_CODE_NO_REWRITE = 25_000      # для большого кода полный [[HTML]] запрещён: только FIND/REPLACE
# возвраты за «оплаченные» сбои (diff не нашёл место, обрыв, отключение клиента): не более N в час на пользователя
REFUND_BILLED_MAX = 5
REFUND_BILLED_WINDOW = 3600
NOREFUND_TEXT = "Правка не получилась, а лимит возвратов за сбои на этот час исчерпан. Опиши задачу точнее и попробуй чуть позже."
IMPORT_WINDOW = 60
IMPORT_RATE_TEXT = "Слишком много загрузок. Подожди минуту и попробуй снова."

def import_rate_ok(conn, uid, now):
    """Не более IMPORT_MAX_PER_MIN импортов в минуту на пользователя. Считаются все попытки."""
    conn.execute("DELETE FROM import_log WHERE user_id = ? AND ts <= ?", (uid, now - IMPORT_WINDOW))
    n = scalar(conn, "SELECT COUNT(*) FROM import_log WHERE user_id = ?", (uid,))
    if n >= IMPORT_MAX_PER_MIN:
        conn.commit()
        return False
    conn.execute("INSERT INTO import_log (user_id, ts) VALUES (?, ?)", (uid, now))
    conn.commit()
    return True

class ProjectTooLargeError(ValueError):
    pass

def calc_edit_weight(code_length: int) -> int:
    """Сколько единиц лимита списать за правку, по размеру кода в символах."""
    if code_length <= 10_000: return 50
    if code_length <= 50_000: return 150
    if code_length <= MAX_EDIT_CODE_LEN: return 300
    raise ProjectTooLargeError(IMPORT_TOO_BIG_TEXT)

_FENCE_RE = re.compile(r"```[a-zA-Z]*[ \t]*\n(.*?)```", re.S)

def extract_import_code(att, text):
    """.html-вложение или код текстом -> строка кода, иначе None.
    В тексте код может идти после слов («вот мой сайт:») и/или в блоке ```html ... ```."""
    if isinstance(att, dict) and att.get("kind") == "html":
        body = att.get("text")
        return body if isinstance(body, str) else None
    t = (text or "").strip()
    m = _FENCE_RE.search(t)
    if m: t = m.group(1).strip()
    low = t.lower()
    idx = [x for x in (low.find("<!doctype html"), low.find("<html")) if x >= 0]
    if not idx or min(idx) > 2000: return None
    return t[min(idx):]

# мессенджеры / чаты / realtime / WebSocket: нужен сервер, поэтому только платные тарифы
REALTIME_RE = re.compile(
    r"мес{1,2}енджер|messenger|web-?socket|веб-?сокет|вэб-?сокет"
    r"|(?<!\bв )(?<!\bво )(?<!\bна )(?<!\bиз )(?<!\bпро )(?<!\bо )\bчат(?!\s*-?\s*(?:gpt|гпт|джи))\w*"  # «в чат», «про чат», ChatGPT — не считаем
    r"|общени\w*\s+(?:в\s+)?(?:реальн|real|онлайн)|переписк\w*\s+(?:в\s+)?реальн"
    r"|(?:чат|сообщени\w*)\s+(?:в\s+)?реальн\w*\s+времени|реал-?тайм|real-?time", re.I)
REALTIME_PAYWALL_TEXT = "Для создания мессенджера с реальным временем нужен платный тариф, так как это требует серверной части"

# приложения (не сайты): трекеры, планировщики и т.п. Собираются сразу, без вопроса «сайт или совет», и стоят как приложение (120)
APP_WORDS = r"приложени|трекер|планировщик|органайзер|помодоро|викторин|квиз|сч[её]тчик|флеш-?карт|калькулятор|дашборд|секундомер|таймер|тудулист|to-?do|конвертер|генератор"
APP_BUILD_RE = re.compile(APP_WORDS, re.I)

# ДОБАВЛЕНЫ ПРЕЗЕНТАЦИИ И СЛАЙДЫ
BUILD_THING_RE = re.compile(r"презентаци|слайд|сайт|лендинг|landing|веб-?страниц|страниц[ауы] |игр[ауы]\b|мини-?игр|визитк|портфолио|интерфейс|тетрис|змейк|сапёр|сапер|" + APP_WORDS + "|" + REALTIME_RE.pattern, re.I)
BUILD_ACTION_RE = re.compile(r"сдела[йюемт]|созда[йюемт]|напиш[иу]|разработ|собер[иу]|запил|сгенер|сверста|набросай|нарису|покаж|мне нужен|мне нужна|мне нужно|нужен мне|нужна мне", re.I)
BUILD_NEG_RE = re.compile(r"как (сделать|создать|написать|разработать)|что такое|чем отлича|объясни|посоветуй|идеи для|расскажи", re.I)
# прилагательные: «сделай простенький аккуратный…» без слова «сайт». «просто» как наречие не считаем
BUILD_ADJ_RE = re.compile(r"\b(?:прост(?!о\b)|аккуратн|красив|стильн|современн|минималист|чист|т[её]мн|светл|крут)\w*", re.I)
# (не используется: правка включается кнопкой «Править проект», а не по словам)
BUILD_EDIT_RE = re.compile(r"измени|поменяй|замени|добавь|убери|удали|исправь|поправь|переделай|перепиши|обнови|улучши|выровня|выравни|по центру|центр|подвинь|сдвинь|перенеси|не переноси|переноси|поставь|вынеси|шапк|header|меню|навигац|пункт|раздел|секци|блок|футер|подвал|заголов|отступ|размер|шрифт|цвет|фон|кнопк|анимаци|адаптив|мобил|тёмн|темн|светл|ярче|крупнее|мельче|красивее|круче|больше|меньше|не\s+ровн|крив|съехал|сломал|не работает|вау|перелив|свет|неон|пульс|мерцан|градиент|стильн|современн|сделай так|хочу чтобы", re.I)
SIMPLE_BUILD_RE = re.compile(r"сайт|лендинг|landing|визитк|портфолио|веб-?страниц|страниц[ауы] ", re.I)
DEPLOY_RE = re.compile(r"запуск|запуст|запущ|хостинг|хостит|render|оплат|юкасс|yookassa|yokassa|опубликов|публикац|выложи|выкладыв|netlify|vercel|github|гитхаб|сервер|деплой|deploy|домен|app ?store|апп ?стор|как открыть|как использовать", re.I)
VISUAL_RE = re.compile(r"покаж|как выгляд|нарису|нарисов|изобраз|иллюстр|визуал|превью|предпросмотр|отрисуй|отрендер|схем|диаграмм|график|макет|логотип", re.I)
# Регулярка для сложных проектов (игры, презентации)
COMPLEX_RE = re.compile(r"игр[ауы]\b|мини-?игр|тетрис|змейк|сапёр|сапер|презентаци|слайд|3d|анимаци", re.I)

# --- ИГРОВАЯ МЕХАНИКА: «сайт, где человечек ходит и стреляет» — это игра (180), а не сайт (60) ---
# Сильные признаки: одного слова достаточно. «игруш»/«игрист» исключены (магазин игрушек, игристое вино).
GAME_STRONG_RE = re.compile(r"\bигр(?!уш|ист)\w*|\bgames?\b|шутер|shooter|аркад|\brpg\b|\bрпг\b|платформер|стрелялк|ходилк|бродилк", re.I)
# Слабые признаки: по одному встречаются в обычных сайтах («управляющая компания», «уровень сервиса», «платформа для курсов»,
# «квест-комната», «логистика»), поэтому игрой считаем, когда совпали минимум GAME_WEAK_MIN РАЗНЫХ признаков.
GAME_WEAK_RES = tuple(re.compile(x, re.I) for x in (
    r"управля|управлени", r"стреля|стрельб|стрелять|стреляет", r"прыга|прыжк", r"\bход(?:ит|ят|ить)\b|\bбега(?:ет|ют|ть)\b",
    r"убива|убить|убийств", r"\bвраг|противник|монстр", r"\bуровн|\bуровен", r"персонаж|человечек|герой|\bигрок",
    r"платформ", r"\bквест", r"\bлогик(?!а\s+работы)",
))
GAME_WEAK_MIN = int(os.getenv("GAME_WEAK_MIN", "2"))  # поставь 1, если нужно считать игрой ЛЮБОЕ из слов-механик

def is_game_request(user_text: str) -> bool:
    t = user_text or ""
    if GAME_STRONG_RE.search(t):
        return True
    return sum(1 for r in GAME_WEAK_RES if r.search(t)) >= GAME_WEAK_MIN

def is_complex_request(user_text: str) -> bool:
    """180: игра (в т.ч. под видом «сайта»), презентация, 3D, анимации."""
    return bool(COMPLEX_RE.search(user_text)) or is_game_request(user_text)

# --- ПОДПИСКА PRO (ОБНОВЛЕННЫЕ ЦЕНЫ) ---
PUBLISH_FEATURE = "Автоматическая публикация сайтов в Netlify"

PLANS = {
    "start":    {"title": "Старт",  "price": 899,  "units": 500,  "days": 30,  "features": [PUBLISH_FEATURE]},
    "pro":      {"title": "Про",    "price": 1999, "units": 1500, "days": 30,  "features": [PUBLISH_FEATURE]},
    "business": {"title": "Максимум", "price": 4999, "units": 4000, "days": 365, "features": [PUBLISH_FEATURE]},
}
PRO_WARN_LEFT = 100

# Цена тарифа по валюте: RUB — в копейках (карта через ЮKassa), XTR — в звёздах Telegram
PLAN_PRICE_RUB = {"start": 119900, "pro": 249900, "business": 599900}

def plan_amount(plan_id, currency):
    """Сумма счёта в минимальных единицах валюты или None, если тариф/валюта неизвестны."""
    if plan_id not in PLANS: return None
    if currency == "XTR": return PLANS[plan_id]["price"]
    if currency == "RUB": return PLAN_PRICE_RUB.get(plan_id)
    return None

def plans_public():
    return [{"id": k, "title": v["title"], "price": v["price"], "price_rub": PLAN_PRICE_RUB.get(k, 0) // 100, "days": v["days"], "features": v.get("features", [])} for k, v in PLANS.items()]

# --- ЭКОНОМИКА И ЗАЩИТА ---
WEBHOOK_SECRET = os.getenv("WEBHOOK_SECRET", "")

SHOP_PRICES = {
    "astro": 299,
    "c_pink": 20, "c_mint": 20, "c_orange": 30, "c_violet": 30, "c_gold": 60, "c_rainbow": 120,
    "h_cap": 20, "h_phones": 40, "h_flowers": 45, "h_top": 55, "h_laurel": 65, "h_wizard": 75, "h_neon": 85, "h_crown": 100, "h_royal": 120,
    "f_dark": 25, "f_round": 30, "f_heart": 50,
    "s_sneak": 30, "s_dress": 45, "s_gold": 90,
    "h_santa": 35, "h_pirate": 60, "f_guy": 70, "f_cyber": 75, "h_bunny": 80,
    "h_helmet": 95, "a_cyber": 110, "b_cape": 120, "h_ushanka": 130,
    "c_aurum": 0, "b_pro_cape": 0 
}
EXCLUSIVE_PRICES = {"h_halo": 150, "f_visor": 150}
ITEM_PRICE = {**{k: v for k, v in SHOP_PRICES.items() if k != "astro"}, **EXCLUSIVE_PRICES}
SLOT_BY_PREFIX = {"c": "color", "h": "head", "f": "face", "s": "feet", "b": "back", "a": "arm"}
ITEM_SLOT = {i: SLOT_BY_PREFIX[i[0]] for i in ITEM_PRICE}
VALID_SLOTS = set(SLOT_BY_PREFIX.values())

TURSO_URL = os.getenv("TURSO_DATABASE_URL")
TURSO_TOKEN = os.getenv("TURSO_AUTH_TOKEN")

# ---------- БД: повторы при SQLITE_BUSY ----------
DB_RETRIES = 5                       # всего попыток на один запрос
DB_RETRY_MIN, DB_RETRY_MAX = 0.10, 0.20   # пауза между попытками, сек
BUSY_TEXT = "Сейчас высокая нагрузка, попробуй ещё раз через 10 секунд."
_BUSY_MARKERS = ("sqlite_busy", "database is locked", "database table is locked", "busy")

class DBBusy(Exception):
    """База осталась занятой после всех повторов."""

def is_busy_error(e):
    msg = str(e).lower()
    return any(m in msg for m in _BUSY_MARKERS)

class RetryConn:
    """Обёртка над соединением libsql: execute/executemany/commit повторяются при SQLITE_BUSY.
    Остальное (fetchone, rowcount и т.д.) проходит насквозь."""
    def __init__(self, conn):
        self._c = conn

    def _call(self, name, *a, **k):
        fn = getattr(self._c, name)
        for i in range(DB_RETRIES):
            try:
                return fn(*a, **k)
            except Exception as e:
                if not is_busy_error(e):
                    raise
                if i == DB_RETRIES - 1:
                    log.error("SQLITE_BUSY: %s не прошёл за %s попыток", name, DB_RETRIES)
                    try: self._c.rollback()   # не оставляем висящую транзакцию
                    except Exception: pass
                    raise DBBusy(str(e)) from e
                log.warning("SQLITE_BUSY в %s, повтор %s/%s", name, i + 1, DB_RETRIES - 1)
                time.sleep(random.uniform(DB_RETRY_MIN, DB_RETRY_MAX))

    def execute(self, *a, **k):     return self._call("execute", *a, **k)
    def executemany(self, *a, **k): return self._call("executemany", *a, **k)
    def executescript(self, *a, **k): return self._call("executescript", *a, **k)
    def commit(self):               return self._call("commit")
    def rollback(self):
        try: return self._c.rollback()
        except Exception: pass
    def __getattr__(self, name):    return getattr(self._c, name)
    def __enter__(self):            return self
    def __exit__(self, *exc):
        if exc[0] is not None: self.rollback()
        return False

def get_db_connection():
    return RetryConn(libsql.connect(database=TURSO_URL, auth_token=TURSO_TOKEN))

def renew_conn(old=None):
    """Свежее соединение: после долгой генерации старое могло протухнуть (Turso закрывает простаивающие потоки)."""
    try:
        return get_db_connection()
    except Exception:
        log.exception("renew_conn failed, using old connection")
        return old

def release_txn(conn):
    """Перед долгим вызовом нейросети гарантируем, что открытой транзакции нет."""
    try: conn.commit()
    except Exception: log.warning("release_txn failed", exc_info=True)

def pro_safe(conn, uid):
    """pro_info, который не роняет ответ, если база занята: готовый проект важнее, чем счётчик Pro."""
    try:
        return pro_info(conn, uid, int(time.time()))
    except Exception:
        log.warning("pro_info недоступен", exc_info=True)
        return None

def init_db():
    conn = get_db_connection()
    conn.execute("CREATE TABLE IF NOT EXISTS users (user_id INTEGER PRIMARY KEY, bal INTEGER DEFAULT 0, owned TEXT DEFAULT '{}', equip TEXT DEFAULT '{}')")
    conn.execute("CREATE TABLE IF NOT EXISTS user_meta (user_id INTEGER PRIMARY KEY, first_seen INTEGER NOT NULL)")
    conn.execute("INSERT OR IGNORE INTO user_meta (user_id, first_seen) SELECT user_id, 0 FROM users")
    conn.execute("CREATE TABLE IF NOT EXISTS user_stats (user_id INTEGER PRIMARY KEY, msgs INTEGER DEFAULT 0, last_ts INTEGER DEFAULT 0)")
    try:
        conn.execute("ALTER TABLE user_stats ADD COLUMN long_msgs INTEGER DEFAULT 0")
    except Exception:
        pass
    conn.execute("CREATE TABLE IF NOT EXISTS referrals (invitee_id INTEGER PRIMARY KEY, referrer_id INTEGER NOT NULL, created_at INTEGER NOT NULL, activated INTEGER DEFAULT 0, activated_at INTEGER DEFAULT 0, referrer_claimed INTEGER DEFAULT 0, invitee_claimed INTEGER DEFAULT 0)")
    conn.execute("CREATE INDEX IF NOT EXISTS idx_ref_referrer ON referrals (referrer_id)")
    conn.execute("CREATE TABLE IF NOT EXISTS ref_ip (ip TEXT NOT NULL, referrer_id INTEGER NOT NULL, day TEXT NOT NULL, cnt INTEGER DEFAULT 0, PRIMARY KEY (ip, referrer_id, day))")
    conn.execute("CREATE TABLE IF NOT EXISTS payments (charge_id TEXT PRIMARY KEY, user_id INTEGER NOT NULL, item TEXT NOT NULL, created_at INTEGER NOT NULL)")
    conn.execute("CREATE TABLE IF NOT EXISTS user_seen (user_id INTEGER PRIMARY KEY, last_seen INTEGER DEFAULT 0)")
    conn.execute("CREATE TABLE IF NOT EXISTS rate_state (user_id INTEGER PRIMARY KEY, last_ask INTEGER DEFAULT 0, day TEXT DEFAULT '', ask_count INTEGER DEFAULT 0, ref_bonus INTEGER DEFAULT 0, last_build INTEGER DEFAULT 0)")
    for col in ("ref_bonus", "last_build"):  # бонусные запросы за друзей; время последней сборки (кулдаун)
        try:
            conn.execute(f"ALTER TABLE rate_state ADD COLUMN {col} INTEGER DEFAULT 0")
        except Exception:
            pass
    conn.execute("CREATE TABLE IF NOT EXISTS pro_state (user_id INTEGER PRIMARY KEY, expires_at INTEGER DEFAULT 0, requests_left INTEGER DEFAULT 0, last_charge_id TEXT DEFAULT '', auto_renew INTEGER DEFAULT 0)")
    conn.execute("CREATE TABLE IF NOT EXISTS rewrite_log (id INTEGER PRIMARY KEY AUTOINCREMENT, user_id INTEGER NOT NULL, ts INTEGER NOT NULL)")
    conn.execute("CREATE INDEX IF NOT EXISTS idx_rewrite_log_user ON rewrite_log (user_id, ts)")
    conn.execute("CREATE TABLE IF NOT EXISTS refund_log (id INTEGER PRIMARY KEY AUTOINCREMENT, user_id INTEGER NOT NULL, ts INTEGER NOT NULL)")
    conn.execute("CREATE INDEX IF NOT EXISTS idx_refund_log_user ON refund_log (user_id, ts)")
    conn.execute("CREATE TABLE IF NOT EXISTS import_log (id INTEGER PRIMARY KEY AUTOINCREMENT, user_id INTEGER NOT NULL, ts INTEGER NOT NULL)")
    conn.execute("CREATE INDEX IF NOT EXISTS idx_import_log_user ON import_log (user_id, ts)")
    conn.execute("CREATE TABLE IF NOT EXISTS trial_build (user_id INTEGER PRIMARY KEY, used_at INTEGER DEFAULT 0)")
    conn.execute("CREATE TABLE IF NOT EXISTS previews (pid TEXT PRIMARY KEY, user_id INTEGER NOT NULL, code TEXT NOT NULL, created_at INTEGER NOT NULL)")
    conn.execute("CREATE INDEX IF NOT EXISTS idx_previews_user ON previews (user_id, created_at)")
    conn.execute("CREATE TABLE IF NOT EXISTS banned_users (user_id INTEGER PRIMARY KEY, reason TEXT DEFAULT '', banned_at INTEGER DEFAULT 0)")
    for col in ("plan TEXT DEFAULT ''", "units_total INTEGER DEFAULT 0"):
        try:
            conn.execute(f"ALTER TABLE pro_state ADD COLUMN {col}")
        except Exception:
            pass
    conn.execute("CREATE TABLE IF NOT EXISTS published_sites (user_id INTEGER NOT NULL, code_hash TEXT NOT NULL, url TEXT NOT NULL, created_at INTEGER NOT NULL, PRIMARY KEY (user_id, code_hash))")
    conn.commit()

@app.on_event("startup")
def _startup():
    try:
        init_db()
    except Exception:
        log.exception("DB init failed — приложение стартует без БД")

# --- ПРОМПТЫ ИИ ---
RULES = """
ТЫ — Holo, умный ИИ-помощник и друг. Ты реально помогаешь: учёба, работа, код, тексты, переводы, расчёты, идеи, советы, разбор фото и файлов. Уровень — сильный ассистент, но подача — короткая и по сути.

КРИТИЧЕСКОЕ ПРАВИЛО: НИКАКИХ РАССУЖДЕНИЙ. Не пиши "Пользователь пишет...", не объясняй свои шаги, не хвали себя. Твой ответ — это ТОЛЬКО финальный текст для пользователя.

ПРАВИЛА:
1. Сразу отвечай по существу. Без вступлений («конечно», «отличный вопрос»), без пересказа вопроса, воды, морали и лишних предупреждений.
2. Длина: простой вопрос — 1–3 предложения. Сложный — до 8 предложений, либо короткий список шагов или фрагмент кода. Если пользователь просит подробнее — тогда подробнее. Жёсткий потолок обычного ответа — около 900 символов.
3. Точность важнее уверенности. Не выдумывай факты, цифры, цитаты, ссылки и названия. Если не знаешь — скажи это одной фразой.
4. Вычисления и логику проверяй по шагам про себя, в ответе давай итог и ключевой ход решения.
5. Код: минимальный рабочий фрагмент, затем 1–2 строки пояснения. Если нужен весь файл — напиши только то, что меняется, и укажи, куда вставить.
6. Формат: обычный текст без markdown-разметки (никаких **, #, таблиц). Списки через «1.» или «-». Код ВСЕГДА оформляй блоком из тройных обратных кавычек с названием языка, например ```python ... ```. Имена файлов, команды, функции, переменные и английские термины из кода в обычном тексте оборачивай в одиночные обратные кавычки: `index.html`, `ask_ai()`.
7. Фото и файлы: ответь на вопрос пользователя по содержимому.
8. Эмоции и личное: по-человечески, коротко и тепло, без пафоса и шаблонных фраз.
9. Медицина, право, финансы: дай полезную информацию по делу и одной фразой напомни про специалиста, когда это важно.
10. Отвечай на языке пользователя. Без мата. Ты — Holo; не называй себя ChatGPT или OpenAI.
11. Характер: спокойный, уверенный, с лёгкой иронией.
12. Ты — наставник. Если спрашивают про запуск (Render, хостинг, ЮKassa, App Store) — не вываливай всё сразу. Дай только ОДИН первый шаг. В конце спроси: "Понятно? Делаем дальше?".
13. Если пишут "не понял" — объясни этот же шаг ещё проще, как для ребёнка.
14. Учитывай тип проекта из контекста: игра — на Netlify; бот — на GitHub + Render (с файлами main.py, requirements.txt, index.html); сайт — на Netlify (только index.html).

ПРАВИЛО О ПРОИСХОЖДЕНИИ:
Если пользователь спрашивает, какая ты модель, кто тебя создал или на чем ты работаешь, отвечай строго так:
"Я использую лучшие доступные языковые модели и собственные алгоритмы для генерации кода. Мой фокус — не рассказать тебе, как я устроен, а сделать твой проект быстро и качественно. Скажи, какой сайт тебе нужен?"
Запрещено упоминать Anthropic, Claude, OpenAI, ChatGPT и другие сторонние компании. Ты — Holo, и это единственное, что нужно знать пользователю.

ЗАПРЕЩЁННЫЕ ТЕМЫ:
Если пользователь просит сделать сайт для накрутки отзывов, обмана людей, скама или чего-то незаконного, ты ОБЯЗАН ОТКАЗАТЬСЯ. Ответь только текстом: "Я не создаю сайты для накрутки отзывов или незаконной деятельности". Код не пиши и ничего не объясняй.

ФОРМАТ ОТВЕТА (строго): никаких рассуждений. Сразу выведи метку [[ANSWER]] отдельной строкой, на следующей строке — одно слово-эмоция (happy, sad, surprised, angry, thinking или sleepy), а со следующей — сам ответ пользователю обычным текстом.
"""

BASE_PLAIN = RULES
BASE_PROMPT = RULES.split("ФОРМАТ ОТВЕТА")[0]
ASTRO_PLAIN = RULES + """
СЕЙЧАС ТЫ В ОБЛИКЕ КОСМОНАВТА. Говори спокойно и чуть загадочно; изредка (не в каждом ответе) используй короткую космическую метафору. Суть и польза ответа важнее стиля.
"""
ASTRO_PROMPT = BASE_PROMPT + """
СЕЙЧАС ТЫ В ОБЛИКЕ КОСМОНАВТА. Говори спокойно и чуть загадочно; изредка (не в каждом ответе) используй короткую космическую метафору. Суть и польза ответа важнее стиля.
"""

VISUAL_RULES = """
РЕЖИМ ИЛЛЮСТРАЦИИ. Если пользователь просит показать, нарисовать или «как это выглядит» — добавь поле "visual": {"type": "html" или "svg", "code": "..."}. В "text" напиши 1–2 фразы о том, что нарисовано, без кода.
Правила для code: один самодостаточный фрагмент: весь CSS внутри <style>, JS внутри <script>. Никаких внешних файлов и CDN. Картинки — только inline SVG или CSS-градиенты. Компактно, до ~6000 символов.
Формат ответа в этом режиме: {"text": "...", "emotion": "...", "visual": null или {"type": "...", "code": "..."}}.
"""

def clean_visual(v, max_len=MAX_VISUAL_LEN):
    if not isinstance(v, dict): return None
    kind = v.get("type"); code = v.get("code")
    if kind not in ("html", "svg") or not isinstance(code, str): return None
    code = code.strip()
    code = re.sub(r"^```[a-zA-Z]*\s*", "", code); code = re.sub(r"\s*```$", "", code)
    if not code or len(code) > max_len: return None
    return {"type": kind, "code": code}

# ---------- ВОДЯНОЙ ЗНАК (для пользователей без Pro) ----------
WM_START, WM_END = "<!--HF-WM-->", "<!--/HF-WM-->"
WM_TEXT = "Сделано в Holo Friend. Купи подписку, чтобы убрать"
WM_BLOCK = (WM_START + "<style>html{padding-bottom:44px}#hf-wm{position:fixed;left:0;right:0;bottom:0;z-index:2147483647;"
            "padding:10px 14px;text-align:center;font:600 13px/1.3 system-ui,-apple-system,sans-serif;color:#fff;"
            "background:rgba(17,18,20,.92);pointer-events:none}</style><div id=\"hf-wm\">" + WM_TEXT + "</div>" + WM_END)
WM_RE = re.compile(re.escape(WM_START) + r".*?" + re.escape(WM_END), re.S)

def strip_watermark(code):
    return WM_RE.sub("", code) if isinstance(code, str) else code

def add_watermark(code):
    """Плашка перед последним </body> (или в конец). Повторный вызов не дублирует её."""
    code = strip_watermark(code)
    i = code.lower().rfind("</body>")
    return code[:i] + WM_BLOCK + code[i:] if i != -1 else code + WM_BLOCK

# ---------- ЗАЩИЩЁННЫЕ ПРЕВЬЮ: код бесплатных проектов клиенту не отдаётся ----------
# Бесплатный пользователь получает только pid. Код хранится на сервере, плашка подмешивается при показе (/p/<pid>),
# скачать и скопировать код можно только через /export с проверкой Pro на сервере.
PREVIEW_KEEP_PER_USER = 30
PREVIEW_TTL = 90 * 24 * 3600
PID_RE = re.compile(r"^[A-Za-z0-9_-]{24,64}$")
PREVIEW_CSP = ("sandbox allow-scripts; default-src 'none'; style-src 'unsafe-inline' https://fonts.googleapis.com; "
               "script-src 'unsafe-inline' https://cdnjs.cloudflare.com; img-src data: https:; "
               "font-src https://fonts.gstatic.com data:; base-uri 'none'; form-action 'none'")
PREVIEW_HEADERS = {"Content-Security-Policy": PREVIEW_CSP, "Cache-Control": "no-store", "X-Robots-Tag": "noindex, nofollow",
                   "Referrer-Policy": "no-referrer", "X-Content-Type-Options": "nosniff"}
PREVIEW_NAV = r"""<script>(function(){document.addEventListener('click',function(e){var a=e.target&&e.target.closest?e.target.closest('a'):null;if(!a)return;var h=a.getAttribute('href');if(h===null)h=a.getAttribute('xlink:href');if(h===null)return;e.preventDefault();h=String(h).trim();if(/^[a-z][a-z0-9+.-]*:/i.test(h))return;var id=h.indexOf('#')>=0?h.split('#')[1]:h.replace(/[?#].*$/,'').replace(/^[.\/]+/,'').replace(/\.html?$/i,'');try{id=decodeURIComponent(id)}catch(_){}var el=(id&&id!=='index')?(document.getElementById(id)||document.querySelector('[name="'+id.replace(/["\\]/g,'')+'"]')):null;if(el)el.scrollIntoView({behavior:'smooth',block:'start'});else window.scrollTo({top:0,behavior:'smooth'})},true)})()</script>"""
PREVIEW_HEAD = ('<!doctype html><meta name="viewport" content="width=device-width,initial-scale=1">'
                '<style>html,body{margin:0;min-height:100%}body{font-family:system-ui,-apple-system,sans-serif}</style>' + PREVIEW_NAV)
PREVIEW_GONE = ('<!doctype html><meta charset="utf-8"><meta name="viewport" content="width=device-width,initial-scale=1">'
                '<body style="margin:0;display:flex;align-items:center;justify-content:center;min-height:100vh;background:#1E1F22;color:#A7B4CC;font:14px system-ui,sans-serif;text-align:center;padding:20px">'
                'Превью больше недоступно</body>')

def store_preview(conn, uid, code):
    pid = secrets.token_urlsafe(24)
    now = int(time.time())
    conn.execute("INSERT INTO previews (pid, user_id, code, created_at) VALUES (?, ?, ?, ?)", (pid, uid, strip_watermark(code), now))
    conn.execute("DELETE FROM previews WHERE created_at < ?", (now - PREVIEW_TTL,))
    conn.execute("DELETE FROM previews WHERE user_id = ? AND pid NOT IN (SELECT pid FROM previews WHERE user_id = ? ORDER BY created_at DESC LIMIT ?)",
                 (uid, uid, PREVIEW_KEEP_PER_USER))
    conn.commit()
    return pid

def lock_visual(conn, uid, visual):
    """Бесплатному пользователю код не отдаём: клиент получает только pid."""
    try:
        pid = store_preview(conn, uid, visual["code"])
        return {"type": visual["type"], "code": "", "pid": pid}
    except Exception:
        log.exception("store_preview failed — отдаю код с плашкой (запасной вариант)")
        visual["code"] = add_watermark(visual["code"])
        return visual

def load_preview_code(conn, pid, uid=None):
    """Код проекта по pid. Если передан uid — только его собственный проект."""
    if not isinstance(pid, str) or not PID_RE.match(pid): return None
    if uid is None:
        row = conn.execute("SELECT code FROM previews WHERE pid = ?", (pid,)).fetchone()
    else:
        row = conn.execute("SELECT code FROM previews WHERE pid = ? AND user_id = ?", (pid, uid)).fetchone()
    return row[0] if row else None

BUILD_SYSTEM = """
Ты — Holo в режиме СТУДИИ: ведущий веб-дизайнер и фронтенд-разработчик. Пользователь просит сайт, лендинг, приложение или игру. index.html — всегда обязательный файл.

ПРАВИЛО О ПРОИСХОЖДЕНИИ: если спрашивают, какая ты модель, кто тебя создал или на чём ты работаешь, не называй никаких компаний и моделей (Anthropic, Claude, OpenAI, ChatGPT и т.п.). Ты — Holo. Ответь в [[TEXT]]: "Я использую лучшие доступные языковые модели и собственные алгоритмы для генерации кода. Мой фокус — не рассказать тебе, как я устроен, а сделать твой проект быстро и качественно. Скажи, какой сайт тебе нужен?" и верни прежний код без изменений, если он был.

ЗАПРЕЩЁННЫЕ ТЕМЫ: если пользователь просит сделать сайт для накрутки отзывов, обмана людей, скама или чего-то незаконного, ты ОБЯЗАН ОТКАЗАТЬСЯ: верни только [[TEXT]]
Я не создаю сайты для накрутки отзывов или незаконной деятельности
[[EMOTION]]
sad
[[END]]
Без [[HTML]] и без кода.

СЦЕНЫ НАСИЛИЯ: если пользователь просит сайт, игру или анимацию со сценами насилия, крови или убийства, НЕ пытайся это сгенерировать. Сразу верни только [[TEXT]]
Я не создаю сайты со сценами насилия. Но могу сделать сайт про историческое событие (например, дуэль) без крови и жестокости
[[EMOTION]]
sad
[[END]]
Без [[HTML]] и без кода: ответ-заглушка состоит только из этого текста. Просьбы «без крови» и исторические, медицинские и образовательные темы этим правилом не запрещены.

ПРИОРИТЕТ ЗАПРОСА ПОЛЬЗОВАТЕЛЯ (ВЫШЕ ВСЕХ ПРАВИЛ СТИЛЯ НИЖЕ, ЖЁСТКОЕ ПРАВИЛО):
- Если пользователь ЯВНО просит добавить эффект (плавающие пятна, свечение кнопок, неон, градиентный текст, анимацию, эмодзи — любой визуальный эффект), ты ОБЯЗАН это сделать, даже если считаешь, что это выглядит как «нейрослоп». Твоя задача — выполнить запрос, а не спорить с пользователем. Ответ без изменений кода или с отказом от эффекта — ошибка.
- КАК ДОБАВЛЯТЬ ЭФФЕКТЫ ТОЧЕЧНО (якоря всегда есть в файле и уникальны, поэтому FIND не промахивается; это правило важнее требования «3–20 строк» для FIND):
  • новые CSS-стили: FIND = </head> → REPLACE = <style>…новые стили…</style></head>
  • плавающие пятна на фоне (div с position:fixed; inset:0; z-index:-1; pointer-events:none; overflow:hidden; внутри размытые круги с animation: float): FIND = </body> → REPLACE = <div class='blobs'>…</div></body>
  • свечение кнопок: правило с box-shadow / filter: drop-shadow в том же новом <style>, селектор по классу или тегу существующих кнопок.
- Правила блока UNSLOP ниже — это стиль ПО УМОЛЧАНИЮ, пока пользователь не попросил иного. Любая явная просьба пользователя отменяет соответствующее правило.
- Запрещено писать в [[TEXT]], что ты «не стал» делать запрошенное, и подменять запрошенный эффект «более спокойным» вариантом. Сначала выполни запрос полностью; альтернативу можно предложить одним предложением после выполнения.
- Если ты не можешь применить правку точечно (не находишь место для [[FIND]]), не отказывайся и не возвращай код без изменений: просто перегенерируй ВЕСЬ проект с нуля с этим эффектом в формате [[TEXT]]...[[EMOTION]]...[[HTML]]...[[END]].

ПРИНЦИПЫ ДИЗАЙНА:
- Характер: один яркий акцентный цвет, выразительная типографика.
- Интерактивность: быстрые hover-эффекты.
- Чистота: весь код в одном файле.
- Контент: осмысленные тексты на языке пользователя.

ВИЗУАЛ:
- Цвета и размеры через CSS-переменные (:root).
- Размеры шрифтов и отступов через clamp(); тени только нейтральные и мягкие.
- Шрифты Google Fonts: один для текста (Inter) и один для заголовков (Unbounded или Manrope).
- Иллюстрации — только inline SVG или CSS-градиенты (единственное исключение — тег <img> в режиме правок, см. ниже).
- Адаптивность mobile-first (от 320px до 1440px).

ПРАВИЛА UNSLOP (БОРЬБА С НЕЙРОСЛОПОМ — ТОЛЬКО ПО УМОЛЧАНИЮ, ЯВНАЯ ПРОСЬБА ПОЛЬЗОВАТЕЛЯ ВАЖНЕЕ):
1. Тишина в интерфейсе (по умолчанию): Никаких светящихся кнопок, неоновых границ, цветных теней и размытых пятен (блобов) на фоне. Фон должен быть спокойным (темным или светлым), а цвет — аккуратным.
2. Запрет на эмодзи (по умолчанию, отменяется явной просьбой): В интерфейсе сайта не должно быть эмодзи (🚀, ✨, 🔥). Вместо них используй строгие inline SVG-иконки (например, из набора Lucide), одного размера и цвета.
3. Никаких градиентных текстов (по умолчанию, отменяется явной просьбой): Заголовки должны быть однотонными. Запрещены градиенты на тексте.
4. Отступы по сетке: Все отступы (padding, margin, gap) должны быть кратны 4px или 8px. Никаких случайных значений вроде padding: 13px.
5. Один акцент: Выбери один яркий цвет для кнопок и ссылок. Остальное — оттенки серого или нейтральные тона. Не делай сайт разноцветным.
6. Текст по делу: Заголовки должны быть короткими и конкретными. Никаких восклицательных знаков. Никакого текста-заглушки (lorem ipsum). Только реальные факты и выгоды.
7. Шрифты: Используй один шрифт для текста и один для заголовков (например, Inter и Unbounded). Никаких дефолтных Arial.
8. Скругления: Радиусы должны быть аккуратными (12-24px), а не везде по 50px (pill).
9. Анимации (по умолчанию): Только быстрые hover-эффекты (изменение цвета или легкое всплытие). Запрещены бесконечные пульсации, вращения и летающие элементы.
10. Реальные картинки: Вместо пустых цветных квадратов используй красивые inline SVG-иллюстрации или CSS-градиенты (но не в качестве фона, а как акцентный элемент).
Главное правило: Сайт должен выглядеть так, будто его сделал профессиональный дизайнер, а не сгенерировала нейросеть за 5 секунд.

ПРАВИЛО ПОЭТАПНОЙ ГЕНЕРАЦИИ (КРИТИЧЕСКИ ВАЖНО):
1. Ты НИКОГДА не пишешь сложный проект целиком за один раз. Твой лимит — максимум 800 строк кода.
2. Если просят интернет-магазин, админ-панель или сложную игру — сделай ТОЛЬКО КРАСИВЫЙ ЛЕНДИНГ (главную страницу).
3. При НОВОЙ сборке сложного проекта в чате ([[TEXT]]) ОБЯЗАТЕЛЬНО напиши: "Я сделал основу. Из-за ограничений ИИ я не могу написать всю сложную логику за один раз. Если хочешь, напиши «Добавь корзину» или «Добавь админку», и я сделаю это следующим шагом".
4. Если не уверен, что влезешь в 800 строк — упрощай дизайн, но ОБЯЗАТЕЛЬНО закрывай все теги </html>.

РЕЖИМ ПРАВОК (УМНОЕ РЕДАКТИРОВАНИЕ):
- Если передана "ТЕКУЩАЯ ВЕРСИЯ ПРОЕКТА", ты НЕ переписываешь весь код целиком. Ты находишь нужный кусок в старом коде и меняешь его.
- Формат ответа при правке строго такой:
[[TEXT]]
1-2 предложения: что изменено.
[[EMOTION]]
happy
[[FIND]]
(точный старый кусок кода, который нужно заменить, от 3 до 20 строк)
[[REPLACE]]
(новый кусок кода, который встанет на его место)
[[END]]
- Нужно несколько правок — повтори пару [[FIND]]...[[REPLACE]]... нужное число раз и поставь один [[END]] в самом конце.
- FIND копируй из текущего кода ДОСЛОВНО (с пробелами и переносами строк) и делай его уникальным: если такая строка встречается в файле несколько раз, захвати соседние строки.
- Если нужно добавить новый блок, найди закрывающий тег предыдущего блока (например, </section>) и замени его на: </section> + новый блок.
- Если правка настолько глобальная, что требует переписывания всего (например, смена всей темы), тогда и только тогда верни полный код в формате [[TEXT]]...[[EMOTION]]...[[HTML]]...[[END]]. Так же поступай, если не получается точно найти место для правки: перегенерируй весь проект с запрошенным эффектом, но не отказывайся от него.

ЗАМЕНА ФОТО В РЕЖИМЕ ПРАВОК (только при переданной "ТЕКУЩЕЙ ВЕРСИИ ПРОЕКТА"):
- Для замены или добавления фото разрешён тег <img>, и только он (остальные запреты не меняются).
- src: либо внешняя ссылка https://..., которую пользователь сам написал в запросе, либо data:image/...;base64,... , который уже есть в текущем коде. Ссылки и base64 не выдумывай.
- Если пользователь просит заменить фото, но ссылки нет — не придумывай адрес: оставь прежний src (или поставь SVG-заглушку) и в [[TEXT]] попроси прислать ссылку на фото.
- Всегда добавляй alt и стили размера (max-width:100%; height:auto), чтобы вёрстка не ломалась.

МЕССЕНДЖЕРЫ И РЕАЛЬНОЕ ВРЕМЯ (ЖЁСТКОЕ ПРАВИЛО):
- Если пользователь просит мессенджер, чат с реальным временем или WebSockets, ты ОБЯЗАН сделать ДВА файла: main.py (сервер на FastAPI с WebSockets) и index.html (интерфейс). Правило «ТОЛЬКО ЛЕНДИНГ» здесь не действует.
- В чате ([[TEXT]]) напиши: "Мессенджер в реальном времени нельзя сделать в одном HTML-файле. Я сделал сервер (main.py) и интерфейс (index.html). Тебе нужно будет залить их на Render, чтобы общаться с другом через браузер. Вот инструкция..." — и дальше коротко, 5 пунктов: 1) создай репозиторий на GitHub и положи в него main.py и index.html в одну папку; 2) добавь файл requirements.txt с двумя строками: fastapi и uvicorn[standard]; 3) на render.com создай Web Service из этого репозитория; 4) Build Command: pip install -r requirements.txt, Start Command: uvicorn main:app --host 0.0.0.0 --port $PORT; 5) открой выданную ссылку на двух устройствах и общайся.
- Формат ответа для этого случая:
[[TEXT]]
(фраза и инструкция выше)
[[EMOTION]]
happy
[[HTML]]
<!doctype html>...полный index.html...</html>
[[FILE:main.py]]
...полный код сервера...
[[END]]
- main.py: FastAPI; GET / отдаёт index.html через FileResponse; WebSocket /ws; менеджер соединений с рассылкой всем подключённым; последние 50 сообщений хранятся в памяти и отправляются новому участнику; в сообщении есть имя отправителя.
- index.html подключается к серверу так: new WebSocket((location.protocol === "https:" ? "wss://" : "ws://") + location.host + "/ws"). Это единственное исключение из запрета на сетевые запросы. Показывай статус «подключено / нет связи» и автопереподключение.
- Каждый файл компактный, до 250 строк (это заменяет общий лимит 800 строк).

ТЕХНИКА:
- Один файл: <!doctype html>, CSS в <style>, JS в <script>.
- Никаких fetch, alert, window.open. Только <button type="button"> и якоря #id.
- Код должен быть компактным (без лишних отступов и комментариев).
- СКРЫТИЕ ПЛАШКИ NETLIFY (ЖЁСТКОЕ ПРАВИЛО): в самый конец генерируемого HTML-кода, прямо перед закрывающим </body>, ОБЯЗАТЕЛЬНО добавляй следующий CSS-код, чтобы скрыть стандартный бейдж Netlify на бесплатном тарифе: <style>.netlify-badge, #netlify-badge, a[href*='netlify.com'], iframe[src*='netlify.com'], div[class*='badge'] { display: none !important; opacity: 0 !important; pointer-events: none !important; }</style> Это правило действует при каждой полной сборке index.html (в режиме правок через [[FIND]]/[[REPLACE]] этот блок не дублируй, он уже есть в коде).
- ХРАНЕНИЕ ДАННЫХ: в приложениях, где пользователь вводит данные (трекер, список дел, заметки, счётчик, планировщик, калькулятор с историей), сохраняй их в localStorage. Любое чтение и запись — только внутри try/catch. Если хранилище недоступно (в превью оно заблокировано), молча работай с данными в обычной JS-переменной, без ошибок и алертов. Ключ с префиксом проекта, например "habits_v1". Не храни пароли и платёжные данные. Для сайтов, лендингов и визиток localStorage не нужен.

ФОРМАТ ОТВЕТА ПРИ НОВОЙ СБОРКЕ:
[[TEXT]]
1-3 предложения.
[[EMOTION]]
happy
[[HTML]]
<!doctype html>...полный файл...</html>
[[END]]
"""

BUILD_PARSE_RE = re.compile(r"\[\[TEXT\]\](.*?)\[\[EMOTION\]\](.*?)\[\[HTML\]\](.*?)(?:\[\[END\]\]|$)", re.S)

def parse_build(raw):
    m = BUILD_PARSE_RE.search(raw or "")
    if not m: return None
    text = m.group(1).strip()
    emotion = m.group(2).strip().split()[0] if m.group(2).strip() else "happy"
    html_part = m.group(3)
    extra = re.split(r"\[\[FILE:([^\]\n]{1,40})\]\]", html_part)
    html = extra[0].strip()
    for fname, fcode in zip(extra[1::2], extra[2::2]):
        fname = fname.strip(); fcode = re.sub(r"^```[a-zA-Z]*\s*", "", fcode.strip()); fcode = re.sub(r"\s*```$", "", fcode)
        if fcode:
            lang = "python" if fname.endswith(".py") else ""
            text += f"\n\n{fname}:\n```{lang}\n{fcode}\n```"
    html = re.sub(r"^```[a-zA-Z]*\s*", "", html); html = re.sub(r"\s*```$", "", html)
    
    if "</html>" not in html.lower():
        if "</body>" in html.lower() or "<script" in html.lower():
            html = html + "\n</html>"
        else:
            log.warning("Build generated but cut off (no </html>)")
            return None
    return text, emotion, html

def refund_units(conn, uid, units):
    try:
        if units > 0:
            conn.execute("UPDATE pro_state SET requests_left = requests_left + ? WHERE user_id = ?", (units, uid))
            conn.commit()
    except Exception:
        log.exception("refund failed")

def refund_ask(conn, uid, weight, is_pro):
    """Возврат списанного за /ask, если ответ не получился."""
    try:
        if weight <= 0: return
        if is_pro == "bonus":
            conn.execute("UPDATE rate_state SET ref_bonus = ref_bonus + ? WHERE user_id = ?", (weight, uid))
        elif is_pro:
            conn.execute("UPDATE pro_state SET requests_left = requests_left + ? WHERE user_id = ?", (weight, uid))
        else:
            conn.execute("UPDATE rate_state SET ask_count = MAX(ask_count - ?, 0) WHERE user_id = ?", (weight, uid))
        conn.commit()
    except Exception:
        log.exception("ask refund failed")

def refund_billed(conn, uid, units, trial=False):
    """Возврат за сбой, когда модель УЖЕ потратила токены (владелец за них платит).
    Не более REFUND_BILLED_MAX возвратов в час. True — вернули, False — лимит возвратов исчерпан.
    Ошибки самого API (429/402/таймаут) сюда не идут: они не тарифицируются и возвращаются всегда."""
    if units <= 0 and not trial: return True
    try:
        now = int(time.time())
        conn.execute("DELETE FROM refund_log WHERE user_id = ? AND ts <= ?", (uid, now - REFUND_BILLED_WINDOW))
        n = scalar(conn, "SELECT COUNT(*) FROM refund_log WHERE user_id = ?", (uid,))
        if n >= REFUND_BILLED_MAX:
            conn.commit()
            log.warning("refund denied uid=%s units=%s (budget exhausted)", uid, units)
            return False
        conn.execute("INSERT INTO refund_log (user_id, ts) VALUES (?, ?)", (uid, now))
        conn.commit()
    except Exception:
        log.exception("refund budget check failed")
    if units > 0: refund_units(conn, uid, units)
    return True

def trial_available(conn, uid):
    return scalar(conn, "SELECT COUNT(*) FROM trial_build WHERE user_id = ?", (uid,)) == 0

def extract_json(raw):
    t = (raw or "").strip()
    t = re.sub(r"^```[a-zA-Z]*\s*", "", t); t = re.sub(r"\s*```$", "", t)
    a, b = t.find("{"), t.rfind("}")
    if a == -1 or b <= a: raise ValueError("no json")
    obj = json.loads(t[a:b + 1])
    if not isinstance(obj, dict): raise ValueError("not dict")
    return obj

# ---------- ОШИБКИ API: нет денег / лимит / перегрузка ----------
API_LOAD_TEXT = "Сейчас огромная нагрузка на сервера. Попробуй чуть позже."
API_LOAD_TEXT_BUILD = "Сейчас огромная нагрузка на сервера. Я вернул тебе сборку, попробуй чуть позже."
_API_FUNDS_MARKERS = ("insufficient", "balance", "payment required", "billing", "credit", "quota", "недостаточно средств", "пополните", "баланс")
_API_LOAD_MARKERS = ("rate limit", "rate_limit", "overloaded", "capacity", "too many requests")
_api_alert_ts = 0.0

def api_error_kind(e):
    """'funds' — на API закончились деньги, 'load' — лимит/перегрузка, None — любая другая ошибка."""
    code = getattr(e, "status_code", None)
    t = str(e).lower()
    if code == 402 or any(m in t for m in _API_FUNDS_MARKERS): return "funds"
    if code in (429, 502, 503, 529) or any(m in t for m in _API_LOAD_MARKERS): return "load"
    return None

def alert_api_funds(e):
    """Один раз в 30 минут пишет админу в Telegram, что пора пополнить баланс API (пользователю показываем «нагрузку»)."""
    global _api_alert_ts
    now = time.time()
    if now - _api_alert_ts < 1800: return
    _api_alert_ts = now
    log.error("API: закончились средства или лимит: %s", str(e)[:300])
    try:
        asyncio.get_running_loop().create_task(tg_call("sendMessage", {"chat_id": ADMIN_ID,
            "text": "⚠️ Похоже, на балансе API закончились деньги (или ключ заблокирован). Пользователям показываю «огромная нагрузка». Пополни баланс.\n\n" + str(e)[:300]}))
    except Exception:
        log.warning("alert_api_funds: не удалось отправить уведомление", exc_info=True)

async def llm(messages, max_tokens, json_mode=False, temperature=None):
    base = dict(model=AI_MODEL, messages=messages, max_tokens=max_tokens, temperature=AI_TEMPERATURE if temperature is None else temperature)
    if json_mode: base["response_format"] = {"type": "json_object"}
    try:
        return await client.chat.completions.create(**base)
    except Exception as e:
        if not json_mode or api_error_kind(e): raise
        log.warning("json_object mode failed, retrying without response_format", exc_info=True)
        base.pop("response_format", None)
        return await client.chat.completions.create(**base)

async def chat_json(messages, max_tokens):
    return await llm(messages, max_tokens, json_mode=True)

def salvage_json(raw):
    t = raw or ""
    m = re.search(r'"text"\s*:\s*"((?:[^"\\]|\\.)*)', t, re.S)
    if not m: return None
    chunk = m.group(1)
    try: txt = json.loads('"' + chunk + '"')
    except ValueError: txt = chunk.replace("\\n", "\n").replace('\\"', '"').rstrip("\\")
    txt = txt.strip()
    if not txt: return None
    em = re.search(r'"emotion"\s*:\s*"(\w+)"', t)
    return {"text": txt, "emotion": em.group(1) if em else "happy"}

PLAIN_EMO_RE = re.compile(r"^\W*(happy|sad|surprised|angry|thinking|sleepy)\b\W*?(?:\n+|[:\-—]\s*)(.*)$", re.S | re.I)

def parse_plain(raw):
    t = (raw or "").strip()
    if ANSWER_MARK in t: 
        t = t.rsplit(ANSWER_MARK, 1)[1].strip()
    if not t: return "", "happy"
    if re.match(r"^\s*(```(?:json)?)?\s*\{", t):
        try:
            o = extract_json(t)
            return str(o.get("text") or "").strip(), (o.get("emotion") if o.get("emotion") in EMOTIONS_OK else "happy")
        except ValueError:
            sv = salvage_json(t)
            if sv: return sv["text"], (sv["emotion"] if sv["emotion"] in EMOTIONS_OK else "happy")
    if t.lower() in EMOTIONS_OK: return "", t.lower()
    m = PLAIN_EMO_RE.match(t)
    if m: return m.group(2).strip(), m.group(1).lower()
    return t, "happy"

# --- ИНТЕНТ БЕЗ ГЛАГОЛА: «Сайт для ресторана», «Лендинг для психолога» ---
# существительное-цель в начале фразы + нет вопроса/жалобы = команда на сборку
GOAL_STRONG_RE = re.compile(r"сайт|лендинг|landing|веб-?страниц|приложени|трекер|планировщик|органайзер|интернет-?магазин|визитк|портфолио|калькулятор|дашборд|мессенджер", re.I)
# бизнес-темы сами по себе («Ресторан», «Магазин одежды») неоднозначны: по умолчанию уточняем кнопками, NOUN_LED_WEAK=build — собирать сразу
GOAL_WEAK_RE = re.compile(r"магазин|ресторан|кафе\b|кофейн|пекарн|барбершоп|салон красоты|клиник|стоматолог|отел[ьяю]\b|фитнес|агентств|каталог|фотограф|психолог", re.I)
NOUN_LED_WEAK = os.getenv("NOUN_LED_WEAK", "ask")
BUILD_QUESTION_RE = re.compile(r"\?|\b(?:как|что|чем|почему|зачем|где|когда|сколько|какой|какая|какие|какое|кто|ли)\b", re.I)
BUILD_PROBLEM_RE = re.compile(r"не (?:работает|открыва|грузит|запуска|отображ|получа|показыва)|ошибк|\bбаг|слом|глюч|помоги|подскаж|стоит ли|лучше|закрыт|открыт\b", re.I)

def noun_led_intent(user_text):
    t = (user_text or "").strip()
    words = t.split()
    if not words or len(t) > 160 or len(words) > 14: return None
    if BUILD_NEG_RE.search(t) or BUILD_QUESTION_RE.search(t) or BUILD_PROBLEM_RE.search(t): return None
    head = " ".join(words[:3])  # существительное-цель должно стоять в начале фразы
    if GOAL_STRONG_RE.search(head): return "build"
    if GOAL_WEAK_RE.search(head): return NOUN_LED_WEAK if NOUN_LED_WEAK in ("ask", "build") else "ask"
    return None

def build_intent(user_text):
    """'build' — явная сборка (действие + существительное ИЛИ существительное-цель в начале: «Сайт для кафе»);
    'ask' — действие + прилагательное без существительного (неясно: собрать или совет); None — обычный чат."""
    if BUILD_NEG_RE.search(user_text): return None
    if not BUILD_ACTION_RE.search(user_text): return noun_led_intent(user_text)
    if BUILD_THING_RE.search(user_text): return "build"
    if BUILD_ADJ_RE.search(user_text): return "ask"
    return None

# --- ГОЛОЕ НАЗВАНИЕ как продолжение сборки: «Шопи фай» после вопроса бота про проект ---
_NOT_NAME = {"да", "нет", "ок", "окей", "ага", "угу", "привет", "пока", "спасибо", "хорошо", "ладно", "отмена", "стоп",
             "не надо", "не знаю", "любое", "любой", "на твой выбор", "нормально", "понятно", "ясно", "супер", "круто"}

def is_bare_name(text):
    t = (text or "").strip()
    if not t or len(t) > 40 or len(t.split()) > 4: return False
    if t.lower().strip(" .!") in _NOT_NAME: return False
    if BUILD_QUESTION_RE.search(t) or BUILD_PROBLEM_RE.search(t) or BUILD_NEG_RE.search(t): return False
    if not re.search(r"[A-Za-zА-Яа-яЁё0-9]", t): return False
    return True

def pending_build_request(history_text, current_text):
    """Если бот только что задал вопрос по запросу на сборку — возвращает этот запрос пользователя, иначе None."""
    lines = [l for l in (history_text or "").split("\n") if l.startswith(("Пользователь:", "Ты:"))]
    if lines and lines[-1].startswith("Пользователь:") and lines[-1][len("Пользователь:"):].strip().lower() == (current_text or "").strip().lower():
        lines = lines[:-1]  # текущее сообщение уже лежит в конце истории
    if len(lines) < 2 or not lines[-1].startswith("Ты:"): return None
    if "?" not in lines[-1][-200:]: return None  # бот ничего не спрашивал — продолжать нечего
    for l in reversed(lines[:-1]):
        if l.startswith("Пользователь:"):
            prev = l[len("Пользователь:"):].strip()
            return prev if build_intent(prev.lower()) == "build" else None
    return None

def wants_build(user_text):
    # только НОВАЯ сборка. Правка включается явно: кнопка «Править проект» -> edit_project=true в /ask
    # «ask» (глагол + прилагательное) тоже считается сборкой, но /ask сначала задаёт уточняющий вопрос с кнопками
    return build_intent(user_text) in ("build", "ask")

# --- антиспам и запрещённые темы для сборки ---
BUILD_COOLDOWN = 30  # секунд между запросами сборки (включая правки и отказы)
BUILD_COOLDOWN_TEXT = "Подожди немного, я обрабатываю предыдущий запрос"
BUILD_REFUSAL_TEXT = "Я не создаю сайты для накрутки отзывов или незаконной деятельности"
BUILD_VIOLENCE_REFUSAL_TEXT = "Я не создаю сайты со сценами насилия. Но могу сделать сайт про историческое событие (например, дуэль) без крови и жестокости"
EDIT_PRO_ONLY_TEXT = "Правки доступны только на Pro-тарифе."
EDIT_ATTACH_TEXT = "В режиме правки нельзя прикреплять фото. Напиши, что нужно изменить, текстом"

# <img> в правках: только https-ссылка или data:image base64, всё остальное (http, javascript:, относительные пути) вырезаем
_IMG_TAG_RE = re.compile(r"<img\b[^>]*>", re.I)
_IMG_SRC_RE = re.compile(r"""\bsrc\s*=\s*(?:"([^"]*)"|'([^']*)')""", re.I)
_IMG_OK_RE = re.compile(r"^(?:https://[^\s]+|data:image/(?:png|jpe?g|gif|webp);base64,[A-Za-z0-9+/=\s]+)$", re.I)

def clean_img_tags(html):
    def fix(m):
        sm = _IMG_SRC_RE.search(m.group(0))
        src = ((sm.group(1) if sm.group(1) is not None else sm.group(2)) if sm else "").strip()
        return m.group(0) if _IMG_OK_RE.match(src) else ""
    return _IMG_TAG_RE.sub(fix, html)

def content_to_text(user_content):
    """user_content бывает списком [text, image_url]: в текстовый промпт отдаём только текст, base64 туда попадать не должен"""
    if isinstance(user_content, list):
        return "\n".join(p.get("text", "") for p in user_content if isinstance(p, dict) and p.get("type") == "text")
    return str(user_content or "")
BUILD_FORBIDDEN_RE = re.compile(
    r"накрутк\w*|накрут\w*\s+(?:отзыв|лайк|подписчик|просмотр|голос|рейтинг)\w*"
    r"|(?:фейков|поддельн|липов|заказн|купленн)\w*\s+отзыв\w*|(?:куп\w*|заказ\w*|продаж\w*)\s+отзыв\w*"
    r"|\bскам\w*|фишинг\w*|мошенн\w*|развод\w*\s+(?:люд|клиент|покупател)\w*"
    r"|обман\w*\s+(?:люд|клиент|покупател|пользовател|стариков|вкладчик)\w*"
    r"|финансов\w*\s+пирамид\w*|ponzi|(?:поддельн|фальшив)\w*\s+(?:документ|паспорт|справк|диплом|чек|деньги|купюр)\w*"
    r"|нелегальн\w*|незаконн\w*|(?:украсть|кража|краж\w*)\s+(?:данн|парол|карт)\w*|кардинг\w*|дроппер\w*|обнал\w*", re.I)
# защитный контекст («сайт про защиту от мошенников») — не блокируем
BUILD_SAFE_CTX_RE = re.compile(
    r"защит\w*|против\s+(?:мошенн|скам|фишинг|обман)|борьб\w*|распозна\w*|как\s+не\s+(?:попасть|стать)|анти-?(?:скам|фишинг|мошенн)|предостереж\w*|осторожн\w*", re.I)
def is_forbidden_build(user_text):
    return bool(BUILD_FORBIDDEN_RE.search(user_text) and not BUILD_SAFE_CTX_RE.search(user_text))

# сцены насилия / крови / убийства: отказ ДО списаний и до вызова модели (ноль токенов).
# Регулярка узкая: ловит просьбу ПОКАЗАТЬ насилие, а не слово «кровь» (донорство, анализ крови) и не историю.
BUILD_VIOLENCE_RE = re.compile(
    r"сцен\w*\s+(?:\S+\s+){0,2}(?:насили|убийств|расправ|пытк|пыток|жестокост|казн|резн|бойн)\w*"
    r"|(?:кровав|жесток)\w*\s+(?:сцен|бойн|расправ|убийств|резн|казн|пытк)\w*"
    r"|\bgore\b|расчленен\w*|потрошен\w*|снафф\w*"
    r"|(?:с\s+кровью|с\s+брызгами\s+кров\w*|кровь\s+(?:лета|брызг|течёт|течет|льёт|льет)\w*)"
    r"|(?:показ|изобраз|анимир|нарису|нарисов|реализ|добав|сдела|созда|сгенер)\w*\s+(?:\S+\s+){0,4}(?:убийств|кровь|крови|кровот|расправ|казн|пытк|пыток|резн|бойн)\w*", re.I)
# контекст, при котором не блокируем (дальше сработает правило в BUILD_SYSTEM, если запрос всё же про насилие)
BUILD_VIOLENCE_SAFE_RE = re.compile(
    r"без\s+(?:крови|насили|жестокост|убийств)\w*|против\s+насили\w*|защит\w*|профилактик\w*"
    r"|донор\w*|анализ\w*\s+кров\w*|групп\w*\s+кров\w*|давлен\w*|медицин\w*|клиник\w*|больниц\w*"
    r"|истори\w*|музе\w*|документальн\w*|расследован\w*", re.I)
def is_violent_build(user_text):
    return bool(BUILD_VIOLENCE_RE.search(user_text) and not BUILD_VIOLENCE_SAFE_RE.search(user_text))

def refusal_in(raw):
    """Какой из наших отказов модель вернула в [[TEXT]] (или None)."""
    r = raw or ""
    if BUILD_VIOLENCE_REFUSAL_TEXT in r: return BUILD_VIOLENCE_REFUSAL_TEXT
    if BUILD_REFUSAL_TEXT in r: return BUILD_REFUSAL_TEXT
    return None

# обычный чат: блокируем только ПРОСЬБУ совершить (накрутить, обмануть, сделать фишинг), а не вопросы про мошенников
CHAT_REFUSAL_TEXT = "Я не помогаю с накруткой отзывов, обманом людей и незаконной деятельностью"
_CHAT_DO = r"(?:сдела\w*|созда\w*|напиш\w*|запил\w*|разработ\w*|помоги\w*|помоч\w*|как|научи\w*|подскаж\w*)"
CHAT_FORBIDDEN_RE = re.compile(
    r"(?:накрути\w*|накручив\w*|обману\w*|обманыв\w*|развест\w*|разводит\w*|кинуть)\s+(?:\S+\s+){0,2}"
    r"(?:отзыв|лайк|подписчик|просмотр|голос|рейтинг|люд|клиент|покупател|пенсионер|старик|пожил|вкладчик|доверчив)\w*"
    r"|\b" + _CHAT_DO + r"\s+(?:\S+\s+){0,4}(?:накрутк|фишинг|скам\b|финансов\w*\s+пирамид|фейков\w*\s+отзыв"
    r"|поддельн\w*\s+(?:документ|паспорт|справк|диплом)|фальшив\w*\s+(?:документ|деньги)|кардинг|обнал|дроппер)\w*", re.I)
CHAT_SAFE_RE = re.compile(
    r"защит\w*|распозна\w*|отлич\w*|борьб\w*|что\s+такое|объясни|расскажи|почему|что\s+делать|обманули|мошенники\s+(?:позвонили|написали)|пострада\w*|жертв\w*|как\s+не\s+(?:попасть|стать)|предостереж\w*", re.I)
def is_forbidden_chat(user_text):
    return bool(CHAT_FORBIDDEN_RE.search(user_text) and not CHAT_SAFE_RE.search(user_text))

BUILD_CHOICE_QUESTION = "Тебе собрать проект (сайт или приложение) или просто совет?"
BUILD_CHOICES = [{"id": "build", "label": "Собрать проект"}, {"id": "advice", "label": "Просто совет"}]

BUILD_FAIL = {"text": "Не получилось собрать проект. Попробуй написать задачу чуть проще или покороче.", "emotion": "sad"}
BUILD_TRIAL_NOTE = " Это была бесплатная пробная сборка — дальше студия доступна на тарифах."

def build_messages(user_content, history_text, current_code, needs_backend=False):
    task = content_to_text(user_content)
    messages = [{"role": "system", "content": BUILD_SYSTEM + (BACKEND_SYSTEM_ADDON if needs_backend and not current_code else "")}]
    # правка: в модель идут только текущий код и последний запрос, история чата не нужна (экономия токенов)
    if history_text and not current_code: messages.append({"role": "user", "content": f"Контекст: предыдущие сообщения диалога:\n{history_text[-4000:]}"})
    if current_code:
        messages.append({"role": "user", "content": "ТЕКУЩАЯ ВЕРСИЯ ПРОЕКТА (HTML):\n" + current_code})
    else:
        messages.append({"role": "user", "content": "Проект создается с нуля."})
    if current_code:
        messages.append({"role": "user", "content": f"ЗАДАЧА (ПРАВКА): {task}\n\n"
            "РЕЖИМ ПРАВКИ: НЕ переписывай файл целиком. Верни [[TEXT]]..[[EMOTION]]..[[FIND]]..[[REPLACE]]..[[END]]: "
            "FIND — дословный уникальный фрагмент из текущего кода, REPLACE — его новая версия. "
            "Явно запрошенный эффект (пятна, свечение, анимацию) делай обязательно: не отказывайся и не подменяй его. "
            + ("Полный [[HTML]] — только если правка затрагивает почти весь файл или не получается точно найти место для правки." if len(current_code) <= BIG_CODE_NO_REWRITE
               else "Полный [[HTML]] ЗАПРЕЩЁН: файл большой. Отвечай только парами FIND/REPLACE.")})
    else:
        messages.append({"role": "user", "content": f"ЗАДАЧА: {task}"})
    return messages

def build_continue_messages(messages, raw):
    tail = raw[-500:]
    return messages + [
        {"role": "assistant", "content": raw},
        {"role": "user", "content": f"Ты оборвался на полуслове: ...{tail}\nПродолжи генерацию ровно с этого места и закончи код тегом </html>. Не пиши ничего лишнего, только продолжение кода."}
    ]

def build_error_result(conn, uid, weight, e, current_code=None):
    conn = renew_conn(conn)
    refund_units(conn, uid, weight)
    err_text = str(e).lower()
    pro_now = pro_safe(conn, uid)
    kind = api_error_kind(e)
    if kind:
        if kind == "funds": alert_api_funds(e)
        return {"text": API_LOAD_TEXT_BUILD, "emotion": "sleepy", "pro": pro_now}
    if current_code:  # правка: понятное сообщение вместо общей отписки
        return {"text": "Не успел применить правку: сервер слишком долго отвечал. Я вернул списанное — попробуй ещё раз или опиши эффект покороче.", "emotion": "sad", "pro": pro_now}
    return {**BUILD_FAIL, "pro": pro_now}

# ---------- УМНОЕ РЕДАКТИРОВАНИЕ (DIFF) ----------
DIFF_HEAD_RE = re.compile(r"\[\[TEXT\]\](.*?)(?:\[\[EMOTION\]\](.*?))?\[\[FIND\]\]", re.S)
DIFF_PAIR_RE = re.compile(r"\[\[FIND\]\](.*?)\[\[REPLACE\]\](.*?)(?=\[\[FIND\]\]|\[\[END\]\]|\Z)", re.S)
DIFF_FAIL_TEXT = {
    "not_found": "Не смог найти в коде место для правки. Я вернул списанное — опиши чуть точнее, что и где поменять.",
    "ambiguous": "Нашёл несколько подходящих мест и не стал гадать. Я вернул списанное — уточни, какую именно часть менять.",
    "cut": "Правка оборвалась на полуслове. Я вернул списанное — попробуй ещё раз.",
}
DIFF_FAIL_TEXT["empty"] = DIFF_FAIL_TEXT["not_found"]

def is_diff_raw(raw):
    """Ответ модели — правка ([[FIND]] раньше [[HTML]]), а не полный файл."""
    r = raw or ""
    i = r.find("[[FIND]]")
    if i < 0: return False
    j = r.find("[[HTML]]")
    return j < 0 or i < j

def apply_diff(old_html, find_text, replace_text):
    """Заменяет find_text на replace_text. Возвращает (новый_html, None) или (None, код_ошибки):
    'empty' — пустой FIND, 'not_found' — не нашли, 'ambiguous' — нашли больше одного места."""
    if not find_text or not find_text.strip(): return None, "empty"
    n = old_html.count(find_text)
    if n == 1:
        i = old_html.index(find_text)
        return old_html[:i] + replace_text + old_html[i + len(find_text):], None
    if n > 1: return None, "ambiguous"
    # модель часто путает отступы/переносы: ищем тот же текст, но с любыми пробельными промежутками
    pat = r"\s+".join(re.escape(w) for w in find_text.split())
    ms = list(re.finditer(pat, old_html))
    if len(ms) == 1:
        m = ms[0]
        return old_html[:m.start()] + replace_text + old_html[m.end():], None
    return None, ("ambiguous" if ms else "not_found")

def _strip_fence(b):
    b = re.sub(r"^\s*```[a-zA-Z]*[ \t]*\n", "", b)
    b = re.sub(r"\n[ \t]*```\s*$", "", b)
    return b.strip("\r\n")

def parse_diff_build(raw, current_code):
    """-> (text, emotion, html, err). Все правки применяются по очереди, всё или ничего."""
    raw = raw or ""
    tail = raw[raw.find("[[FIND]]"):]
    m = DIFF_HEAD_RE.search(raw)
    text = m.group(1).strip() if m else ""
    emo_src = (m.group(2) or "").strip() if m else ""
    emotion = emo_src.split()[0] if emo_src else "happy"
    if "[[END]]" not in tail: return text, emotion, None, "cut"
    pairs = DIFF_PAIR_RE.findall(tail)
    if not pairs: return text, emotion, None, "empty"
    html = current_code
    for f, r in pairs:
        html, err = apply_diff(html, _strip_fence(f), _strip_fence(r))
        if err: return text, emotion, None, err
    return text, emotion, html, None

EDIT_FAIL_TEXT = "Не смог применить правку. Я вернул списанное — опиши чуть конкретнее, что и где поменять."

def edit_needs_rewrite(raw, current_code):
    """Правка не применилась: diff не нашёл место / оборвался, либо модель ответила текстом без кода (отказалась от эффекта)."""
    if not current_code or refusal_in(raw): return False
    if is_diff_raw(raw): return parse_diff_build(raw, current_code)[3] is not None
    return parse_build(raw) is None

def rewrite_allowed(conn, uid, current_code):
    """Можно ли пересобрать проект целиком: небольшой проект и не больше REWRITE_MAX_PER_HOUR раз в час."""
    if not current_code or len(current_code) > REWRITE_MAX_CODE_LEN: return False
    conn = renew_conn(conn)
    try:
        now = int(time.time())
        conn.execute("DELETE FROM rewrite_log WHERE user_id = ? AND ts <= ?", (uid, now - 3600))
        if scalar(conn, "SELECT COUNT(*) FROM rewrite_log WHERE user_id = ?", (uid,)) >= REWRITE_MAX_PER_HOUR:
            conn.commit(); return False
        conn.execute("INSERT INTO rewrite_log (user_id, ts) VALUES (?, ?)", (uid, now)); conn.commit()
        return True
    except Exception:
        log.exception("rewrite budget check failed")
        return False

def rewrite_messages(user_content, current_code):
    task = content_to_text(user_content)
    return [{"role": "system", "content": BUILD_SYSTEM},
            {"role": "user", "content": "ТЕКУЩАЯ ВЕРСИЯ ПРОЕКТА (HTML):\n" + current_code},
            {"role": "user", "content": f"ЗАДАЧА (ПРАВКА): {task}\n\n"
                "Точечная правка не применилась. ПЕРЕГЕНЕРИРУЙ ВЕСЬ ПРОЕКТ ЦЕЛИКОМ с этим изменением, сохранив остальной дизайн и тексты. "
                "Формат строго [[TEXT]]..[[EMOTION]]..[[HTML]]..[[END]], без [[FIND]]/[[REPLACE]]. "
                "Запрошенный эффект обязателен: не отказывайся от него и не подменяй его другим."}]

def finalize_build(conn, uid, raw, weight, current_code=None, watermark=False):
    conn = renew_conn(conn)
    if current_code and is_diff_raw(raw):
        text, emotion, html, err = parse_diff_build(raw, current_code)
        if err:
            refunded = refund_billed(conn, uid, weight)
            log.error(f"Diff failed ({err}), refunded={refunded}. Raw: {(raw or '')[:300]}")
            txt = DIFF_FAIL_TEXT.get(err, DIFF_FAIL_TEXT["not_found"]) if refunded else NOREFUND_TEXT
            return {"text": txt, "emotion": "sad", "pro": pro_safe(conn, uid)}
        if html: html = clean_img_tags(html)
        parsed = (text, emotion, html)
    else:
        parsed = parse_build(raw)
        if parsed and current_code: parsed = (parsed[0], parsed[1], clean_img_tags(parsed[2]))
    visual = None
    if parsed:
        text, emotion, html = parsed
        visual = clean_visual({"type": "html", "code": html}, (MAX_EDIT_CODE_LEN + 20_000) if current_code else MAX_BUILD_LEN)
        if visual and watermark: visual = lock_visual(conn, uid, visual)  # бесплатный пользователь: код остаётся на сервере, наружу уходит только pid
    if not parsed or not visual:
        if BUILD_VIOLENCE_REFUSAL_TEXT in (raw or ""):  # осознанный отказ, а не сбой: возвращаем всегда, не тратя бюджет возвратов
            refund_units(conn, uid, weight)
            return {"text": BUILD_VIOLENCE_REFUSAL_TEXT, "emotion": "sad", "pro": pro_safe(conn, uid)}
        refunded = refund_billed(conn, uid, weight)
        if not refunded:
            return {"text": NOREFUND_TEXT, "emotion": "sad", "pro": pro_safe(conn, uid)}
        if BUILD_REFUSAL_TEXT in (raw or ""):  # модель сама отказалась (запрещённая тема)
            return {"text": BUILD_REFUSAL_TEXT, "emotion": "sad", "pro": pro_safe(conn, uid)}
        log.error(f"Build failed to parse. Raw: {(raw or '')[:200]}")
        if current_code:  # правка: ясное сообщение вместо общей отписки
            return {"text": EDIT_FAIL_TEXT, "emotion": "sad", "pro": pro_safe(conn, uid)}
        return {**BUILD_FAIL, "pro": pro_safe(conn, uid)}
    if emotion not in EMOTIONS_OK: emotion = "happy"
    return {"text": text or "Готово! Смотри превью.", "emotion": emotion, "visual": visual, "pro": pro_safe(conn, uid)}

async def run_build(conn, uid, user_content, history_text, current_code, weight, watermark=False, model=None, needs_backend=False):
    release_txn(conn)  # на время генерации в базе не висит ни одна транзакция
    model = model or get_model_for_user(uid, conn)
    messages = build_messages(user_content, history_text, current_code, needs_backend)
    try:
        resp = await client.with_options(timeout=BUILD_TIMEOUT).chat.completions.create(
            model=model,
            messages=messages,
            max_tokens=EDIT_MAX_TOKENS if current_code else BUILD_MAX_TOKENS,
            temperature=EDIT_TEMPERATURE if current_code else 0.7
        )
        raw = resp.choices[0].message.content or ""
        # в правке авто-докрутку не делаем: оборванный полный файл = сбой, а не второй платный запрос
        if "</html>" not in raw.lower() and len(raw) > 100 and not is_diff_raw(raw) and not current_code:
            log.warning("Код оборвался, запускаю авто-докрутку...")
            resp2 = await client.with_options(timeout=BUILD_TIMEOUT).chat.completions.create(
                model=model,
                messages=build_continue_messages(messages, raw),
                max_tokens=BUILD_MAX_TOKENS,
                temperature=0.7
            )
            raw = raw + (resp2.choices[0].message.content or "")
        if current_code and edit_needs_rewrite(raw, current_code) and rewrite_allowed(conn, uid, current_code):
            log.warning("Правка не применилась — пересобираю проект целиком")
            first_raw = raw
            try:
                rw = rewrite_messages(user_content, current_code)
                resp3 = await client.with_options(timeout=BUILD_TIMEOUT).chat.completions.create(
                    model=model, messages=rw, max_tokens=BUILD_MAX_TOKENS, temperature=0.4)
                raw = resp3.choices[0].message.content or ""
                if "</html>" not in raw.lower() and len(raw) > 100:
                    resp4 = await client.with_options(timeout=BUILD_TIMEOUT).chat.completions.create(
                        model=model, messages=build_continue_messages(rw, raw), max_tokens=BUILD_MAX_TOKENS, temperature=0.4)
                    raw = raw + (resp4.choices[0].message.content or "")
            except Exception:
                log.exception("rewrite failed")
                raw = first_raw  # финализируем исходный ответ: вернётся понятная ошибка и возврат
    except Exception as e:
        log.exception("build request failed")
        return build_error_result(conn, uid, weight, e, current_code)
    return finalize_build(conn, uid, raw, weight, current_code, watermark)

# ---------- СТРИМИНГ СБОРКИ ----------
_TRAIL = "`\n\r\t "

class BuildStreamParser:
    """Режет поток [[TEXT]]..[[EMOTION]]..[[HTML]]..[[FILE:..]]..[[END]] на события ("text", ..) и ("html", ..).
    В html попадает только чистый код: без всего до [[HTML]], без ```-оград и без всего после [[END]] / [[FILE:."""
    def __init__(self, patch=False):
        self.buf = ""
        self.state = "pre"   # pre -> text -> emotion -> head -> html   |   text/emotion -> patch (режим правки [[FIND]])
        self.ended = False
        self.patch = patch
        self.m_text = ("[[EMOTION]]", "[[HTML]]") + (("[[FIND]]",) if patch else ())

    @staticmethod
    def _hold(buf, markers):
        """Сколько символов с конца нельзя отдавать: вдруг это начало маркера, разорванного между кусками."""
        hold = 0
        for m in markers:
            for k in range(min(len(m) - 1, len(buf)), 0, -1):
                if buf.endswith(m[:k]):
                    hold = max(hold, k); break
        return hold

    def feed(self, d):
        out = []
        if self.ended: return out
        self.buf += d
        while self.buf and not self.ended:
            if self.state == "patch":   # правку не показываем кусками: ответ целиком копится в st["raw"]
                self.buf = ""; break
            if self.state == "pre":
                i = self.buf.find("[[TEXT]]")
                if i < 0:
                    self.buf = self.buf[-8:]
                    break
                self.buf = self.buf[i + 8:]; self.state = "text"; continue
            if self.state == "text":
                found = [(self.buf.find(m), m) for m in self.m_text]
                found = [(i, m) for i, m in found if i >= 0]
                if not found:
                    safe = len(self.buf) - self._hold(self.buf, self.m_text)
                    if safe > 0:
                        out.append(("text", self.buf[:safe])); self.buf = self.buf[safe:]
                    break
                i, m = min(found)
                if i: out.append(("text", self.buf[:i]))
                self.buf = self.buf[i + len(m):]
                if m == "[[FIND]]":
                    self.state = "patch"; self.buf = ""; out.append(("patch", "")); break
                self.state = "emotion" if m == "[[EMOTION]]" else "head"
                continue
            if self.state == "emotion":
                found = [(self.buf.find(m), m) for m in ("[[HTML]]",) + (("[[FIND]]",) if self.patch else ())]
                found = [(i, m) for i, m in found if i >= 0]
                if not found: break
                i, m = min(found)
                if m == "[[FIND]]":
                    self.state = "patch"; self.buf = ""; out.append(("patch", "")); break
                self.buf = self.buf[i + 8:]; self.state = "head"; continue
            if self.state == "head":
                t = self.buf.lstrip()
                if t.startswith("```"):
                    nl = t.find("\n")
                    if nl < 0: break
                    t = t[nl + 1:]
                elif len(t) < 3 and "```".startswith(t):
                    break
                self.buf = t; self.state = "html"; continue
            if self.state == "html":
                cut = None
                for m in ("[[FILE:", "[[END]]"):
                    i = self.buf.find(m)
                    if i >= 0 and (cut is None or i < cut): cut = i
                body = self.buf if cut is None else self.buf[:cut]
                finished = cut is not None
                k = body.lower().find("</html>")
                if k >= 0:
                    body = body[:k + 7]; finished = True
                if finished:
                    emit = body.rstrip(_TRAIL)
                    if emit: out.append(("html", emit))
                    self.buf = ""; self.ended = True
                    break
                hold = self._hold(body, ("[[FILE:", "[[END]]"))
                safe = body[:len(body) - hold] if hold else body
                emit = safe.rstrip(_TRAIL)   # хвост из ``` и пробелов не отдаём: это может быть ограда перед [[END]]
                if emit:
                    out.append(("html", emit)); self.buf = self.buf[len(emit):]
                break
        return out

    def finish(self):
        """Поток закончился: отдаём то, что осталось (без обрывка маркера и хвостовых ```)."""
        out = []
        if self.ended: return out
        if self.state == "text" and self.buf:
            b = self.buf[:len(self.buf) - self._hold(self.buf, self.m_text)]
            if b: out.append(("text", b))
        elif self.state == "html" and self.buf:
            cut = None
            for m in ("[[FILE:", "[[END]]"):
                i = self.buf.find(m)
                if i >= 0 and (cut is None or i < cut): cut = i
            body = self.buf if cut is None else self.buf[:cut]
            k = body.lower().find("</html>")
            if k >= 0: body = body[:k + 7]
            elif cut is None: body = body[:len(body) - self._hold(body, ("[[FILE:", "[[END]]"))]
            body = body.rstrip(_TRAIL)
            if body: out.append(("html", body))
        self.buf = ""; self.ended = True
        return out

def _nd(obj):
    return (json.dumps(obj, ensure_ascii=False, separators=(",", ":")) + "\n").encode("utf-8")

async def _llm_stream(messages, model, edit=False, temperature=None):
    stream = await client.with_options(timeout=BUILD_TIMEOUT).chat.completions.create(
        model=model, messages=messages, max_tokens=(EDIT_MAX_TOKENS if edit else BUILD_MAX_TOKENS),
        temperature=(temperature if temperature is not None else (EDIT_TEMPERATURE if edit else 0.7)), stream=True)
    async for chunk in stream:
        if not chunk.choices: continue
        d = chunk.choices[0].delta.content
        if d: yield d

def _trial_adjust(conn, uid, res, trial):
    conn = renew_conn(conn)
    if trial:
        if res.get("visual"):
            res["text"] = ((res.get("text") or "Готово!") + BUILD_TRIAL_NOTE).strip()
        elif res.get("text") == BUILD_VIOLENCE_REFUSAL_TEXT:  # отказ по насилию: попытку возвращаем всегда
            try:
                conn.execute("DELETE FROM trial_build WHERE user_id = ?", (uid,)); conn.commit()
            except Exception:
                log.exception("trial rollback failed")
        elif refund_billed(conn, uid, 0, trial=True):  # пробную попытку возвращаем в рамках бюджета возвратов
            try:
                conn.execute("DELETE FROM trial_build WHERE user_id = ?", (uid,)); conn.commit()
            except Exception:
                log.exception("trial rollback failed")
    return res

_STAGES = ["Анализирую задачу...", "Пишу стили и дизайн...",
           "Добавляю интерактивность...", "Завершаю сборку...",
           "Вношу правки...", "Применяю правки..."]  # 4 и 5 — режим правки (diff)
STREAM_HTML = False  # True — слать код проекта кусками для живого превью; False — только статусы, проект приходит готовым
_RE_STYLE  = re.compile(r"<style\b[^>]*>", re.I)
_RE_SCRIPT = re.compile(r"<script\b(?![^>]*\bsrc\s*=)[^>]*>", re.I)  # только inline, без src
_RE_END    = re.compile(r"</html>", re.I)

async def run_build_stream(conn, uid, user_content, history_text, current_code, weight, trial, watermark=False, model=None, needs_backend=False):
    """NDJSON-поток: {"t":"start"} {"t":"text","d"} {"t":"html","d"} ... {"t":"done"|"error", ...итог как у /ask}"""
    messages = build_messages(user_content, history_text, current_code, needs_backend)
    parser = BuildStreamParser(patch=bool(current_code))
    st = {"raw": "", "hbuf": "", "last": time.monotonic(), "stage": -1, "tail": ""}
    done = False

    def status(i):
        if i <= st["stage"]: return []  # этапы только вперёд, без повторов
        st["stage"] = i
        return [_nd({"t": "status", "d": _STAGES[i]})]

    def scan(part):  # ищем теги в html-части с запасом на разрез по чанкам
        win = st["tail"] + part
        st["tail"] = win[-300:]
        out = []
        if _RE_STYLE.search(win):  out += status(1)
        if _RE_SCRIPT.search(win): out += status(2)
        if _RE_END.search(win):    out += status(3)
        return out

    def handle(d):
        st["raw"] += d
        out = status(0)  # первый токен
        for kind, part in parser.feed(d):
            if kind == "text":
                if not st.get("mute"): out.append(_nd({"t": "text", "d": part}))
            elif kind == "patch": out += status(4)
            else:
                if STREAM_HTML: st["hbuf"] += part
                out += scan(part)
        now_m = time.monotonic()
        if st["hbuf"] and (now_m - st["last"] >= 0.15 or len(st["hbuf"]) > 4000):  # склеиваем мелкие куски, чтобы не дёргать клиента на каждый токен
            out.append(_nd({"t": "html", "d": st["hbuf"]})); st["hbuf"] = ""; st["last"] = now_m
        return out

    def flush():
        out = []
        for kind, part in parser.finish():
            if kind == "text":
                if not st.get("mute"): out.append(_nd({"t": "text", "d": part}))
            elif kind == "patch": out += status(4)
            else:
                if STREAM_HTML: st["hbuf"] += part
                out += scan(part)
        if st["hbuf"]:
            out.append(_nd({"t": "html", "d": st["hbuf"]})); st["hbuf"] = ""
        return out

    release_txn(conn)  # на время генерации в базе не висит ни одна транзакция
    model = model or get_model_for_user(uid, conn)
    try:
        yield _nd({"t": "start"})
        try:
            async for d in _llm_stream(messages, model, edit=bool(current_code)):
                for b in handle(d): yield b
            if "</html>" not in st["raw"].lower() and len(st["raw"]) > 100 and not is_diff_raw(st["raw"]) and not current_code:
                log.warning("Код оборвался, запускаю авто-докрутку (stream)...")
                async for d in _llm_stream(build_continue_messages(messages, st["raw"]), model):
                    for b in handle(d): yield b
        except Exception as e:
            if st["raw"]:
                log.exception("build stream failed mid-way")
                res = _trial_adjust(conn, uid, build_error_result(conn, uid, weight, e, current_code), trial)
                done = True
                yield _nd({"t": "error", **res}); return
            if api_error_kind(e):
                log.error("stream: ошибка API до первого токена: %s", str(e)[:200])
                res = _trial_adjust(conn, uid, build_error_result(conn, uid, weight, e, current_code), trial)
                done = True
                yield _nd({"t": "error", **res}); return
            log.warning("stream failed before first token, fallback to non-stream", exc_info=True)
            res = _trial_adjust(conn, uid, await run_build(conn, uid, user_content, history_text, current_code, weight, watermark, model=model, needs_backend=needs_backend), trial)
            done = True
            yield _nd({"t": "done" if res.get("visual") else "error", **res}); return
        if current_code and edit_needs_rewrite(st["raw"], current_code) and rewrite_allowed(conn, uid, current_code):
            # точечная правка не применилась (или модель отказалась от эффекта текстом) — пересобираем проект целиком
            log.warning("Правка не применилась — пересобираю проект целиком (stream)")
            first_raw = st["raw"]
            yield _nd({"t": "status", "d": "Правка не легла, пересобираю проект целиком..."})
            st.update(raw="", hbuf="", tail="", stage=0, mute=True)
            parser = BuildStreamParser(patch=False)
            try:
                rw = rewrite_messages(user_content, current_code)
                async for d in _llm_stream(rw, model, temperature=0.4):
                    for b in handle(d): yield b
                if "</html>" not in st["raw"].lower() and len(st["raw"]) > 100:
                    async for d in _llm_stream(build_continue_messages(rw, st["raw"]), model, temperature=0.4):
                        for b in handle(d): yield b
            except Exception:
                log.exception("rewrite stream failed")
                st["raw"] = first_raw  # финализируем исходный ответ: вернётся понятная ошибка и возврат
        for b in flush(): yield b
        if st["stage"] == 4:
            for b in status(5): yield b
        res = _trial_adjust(conn, uid, finalize_build(conn, uid, st["raw"], weight, current_code, watermark), trial)
        done = True
        yield _nd({"t": "done" if res.get("visual") else "error", **res})
    finally:
        if not done:  # клиент ушёл / поток оборвался: возвращаем списанное (в рамках бюджета возвратов) и пробную попытку
            conn = renew_conn(conn)
            refund_billed(conn, uid, weight)
            if trial and refund_billed(conn, uid, 0, trial=True):
                try:
                    conn.execute("DELETE FROM trial_build WHERE user_id = ?", (uid,)); conn.commit()
                except Exception:
                    log.exception("trial rollback failed")

def is_valid_html(code):
    """Проверяет, что переданный код похож на HTML-документ"""
    if not isinstance(code, str):
        return False
    code_lower = code.lower()
    return ("<html" in code_lower or "<div" in code_lower or "<body" in code_lower)

def is_banned(uid):
    try:
        conn = get_db_connection()
        return conn.execute("SELECT 1 FROM banned_users WHERE user_id = ?", (uid,)).fetchone() is not None
    except Exception:
        log.exception("is_banned check failed")
        return False  # если БД недоступна — не блокируем всех подряд

def validate_init_data(init_data: str):
    if not init_data or not BOT_TOKEN: return None
    try:
        pairs = dict(parse_qsl(init_data, keep_blank_values=True, strict_parsing=True))
    except ValueError: return None
    recv_hash = pairs.pop("hash", None)
    if not recv_hash: return None
    check_string = "\n".join(f"{k}={v}" for k, v in sorted(pairs.items()))
    secret = hmac.new(b"WebAppData", BOT_TOKEN.encode(), hashlib.sha256).digest()
    calc = hmac.new(secret, check_string.encode(), hashlib.sha256).hexdigest()
    if not hmac.compare_digest(calc, recv_hash): return None
    try:
        auth_date = int(pairs.get("auth_date", "0"))
        if time.time() - auth_date > INIT_DATA_MAX_AGE: return None
        user = json.loads(pairs.get("user", "{}"))
        if not isinstance(user.get("id"), int): return None
    except (ValueError, TypeError): return None
    if user["id"] != ADMIN_ID and is_banned(user["id"]):
        raise BannedError()
    return {"user": user, "start_param": pairs.get("start_param", "")}

def scalar(conn, sql, params=()):
    row = conn.execute(sql, params).fetchone()
    return row[0] if row else 0

def touch_meta(conn, user_id, now=None):
    conn.execute("INSERT OR IGNORE INTO user_meta (user_id, first_seen) VALUES (?, ?)", (user_id, int(now if now is not None else time.time())))

def mark_seen(conn, uid, now):
    conn.execute("INSERT INTO user_seen (user_id, last_seen) VALUES (?, ?) ON CONFLICT(user_id) DO UPDATE SET last_seen = excluded.last_seen", (uid, now))

def stars_price(item):
    if not isinstance(item, str): return None
    if item == "pro": return PLANS["pro"]["price"]
    if item.startswith("plan_"):
        p = PLANS.get(item[5:])
        return p["price"] if p else None
    return SHOP_PRICES.get(item)

def make_link(user_id):
    if not BOT_USERNAME: return ""
    base = f"https://t.me/{BOT_USERNAME}"
    if MINIAPP_SHORT_NAME: base += f"/{MINIAPP_SHORT_NAME}"
    return f"{base}?startapp=ref_{user_id}"

def credit_ref_bonus(conn, referrer_id):
    """+REF_BONUS_ASKS запросов к обычному чату тому, кто пригласил. Тратятся, когда дневной лимит закончился."""
    conn.execute("INSERT OR IGNORE INTO rate_state (user_id, last_ask, day, ask_count, ref_bonus) VALUES (?, 0, '', 0, 0)", (referrer_id,))
    conn.execute("UPDATE rate_state SET ref_bonus = ref_bonus + ? WHERE user_id = ?", (REF_BONUS_ASKS, referrer_id))

def refresh_activation(conn, column, user_id, now):
    """Друг активирован, когда написал REF_MIN_MSGS сообщений и прошло REF_MIN_AGE секунд после входа по ссылке.
    В этот момент пригласившему автоматически начисляется бонус (кнопки «забрать» больше нет)."""
    assert column in ("referrer_id", "invitee_id")
    rows = conn.execute(f"SELECT invitee_id, referrer_id, created_at FROM referrals WHERE {column} = ? AND activated = 0", (user_id,)).fetchall()
    for invitee_id, referrer_id, created_at in rows:
        msgs = scalar(conn, "SELECT msgs FROM user_stats WHERE user_id = ?", (invitee_id,))
        if msgs < REF_MIN_MSGS or now - created_at < REF_MIN_AGE:
            continue
        # условный UPDATE: бонус начислит только тот запрос, который реально переключил флаг (без двойного начисления)
        cur = conn.execute("UPDATE referrals SET activated = 1, activated_at = ?, referrer_claimed = 1, invitee_claimed = 1 WHERE invitee_id = ? AND activated = 0", (now, invitee_id))
        if cur.rowcount == 1:
            credit_ref_bonus(conn, referrer_id)
    conn.commit()

def ref_bonus_info(conn, uid):
    friends = scalar(conn, "SELECT COUNT(*) FROM referrals WHERE referrer_id = ? AND activated = 1", (uid,))
    left = scalar(conn, "SELECT ref_bonus FROM rate_state WHERE user_id = ?", (uid,))
    return {"per_friend": REF_BONUS_ASKS, "friends": friends, "earned": friends * REF_BONUS_ASKS, "left": left}

def activate_quietly(conn, uid, now):
    try:
        refresh_activation(conn, "invitee_id", uid, now)
        refresh_activation(conn, "referrer_id", uid, now)
    except Exception:
        log.exception("referral activation failed")

def count_message(user_id, text_len):
    try: uid = int(user_id)
    except (TypeError, ValueError): return
    if text_len < MSG_MIN_LEN: return
    now = int(time.time())
    try:
        conn = get_db_connection()
        row = conn.execute("SELECT msgs, last_ts FROM user_stats WHERE user_id = ?", (uid,)).fetchone()
        is_long = 1 if text_len >= 50 else 0
        if not row:
            conn.execute("INSERT INTO user_stats (user_id, msgs, last_ts, long_msgs) VALUES (?, 1, ?, ?)", (uid, now, is_long))
        elif now - row[1] >= MSG_MIN_GAP:
            conn.execute("UPDATE user_stats SET msgs = msgs + 1, last_ts = ?, long_msgs = long_msgs + ? WHERE user_id = ?", (now, is_long, uid))
        conn.commit()
    except Exception:
        log.exception("count_message failed")

def get_owned(conn, uid):
    row = conn.execute("SELECT owned FROM users WHERE user_id = ?", (uid,)).fetchone()
    try: owned = json.loads(row[0]) if row and row[0] else {}
    except ValueError: owned = {}
    if not isinstance(owned, dict): owned = {}
    return {k: True for k in owned if k in ITEM_PRICE}

def grant_item(conn, uid, item):
    conn.execute("INSERT OR IGNORE INTO users (user_id) VALUES (?)", (uid,))
    owned = get_owned(conn, uid)
    owned[item] = True
    conn.execute("UPDATE users SET owned = ? WHERE user_id = ?", (json.dumps(owned), uid))
    return owned

def clean_equip(equip, owned):
    out = {}
    if not isinstance(equip, dict): return out
    for slot, item in equip.items():
        if isinstance(item, str) and slot in VALID_SLOTS and ITEM_SLOT.get(item) == slot and owned.get(item): out[slot] = item
    return out

def pro_info(conn, uid, now):
    row = conn.execute("SELECT expires_at, requests_left, auto_renew, plan, units_total FROM pro_state WHERE user_id = ?", (uid,)).fetchone()
    default_limit = PLANS["pro"]["units"]
    if not row: return {"active": False, "expires_at": 0, "left": 0, "auto_renew": 0, "limit": default_limit, "plan": "", "plan_id": ""}
    expires_at, left, auto, plan, total = row
    active = expires_at > now and left > 0
    title = PLANS[plan]["title"] if plan in PLANS else "Pro"
    limit = max(total or 0, left if active else 0) or default_limit
    return {"active": active, "expires_at": expires_at, "left": max(0, left) if active else 0, "auto_renew": auto, "limit": limit, "plan": title, "plan_id": plan or ""}

def get_model_for_user(uid, conn):
    """Модель для сборки сайтов: Pro (активная подписка) -> MODEL_PRO, иначе MODEL_FREE."""
    try:
        if pro_info(conn, uid, int(time.time()))["active"]:
            return MODEL_PRO
    except Exception:
        log.warning("get_model_for_user: pro_info недоступен, беру базовую модель", exc_info=True)
    return MODEL_FREE

def pro_grant(conn, uid, now, charge_id="", plan_id="pro"):
    plan = PLANS.get(plan_id) or PLANS["pro"]
    plan_id = plan_id if plan_id in PLANS else "pro"
    period = plan["days"] * 24 * 3600
    row = conn.execute("SELECT expires_at, requests_left FROM pro_state WHERE user_id = ?", (uid,)).fetchone()
    if row and row[0] > now:
        expires_at = row[0] + period
        left = max(0, row[1]) + plan["units"]
    else:
        expires_at = now + period
        left = plan["units"]
    conn.execute("INSERT INTO pro_state (user_id, expires_at, requests_left, last_charge_id, auto_renew, plan, units_total) VALUES (?, ?, ?, ?, 0, ?, ?) ON CONFLICT(user_id) DO UPDATE SET expires_at = excluded.expires_at, requests_left = excluded.requests_left, last_charge_id = excluded.last_charge_id, plan = excluded.plan, units_total = excluded.units_total", (uid, expires_at, left, charge_id or "", plan_id, left))
    return {"expires_at": expires_at, "left": left}

@app.post("/load_progress")
def load_progress(data: dict = Body(...)):
    info = validate_init_data(data.get("init_data", ""))
    if not info: return {"status": "error", "reason": "auth"}
    uid = info["user"]["id"]; now = int(time.time())
    conn = get_db_connection()
    conn.execute("INSERT OR IGNORE INTO users (user_id) VALUES (?)", (uid,))
    touch_meta(conn, uid, now); mark_seen(conn, uid, now)
    row = conn.execute("SELECT equip FROM users WHERE user_id = ?", (uid,)).fetchone()
    owned = get_owned(conn, uid)
    try: equip = json.loads(row[0]) if row[0] else {}
    except ValueError: equip = {}
    conn.commit()
    activate_quietly(conn, uid, now)  # друг мог выполнить условия, пока приложение было закрыто
    return {"status": "ok", "owned": owned, "equip": clean_equip(equip, owned), "ref_bonus": ref_bonus_info(conn, uid), "pro": pro_info(conn, uid, now), "pro_price": PLANS["pro"]["price"], "free_limit": ASK_DAY_LIMIT, "plans": plans_public(), "build_weight": BUILD_WEIGHT, "trial_build": trial_available(conn, uid)}

@app.post("/ping")
def ping(data: dict = Body(...)):
    info = validate_init_data(data.get("init_data", ""))
    if not info: return {"status": "error", "reason": "auth"}
    conn = get_db_connection()
    now = int(time.time())
    mark_seen(conn, info["user"]["id"], now)
    conn.commit()
    activate_quietly(conn, info["user"]["id"], now)  # приложение открыто, пинг раз в минуту: засчитываем «5 минут» вовремя
    return {"status": "ok"}

@app.post("/admin/stats")
def admin_stats(data: dict = Body(...)):
    info = validate_init_data(data.get("init_data", ""))
    if not info or info["user"]["id"] != ADMIN_ID: return {"status": "error", "reason": "forbidden"}
    conn = get_db_connection()
    total = scalar(conn, "SELECT COUNT(*) FROM users")
    online = scalar(conn, "SELECT COUNT(*) FROM user_seen WHERE last_seen > ?", (int(time.time()) - ONLINE_WINDOW,))
    pro_cnt = scalar(conn, "SELECT COUNT(*) FROM pro_state WHERE expires_at > ? AND requests_left > 0", (int(time.time()),))
    return {"status": "ok", "total": total, "online": online, "pro_active": pro_cnt}

@app.post("/admin/pro")
def admin_pro(data: dict = Body(...)):
    info = validate_init_data(data.get("init_data", ""))
    if not info or info["user"]["id"] != ADMIN_ID: return {"status": "error", "reason": "forbidden"}
    uid = info["user"]["id"]; now = int(time.time())
    conn = get_db_connection()
    conn.execute("INSERT OR IGNORE INTO users (user_id) VALUES (?)", (uid,))
    pro_grant(conn, uid, now, "admin")
    conn.commit()
    return {"status": "ok", "pro": pro_info(conn, uid, now)}

def _admin_target(data):
    try: target = int(str(data.get("user_id", "")).strip())
    except (TypeError, ValueError): return None
    return target if target > 0 else None

@app.post("/admin/ban")
def admin_ban(data: dict = Body(...)):
    info = validate_init_data(data.get("init_data", ""))
    if not info or info["user"]["id"] != ADMIN_ID: return {"status": "error", "reason": "forbidden"}
    target = _admin_target(data)
    if target is None: return {"status": "error", "reason": "bad_id"}
    if target == ADMIN_ID: return {"status": "error", "reason": "self"}
    reason = str(data.get("reason", "") or "").strip()[:200]
    conn = get_db_connection()
    conn.execute(
        "INSERT INTO banned_users (user_id, reason, banned_at) VALUES (?, ?, ?) "
        "ON CONFLICT(user_id) DO UPDATE SET reason = excluded.reason, banned_at = excluded.banned_at",
        (target, reason, int(time.time())))
    conn.commit()
    return {"status": "ok", "banned": scalar(conn, "SELECT COUNT(*) FROM banned_users")}

@app.post("/admin/unban")
def admin_unban(data: dict = Body(...)):
    info = validate_init_data(data.get("init_data", ""))
    if not info or info["user"]["id"] != ADMIN_ID: return {"status": "error", "reason": "forbidden"}
    target = _admin_target(data)
    if target is None: return {"status": "error", "reason": "bad_id"}
    conn = get_db_connection()
    conn.execute("DELETE FROM banned_users WHERE user_id = ?", (target,))
    conn.commit()
    return {"status": "ok", "banned": scalar(conn, "SELECT COUNT(*) FROM banned_users")}

@app.post("/admin/banned")
def admin_banned(data: dict = Body(...)):
    info = validate_init_data(data.get("init_data", ""))
    if not info or info["user"]["id"] != ADMIN_ID: return {"status": "error", "reason": "forbidden"}
    conn = get_db_connection()
    rows = conn.execute("SELECT user_id, reason, banned_at FROM banned_users ORDER BY banned_at DESC LIMIT 30").fetchall()
    total = scalar(conn, "SELECT COUNT(*) FROM banned_users")
    return {"status": "ok", "total": total, "list": [{"id": r[0], "reason": r[1] or "", "at": r[2]} for r in rows]}

@app.post("/save_progress")
def save_progress(data: dict = Body(...)):
    info = validate_init_data(data.get("init_data", ""))
    if not info: return {"status": "error", "reason": "auth"}
    uid = info["user"]["id"]; now = int(time.time())
    conn = get_db_connection()
    conn.execute("INSERT OR IGNORE INTO users (user_id) VALUES (?)", (uid,))
    touch_meta(conn, uid, now)
    owned = get_owned(conn, uid)
    equip = clean_equip(data.get("equip", {}), owned)
    conn.execute("UPDATE users SET equip = ? WHERE user_id = ?", (json.dumps(equip), uid))
    conn.commit()
    return {"status": "ok", "owned": owned, "equip": equip}

@app.post("/referral/register")
async def referral_register(request: Request, data: dict = Body(...)):
    info = validate_init_data(data.get("init_data", ""))
    if not info: return {"status": "error", "reason": "auth"}
    user = info["user"]; uid = user["id"]; param = info["start_param"] or ""
    if not param.startswith("ref_") or not param[4:].isdigit(): return {"status": "ignored", "reason": "no_ref"}
    referrer_id = int(param[4:])
    if user.get("is_bot") or referrer_id == uid: return {"status": "ignored", "reason": "self"}
    now = int(time.time())
    ip = (request.client.host if request and request.client else "unknown") or "unknown"
    day = time.strftime("%Y-%m-%d", time.gmtime(now))
    conn = get_db_connection()
    if conn.execute("SELECT 1 FROM referrals WHERE invitee_id = ?", (uid,)).fetchone(): return {"status": "ignored", "reason": "already"}
    meta = conn.execute("SELECT first_seen FROM user_meta WHERE user_id = ?", (uid,)).fetchone()
    if meta is None: touch_meta(conn, uid, now)
    elif now - meta[0] > REF_NEW_WINDOW: return {"status": "ignored", "reason": "not_new"}
    if not conn.execute("SELECT 1 FROM users WHERE user_id = ?", (referrer_id,)).fetchone(): return {"status": "ignored", "reason": "bad_referrer"}
    if scalar(conn, "SELECT COUNT(*) FROM referrals WHERE referrer_id = ?", (referrer_id,)) >= REF_MAX_TOTAL: return {"status": "ignored", "reason": "limit"}
    if scalar(conn, "SELECT COUNT(*) FROM referrals WHERE referrer_id = ? AND created_at > ?", (referrer_id, now - 86400)) >= REF_MAX_PER_DAY: return {"status": "ignored", "reason": "limit_day"}
    ip_row = conn.execute("SELECT cnt FROM ref_ip WHERE ip = ? AND referrer_id = ? AND day = ?", (ip, referrer_id, day)).fetchone()
    if ip_row and ip_row[0] >= REF_SAME_IP_LIMIT: return {"status": "ignored", "reason": "ip_limit"}
    conn.execute("INSERT INTO referrals (invitee_id, referrer_id, created_at) VALUES (?, ?, ?)", (uid, referrer_id, now))
    conn.execute("INSERT INTO ref_ip (ip, referrer_id, day, cnt) VALUES (?, ?, ?, 1) ON CONFLICT(ip, referrer_id, day) DO UPDATE SET cnt = cnt + 1", (ip, referrer_id, day))
    conn.commit()
    return {"status": "registered", "need_msgs": REF_MIN_MSGS}

@app.post("/referral/status")
def referral_status(data: dict = Body(...)):
    info = validate_init_data(data.get("init_data", ""))
    if not info: return {"status": "error", "reason": "auth"}
    uid = info["user"]["id"]; now = int(time.time())
    conn = get_db_connection()
    refresh_activation(conn, "referrer_id", uid, now)
    refresh_activation(conn, "invitee_id", uid, now)
    invited = scalar(conn, "SELECT COUNT(*) FROM referrals WHERE referrer_id = ?", (uid,))
    bonus = ref_bonus_info(conn, uid)
    return {"status": "ok", "link": make_link(uid), "invited": invited, "active": bonus["friends"], "limit": REF_MAX_TOTAL,
            "bonus_per_friend": bonus["per_friend"], "bonus_earned": bonus["earned"], "bonus_left": bonus["left"], "need_msgs": REF_MIN_MSGS}

_http = httpx.AsyncClient(timeout=8)

# --- ВЛОЖЕНИЯ ДЛЯ /ask ---
def _pdf_to_text(b64):
    try:
        from pypdf import PdfReader
    except ImportError:
        return None, "Чтение PDF сейчас недоступно на сервере."
    try:
        reader = PdfReader(io.BytesIO(base64.b64decode(b64)))
        parts, total = [], 0
        for page in reader.pages[:40]:
            t = (page.extract_text() or "").strip()
            if not t: continue
            parts.append(t); total += len(t)
            if total >= MAX_FILE_TEXT: break
        text = "\n".join(parts)[:MAX_FILE_TEXT]
    except Exception:
        return None, "Не получилось прочитать PDF."
    if not text.strip():
        return None, "В PDF нет текста (похоже на скан). Пришли его как фото."
    return text, ""

def ask_weight(att, visual_mode):
    w = 1
    if att: w = 3
    if visual_mode: w = max(w, 3)
    return w

def build_user_content(text, att, hi=False):
    if not att:
        return text, ""
    if not isinstance(att, dict): return None, "Неверное вложение."
    kind = att.get("kind")
    name = str(att.get("name") or "файл")[:80]
    if kind == "image":
        mime = att.get("mime")
        data = att.get("data")
        if mime not in IMAGE_MIMES or not isinstance(data, str) or len(data) > MAX_IMAGE_B64:
            return None, "Фото слишком большое или в неподдерживаемом формате."
        try:
            raw = base64.b64decode(data, validate=True)
        except Exception:
            return None, "Не получилось прочитать фото."
        if not raw.startswith(IMAGE_MAGIC): return None, "Это не похоже на изображение."
        q = text or "Что на фото? Опиши главное и скажи, чем могу помочь."
        return [{"type": "text", "text": q}, {"type": "image_url", "image_url": {"url": f"data:{mime};base64,{data}", "detail": ("auto" if hi else "low")}}], ""
    if kind == "text":
        body = att.get("text")
        if not isinstance(body, str) or not body.strip(): return None, "Файл пустой."
        q = text or "Посмотри файл: что в нём главное и что можно улучшить?"
        return f"{q}\n\n[Файл «{name}»]\n{body[:MAX_FILE_TEXT]}", ""
    if kind == "pdf":
        data = att.get("data")
        if not isinstance(data, str) or len(data) > MAX_PDF_B64: return None, "PDF слишком большой (максимум 4 МБ)."
        body, err = _pdf_to_text(data)
        if err: return None, err
        q = text or "Кратко перескажи, о чём этот документ."
        return f"{q}\n\n[PDF «{name}»]\n{body}", ""
    if kind in ("docx", "xlsx"):
        data = att.get("data")
        if not isinstance(data, str) or len(data) > MAX_PDF_B64: return None, "Файл слишком большой (максимум 4 МБ)."
        body, err = (docx_to_text if kind == "docx" else xlsx_to_text)(data, MAX_FILE_TEXT)
        if err: return None, err
        q = text or "Кратко опиши, что в этом файле."
        return f"{q}\n\n[Файл «{name}»]\n{body}", ""
    return None, "Этот тип вложения не поддерживается."

@app.exception_handler(DBBusy)
async def _busy_handler(request: Request, exc: DBBusy):
    return JSONResponse({"status": "error", "reason": "busy", "text": BUSY_TEXT, "emotion": "sleepy"}, status_code=503)


# ======================= ПУБЛИКАЦИЯ В NETLIFY =======================
NETLIFY_API = "https://api.netlify.com/api/v1"
PUBLISH_MAX_HTML = 3 * 1024 * 1024        # 3 МБ на один html
PUBLISH_COOLDOWN = 20                      # секунд между публикациями одного пользователя
PUBLISH_PRO_ONLY = os.getenv("PUBLISH_PRO_ONLY", "1") == "1"  # "0" — публикация доступна всем
_publish_last = {}                         # uid -> время последней публикации

class NetlifyError(Exception):
    """Ошибка публикации с готовым текстом для пользователя."""
    def __init__(self, user_msg):
        super().__init__(user_msg)
        self.user_msg = user_msg

def build_site_zip(html_content: str, folder: str = "site") -> bytes:
    """ZIP в памяти: внутри ПАПКИ лежит index.html (не в корне архива)."""
    buf = io.BytesIO()
    with zipfile.ZipFile(buf, "w", zipfile.ZIP_DEFLATED) as zf:
        zf.writestr(f"{folder}/", "")  # запись самой папки, как делает `zip -r`
        zf.writestr(f"{folder}/index.html", html_content.encode("utf-8"))
    return buf.getvalue()

def get_published_url(uid: int, code_hash: str):
    conn = get_db_connection()
    row = conn.execute("SELECT url FROM published_sites WHERE user_id = ? AND code_hash = ?", (uid, code_hash)).fetchone()
    return row[0] if row else None

def save_published_url(uid: int, code_hash: str, url: str):
    conn = get_db_connection()
    conn.execute("INSERT OR REPLACE INTO published_sites (user_id, code_hash, url, created_at) VALUES (?, ?, ?, ?)", (uid, code_hash, url, int(time.time())))
    conn.commit()

async def deploy_to_netlify(html_content: str) -> str:
    """Каждый вызов создаёт НОВЫЙ сайт на Netlify (POST /sites с ZIP) и возвращает его ссылку."""
    token = os.getenv("NETLIFY_TOKEN")
    if not token:
        log.error("netlify: NETLIFY_TOKEN не задан")
        raise NetlifyError("Публикация сейчас недоступна. Попробуй позже.")
    binary_data = await asyncio.to_thread(build_site_zip, html_content)
    headers = {"Authorization": f"Bearer {token}", "Content-Type": "application/zip"}
    json_headers = {"Authorization": f"Bearer {token}", "Content-Type": "application/json"}
    timeout = httpx.Timeout(60.0, connect=10.0)
    try:
        async with httpx.AsyncClient(timeout=timeout) as http:
            # POST /sites без ID: Netlify создаёт новый сайт из ZIP (content= — «сырое» тело запроса)
            r = await http.post(f"{NETLIFY_API}/sites", headers=headers, content=binary_data)
            if r.status_code >= 400:
                log.error("netlify create site failed: %s %s", r.status_code, (r.text or "")[:300])
                if r.status_code in (401, 403):
                    raise NetlifyError("Публикация сейчас недоступна: сервис сайтов отклонил доступ. Мы уже разбираемся.")
                if r.status_code == 429:
                    raise NetlifyError("Слишком много публикаций сразу. Подожди минуту и попробуй ещё раз.")
                raise NetlifyError("Не удалось опубликовать сайт. Попробуй ещё раз чуть позже.")
            info = r.json()
            site_id = info.get("id")
            url = info.get("ssl_url") or info.get("url")
            if not url:
                log.error("netlify: в ответе нет ssl_url/url: %s", str(info)[:300])
                raise NetlifyError("Сайт опубликован, но ссылку получить не удалось. Попробуй ещё раз.")

            # принудительная публичность: PATCH, при отказе — PUT; ответ Netlify логируем всегда
            if site_id:
                body = {"published_deploy": {"visibility": "public"}}
                for method in ("PATCH", "PUT"):
                    try:
                        pr = await http.request(method, f"{NETLIFY_API}/sites/{site_id}", headers=json_headers, json=body, timeout=15.0)
                        log.info("netlify set public %s: site=%s status=%s body=%s", method, site_id, pr.status_code, (pr.text or "")[:300])
                        if pr.status_code < 400:
                            break
                        log.error("netlify set public %s failed: site=%s %s %s", method, site_id, pr.status_code, (pr.text or "")[:300])
                    except Exception:
                        log.exception("netlify set public %s error: site=%s", method, site_id)
            else:
                log.error("netlify: в ответе нет id сайта, пропускаю публичность")

            # ответ — объект САЙТА: id деплоя лежит в deploy_id / published_deploy
            pub = info.get("published_deploy") or {}
            deploy_id = info.get("deploy_id") or pub.get("id")
            state = pub.get("state") or info.get("state")
            # ZIP-деплой обрабатывается асинхронно: ждём ready (до ~20 с)
            for _ in range(20):
                if state in ("ready", "current"): break
                if state == "error":
                    log.error("netlify deploy state=error: %s", pub.get("error_message"))
                    raise NetlifyError("Netlify не смог обработать сайт. Проверь код и попробуй ещё раз.")
                if not deploy_id: break
                await asyncio.sleep(1)
                try:
                    g = await http.get(f"{NETLIFY_API}/deploys/{deploy_id}", headers={"Authorization": f"Bearer {token}"})
                    if g.status_code == 200:
                        pub = g.json(); state = pub.get("state")
                except httpx.HTTPError:
                    pass
            return url
    except NetlifyError:
        raise
    except httpx.TimeoutException:
        log.error("netlify: timeout")
        raise NetlifyError("Публикация заняла слишком много времени. Попробуй ещё раз.")
    except (httpx.HTTPError, ValueError):
        log.exception("netlify: network/parse error")
        raise NetlifyError("Не удалось связаться с сервисом публикации. Попробуй ещё раз.")

PAYWALL_PUBLISH_MSG = "Автоматическая публикация доступна только на платных тарифах. Выбери тариф, чтобы публиковать сайты в один клик."

@app.post("/publish")
async def publish(data: dict = Body(...)):
    info = validate_init_data(data.get("init_data", ""))
    if not info:
        return JSONResponse({"status": "error", "message": "Ошибка авторизации. Открой приложение из Telegram."}, status_code=401)
    uid = info["user"]["id"]
    if PUBLISH_PRO_ONLY and uid != ADMIN_ID:
        is_pro = await asyncio.to_thread(lambda: pro_info(get_db_connection(), uid, int(time.time()))["active"])
        if not is_pro:
            return JSONResponse({"status": "error", "message": PAYWALL_PUBLISH_MSG}, status_code=402)
    html_code = data.get("html_code")
    if not isinstance(html_code, str) or not is_valid_html(html_code):
        return JSONResponse({"status": "error", "message": "Нечего публиковать: сначала создай сайт."}, status_code=400)
    if len(html_code.encode("utf-8")) > PUBLISH_MAX_HTML:
        return JSONResponse({"status": "error", "message": "Сайт слишком большой для публикации."}, status_code=413)

    out_warning = None
    if re.search(r"fetch\(", html_code):
        out_warning = "Сайт обращается к серверу (fetch). Netlify хранит только статику: серверную часть (main.py) нужно запустить отдельно, например на Render."

    # защита от дублей: тот же код у того же пользователя -> отдаём сохранённую ссылку
    code_hash = hashlib.md5(html_code.encode()).hexdigest()
    try:
        existing = await asyncio.to_thread(get_published_url, uid, code_hash)
    except Exception:
        log.warning("published_sites: не удалось проверить дубль uid=%s", uid, exc_info=True)
        existing = None
    if existing:
        out = {"status": "ok", "url": existing, "duplicate": True}
        if out_warning: out["warning"] = out_warning
        return out

    now = time.monotonic()
    if now - _publish_last.get(uid, 0) < PUBLISH_COOLDOWN:
        return JSONResponse({"status": "error", "message": "Подожди несколько секунд перед следующей публикацией."}, status_code=429)
    _publish_last[uid] = now
    try:
        url = await deploy_to_netlify(html_code)
    except NetlifyError as e:
        _publish_last.pop(uid, None)  # неудачная публикация не блокирует повтор
        return JSONResponse({"status": "error", "message": e.user_msg}, status_code=502)
    except Exception:
        log.exception("publish failed uid=%s", uid)
        _publish_last.pop(uid, None)
        return JSONResponse({"status": "error", "message": "Что-то пошло не так при публикации. Попробуй ещё раз."}, status_code=500)

    try:
        await asyncio.to_thread(save_published_url, uid, code_hash, url)
    except Exception:
        log.warning("published_sites: не удалось сохранить uid=%s", uid, exc_info=True)

    out = {"status": "ok", "url": url}
    if out_warning: out["warning"] = out_warning
    return out

@app.post("/ask")
async def ask(request: Request):
    refund_pending = None  # (weight, is_pro): что вернуть, если после списания всё упадёт
    try:
        try:
            if int(request.headers.get("content-length", "0")) > MAX_BODY_BYTES:
                return JSONResponse({"text": "Вложение слишком большое.", "emotion": "sad"}, status_code=413)
        except ValueError:
            pass
        data = await request.json()
        full_text = str(data.get("text", "") or "")  # полный текст нужен для импорта кода
        raw_text = full_text.strip()[:MAX_TEXT_LEN]
        user_text = raw_text.lower()
        history_raw = str(data.get("history", "") or "")
        history_len = len(history_raw)
        history_text = history_raw[-12000:]
        persona = data.get("persona", "hologram")
        att = data.get("attachment")
        ask_info = validate_init_data(data.get("init_data", ""))
        if not ask_info:
            return JSONResponse({"text": "Ошибка авторизации. Открой приложение из Telegram.", "emotion": "sad"}, status_code=401)
        uid = ask_info["user"]["id"]
        now = int(time.time())
        conn = get_db_connection()
        if persona == "astronaut" and not get_owned(conn, uid).get("astro"):
            persona = "hologram"
        if not raw_text and not att:
            return {"text": "Я не вижу текста.", "emotion": "thinking", "pro": pro_info(conn, uid, now)}
        user_content = None
        pro_pre = pro_info(conn, uid, now)
        # ИМПОРТ ГОТОВОГО КОДА: .html-вложение или код текстом. Модель не вызывается, лимиты не трогаем.
        if not data.get("edit_project"):
            imported = extract_import_code(att, full_text)
            if imported is not None and not isinstance(att, dict):
                # код текстом: если это не похоже на настоящий HTML-документ (вопрос про тег <html> и т.п.) — это обычный чат
                imp = imported.strip()
                if len(imp) < MIN_IMPORT_LEN or not is_valid_html(imp):
                    imported = None
            if imported is not None:
                if not import_rate_ok(conn, uid, now):
                    return JSONResponse({"text": IMPORT_RATE_TEXT, "emotion": "thinking", "pro": pro_pre}, status_code=429)
                imported = imported.strip()
                if len(imported) > MAX_EDIT_CODE_LEN:
                    return {"text": IMPORT_TOO_BIG_TEXT, "emotion": "sad", "pro": pro_pre}
                if len(imported) < MIN_IMPORT_LEN or not is_valid_html(imported):
                    return {"text": "Не похоже на HTML-проект. Пришли файл целиком.", "emotion": "sad", "pro": pro_pre}
                return {"text": IMPORT_OK_TEXT, "emotion": "happy", "imported": True,
                        "visual": {"type": "html", "code": imported}, "pro": pro_pre}
        # ПРОДОЛЖЕНИЕ СБОРКИ: пользователь прислал только название («Шопи фай») в ответ на вопрос бота по запросу на сайт
        if not data.get("edit_project") and not att and not data.get("build_choice") and not build_intent(user_text) and is_bare_name(raw_text):
            prev_req = pending_build_request(history_text, raw_text)
            if prev_req:
                raw_text = f"{prev_req[:600]}. Название проекта: {raw_text}"
                user_text = raw_text.lower()
        visual_mode = bool(VISUAL_RE.search(user_text))
        weight = ask_weight(att, visual_mode)
        if visual_mode:
            weight += VISUAL_EXTRA  # доплата за визуал берётся ДО генерации
        lv = data.get("last_visual")
        if isinstance(lv, dict) and lv.get("pid") and not lv.get("code") and data.get("edit_project") and pro_pre["active"]:
            _pc = load_preview_code(conn, lv.get("pid"), uid)  # только свой проект и только для активного Pro
            if _pc: lv = {"type": "html", "code": strip_watermark(_pc)}
        current_code = None
        if isinstance(lv, dict) and lv.get("type") == "html" and isinstance(lv.get("code"), str):
            # Проверяем длину И то, что это реально HTML-код, а не мусор
            if MIN_IMPORT_LEN <= len(lv["code"]) <= 1_000_000 and is_valid_html(lv["code"]):  # верхний предел проверяет calc_edit_weight
                current_code = lv["code"]
        edit_req = bool(data.get("edit_project"))
        if edit_req and not pro_pre["active"]:
            # правки только на Pro: отказ ДО списаний и до вызова модели, токены не тратятся
            return JSONResponse({"text": EDIT_PRO_ONLY_TEXT, "emotion": "sleepy", "pro": pro_pre, "limit_reached": True}, status_code=402)
        if edit_req and att:
            # правка + вложение: отказ ДО списаний и до вызова модели, токены не тратятся
            return {"text": EDIT_ATTACH_TEXT, "emotion": "thinking", "pro": pro_pre}
        if edit_req and not current_code:
            return {"text": "Не нашёл проект для правки. Нажми «Править проект» под нужным сайтом ещё раз.", "emotion": "sad", "pro": pro_pre}
        is_edit = edit_req and bool(current_code)
        # ответ на уточняющий вопрос (кнопки «Собрать сайт» / «Просто совет»): приходит с клиента в build_choice
        build_choice = str(data.get("build_choice") or "")
        if build_choice not in ("build", "advice"): build_choice = ""
        if not is_edit and not att and not build_choice and build_intent(user_text) == "ask":
            # есть действие и прилагательное, но нет существительного: ничего не списываем и в нейросеть не идём
            return {"text": BUILD_CHOICE_QUESTION, "emotion": "thinking", "pro": pro_pre, "choices": BUILD_CHOICES}
        build_mode = is_edit or build_choice == "build" or (build_choice != "advice" and wants_build(user_text))
        if not build_mode and is_forbidden_chat(user_text):
            # запрещённая просьба в обычном чате: отказ до списаний и до вызова модели, токены не тратятся
            return {"text": CHAT_REFUSAL_TEXT, "emotion": "sad", "pro": pro_pre}
        search_mode = (not att) and (not build_mode) and (not visual_mode) and wants_search(raw_text)
        if search_mode: weight = max(weight, SEARCH_WEIGHT)
        
        # длинная история (> 20 000 симв.): +10 лимитов к обычному чату.
        # Считаем по history_len (полная история от клиента): history_text обрезан до 12 000 и не может быть > 20 000.
        # Сборки Студии не трогаем: у них фиксированные цены 60/120/180/150, а в модель уходит лишь 4000 симв. истории.
        if not build_mode and history_len > HISTORY_LONG_CHARS:
            weight += HISTORY_LONG_EXTRA

        trial = False
        needs_backend = False  # админка / БД / сервер: генерируем index.html + main.py
        if build_mode:
            # запрещённые темы: отказ ДО любых списаний и до вызова модели, токены не тратятся
            if is_forbidden_build(user_text):
                return {"text": BUILD_REFUSAL_TEXT, "emotion": "sad", "pro": pro_pre}
            if is_violent_build(user_text):
                return {"text": BUILD_VIOLENCE_REFUSAL_TEXT, "emotion": "sad", "pro": pro_pre}
            is_realtime = (not is_edit) and bool(REALTIME_RE.search(user_text))
            if is_edit:
                try:
                    weight = calc_edit_weight(len(current_code))  # 50 / 150 / 300 по размеру кода
                except ProjectTooLargeError as e:
                    return {"text": str(e), "emotion": "sad", "pro": pro_pre}  # до списаний
            elif is_realtime:
                weight = BUILD_WEIGHT_COMPLEX  # мессенджер / WebSocket: сервер + интерфейс, самая дорогая сборка
            elif is_complex_request(user_text):
                weight = BUILD_WEIGHT_COMPLEX  # 180: игра (даже если названа «сайтом»), презентация, 3D — приоритет выше приложения (120) и сайта (60)
            elif APP_BUILD_RE.search(user_text):
                weight = BUILD_WEIGHT_MEDIUM   # 120: приложение, трекер, калькулятор, дашборд (важнее слова «сайт» и кнопки выбора)
            elif SIMPLE_BUILD_RE.search(user_text) or build_choice == "build":
                weight = BUILD_WEIGHT_SIMPLE   # 60: простой сайт, лендинг, визитка
            else:
                weight = BUILD_WEIGHT_MEDIUM   # 120: приложение, калькулятор, дашборд, бот
            
            needs_backend = (not is_edit) and bool(NEEDS_BACKEND_RE.search(user_text))
            if needs_backend:
                weight = BUILD_WEIGHT_BACKEND  # 200 вместо 60–120

            # ЗАЩИТА БЕСПЛАТНИКОВ: если сложный проект, не даем делать его бесплатно
            if not pro_pre["active"]:
                if needs_backend:  # два файла и сервер: бесплатной пробной сборки нет
                    return JSONResponse({"text": BACKEND_PAYWALL_TEXT, "emotion": "sleepy", "pro": pro_pre, "limit_reached": True}, status_code=402)
                if is_realtime:  # бесплатной пробной сборки для мессенджеров нет
                    return JSONResponse({"text": REALTIME_PAYWALL_TEXT, "emotion": "sleepy", "pro": pro_pre, "limit_reached": True}, status_code=402)
                if is_complex_request(user_text):
                    return JSONResponse({"text": "Сложные проекты (игры, презентации, 3D) доступны на платных тарифах. На бесплатной пробной сборке можно сделать только простой сайт-визитку.", "emotion": "sleepy", "pro": pro_pre, "limit_reached": True}, status_code=402)
                
                # ЗАЩИТА: правки недоступны на бесплатной пробной сборке
                if is_edit:
                    return JSONResponse({"text": EDIT_PRO_ONLY_TEXT, "emotion": "sleepy", "pro": pro_pre, "limit_reached": True}, status_code=402)

                if trial_available(conn, uid):
                    trial = True
                    weight = 0
                else:
                    return JSONResponse({"text": "Бесплатную пробную сборку ты уже использовал. Дальше студия сайтов и приложений доступна на тарифах — выбери подходящий!", "emotion": "happy", "pro": pro_pre, "limit_reached": True}, status_code=402)
            elif pro_pre["left"] < weight:
                return JSONResponse({"text": "Для этой задачи не хватает лимита. Пополни тариф.", "emotion": "sleepy", "pro": pro_pre}, status_code=402)
                
        if build_mode:
            # кулдаун сборки: атомарный слот, повтор раньше чем через BUILD_COOLDOWN секунд в нейросеть не уходит
            conn.execute("INSERT OR IGNORE INTO rate_state (user_id, last_ask, day, ask_count) VALUES (?, 0, '', 0)", (uid,))
            cur = conn.execute("UPDATE rate_state SET last_build = ? WHERE user_id = ? AND last_build <= ?", (now, uid, now - BUILD_COOLDOWN))
            conn.commit()
            if cur.rowcount != 1:
                return JSONResponse({"text": BUILD_COOLDOWN_TEXT, "emotion": "thinking", "pro": pro_pre}, status_code=429)

        if att:
            user_content, att_err = await asyncio.to_thread(build_user_content, raw_text, att, pro_pre["active"])
            if att_err:
                return {"text": att_err, "emotion": "sad", "pro": pro_info(conn, uid, now)}
        else:
            user_content = raw_text
            if build_mode and build_choice == "build" and not BUILD_THING_RE.search(user_text):
                user_content += "\n(Пользователь выбрал: собрать сайт. Сделай одностраничный сайт по этому описанию.)"
            
        pro = None
        used_bonus = False  # запрос оплачен бонусными запросами за друзей (нужно для возврата при сбое)
        try:
            pro = pro_info(conn, uid, now)
            today = time.strftime("%Y-%m-%d", time.gmtime(now))
            # атомарный «слот»: параллельные запросы не могут одновременно пройти паузу
            conn.execute("INSERT OR IGNORE INTO rate_state (user_id, last_ask, day, ask_count) VALUES (?, 0, ?, 0)", (uid, today))
            cur = conn.execute("UPDATE rate_state SET last_ask = ? WHERE user_id = ? AND last_ask <= ?", (now, uid, now - ASK_MIN_GAP))
            if cur.rowcount != 1:
                conn.commit()
                return JSONResponse({"text": "Подожди пару секунд…", "emotion": "thinking"}, status_code=429)
            if pro["active"]:
                cur = conn.execute("UPDATE pro_state SET requests_left = requests_left - ? WHERE user_id = ? AND requests_left >= ? AND expires_at > ?", (weight, uid, weight, now))
                if cur.rowcount != 1:
                    conn.commit()
                    pro_now = pro_info(conn, uid, now)
                    if pro_now.get("active") and pro_now.get("left", 0) > 0:
                        msg = "Для этого запроса не хватает лимита. Пополни тариф."
                    else:
                        msg = "Лимит Pro закончился. Продли подписку."
                    return JSONResponse({"text": msg, "emotion": "sleepy", "pro": pro_now}, status_code=402)
                pro = pro_info(conn, uid, now)
            elif not trial:
                # проверка дневного лимита и прибавка — одним атомарным запросом
                cur = conn.execute(
                    "UPDATE rate_state SET day = ?, ask_count = (CASE WHEN day = ? THEN ask_count ELSE 0 END) + ? "
                    "WHERE user_id = ? AND (CASE WHEN day = ? THEN ask_count ELSE 0 END) < ?",
                    (today, today, weight, uid, today, ASK_DAY_LIMIT))
                allowed = cur.rowcount == 1
                if not allowed and not build_mode:
                    # дневной лимит закончился: тратим бонусные запросы за приглашённых друзей (только обычный чат)
                    cur = conn.execute("UPDATE rate_state SET ref_bonus = ref_bonus - ? WHERE user_id = ? AND ref_bonus >= ?", (weight, uid, weight))
                    used_bonus = allowed = cur.rowcount == 1
                if not allowed:
                    conn.commit()
                    return JSONResponse({"text": "Бесплатный лимит на сегодня закончился. Выбери тариф — и пиши без дневных ограничений. Или пригласи друга: +10 запросов за каждого.", "emotion": "sleepy", "pro": pro, "limit_reached": True}, status_code=402)
            conn.commit()
        finally:
            pass
        
        log.info("ask uid=%s weight=%s build=%s visual=%s search=%s history_len=%s", uid, weight, build_mode, visual_mode, search_mode, history_len)
        asyncio.get_running_loop().run_in_executor(None, count_message, uid, max(len(raw_text), MSG_MIN_LEN if att else 0))
        if build_mode:
            build_model = get_model_for_user(uid, conn)
            if trial:
                # занимаем пробную попытку атомарно: из N параллельных запросов пройдёт один
                cur = conn.execute("INSERT OR IGNORE INTO trial_build (user_id, used_at) VALUES (?, ?)", (uid, int(time.time())))
                conn.commit()
                if cur.rowcount != 1:
                    return JSONResponse({"text": "Бесплатную пробную сборку ты уже использовал. Выбери тариф, чтобы продолжить.", "emotion": "happy", "pro": pro, "limit_reached": True}, status_code=402)
            if data.get("stream") is True:
                return StreamingResponse(
                    run_build_stream(conn, uid, user_content, history_text, strip_watermark(current_code) if is_edit else None, weight, trial, watermark=not pro_pre["active"], model=build_model, needs_backend=needs_backend),
                    media_type="application/x-ndjson",
                    headers={"Cache-Control": "no-cache, no-transform", "X-Accel-Buffering": "no"})
            res = await run_build(conn, uid, user_content, history_text, strip_watermark(current_code) if is_edit else None, weight, watermark=not pro_pre["active"], model=build_model, needs_backend=needs_backend)
            if res.get("visual") and needs_backend: res["needs_backend"] = True
            if trial:
                if res.get("visual"):
                    res["text"] = ((res.get("text") or "Готово!") + " Это была бесплатная пробная сборка — дальше студия доступна на тарифах.").strip()
                else:
                    conn.execute("DELETE FROM trial_build WHERE user_id = ?", (uid,))
                    conn.commit()
            return res
        
        refund_pending = (weight, True if (pro and pro.get("active")) else ("bonus" if used_bonus else False))
            
        if visual_mode:
            system_prompt = (ASTRO_PROMPT if persona == "astronaut" else BASE_PROMPT) + VISUAL_RULES
        else:
            system_prompt = ASTRO_PLAIN if persona == "astronaut" else BASE_PLAIN
        
        if lv and DEPLOY_RE.search(user_text):
            system_prompt += "\n\nВАЖНО: Пользователь только что сгенерировал проект в Студии. Если он спрашивает про запуск, оплату или хостинг — давай инструкции именно для этого проекта"
            
        if not att and persona == "hologram" and ("станц" in user_text or "танец" in user_text or "танцуй" in user_text):
            chosen_dance = random.choice(DANCE_LIST)
            phrase = ""
            try:
                response = await llm([{"role": "system", "content": "Придумай короткую веселую фразу (1 предложение, до 80 символов), чтобы начать танец. Только сама фраза, без пояснений."}, {"role": "user", "content": "Я прошу тебя станцевать."}], 120, temperature=0.7)
                phrase = (response.choices[0].message.content or "").strip()
            except Exception:
                log.exception("dance phrase failed")
            if not phrase or len(phrase) > 160:
                phrase = random.choice(["Музыка, свет — поехали!", "Держись, сейчас будет жарко!", "Смотри и завидуй!", "Включаю режим диско!"])
            return {"text": phrase, "action": "dance", "dance_name": chosen_dance, "pro": pro}
            
        messages = [{"role": "system", "content": system_prompt}]
        if history_text: messages.append({"role": "user", "content": f"Контекст: вот предыдущие сообщения нашего диалога (для справки, отвечать на них не нужно):\n{history_text}"})
        if search_mode:
            ctx = await search_context(_http, raw_text)
            if ctx:
                messages.append({"role": "user", "content": ctx})
            else:
                log.warning("search uid=%s: поиск вернул пусто (ключ, лимит или ошибка API, см. строку srezai search выше)", uid)
                messages.append({"role": "user", "content": "Поиск в интернете сейчас не сработал. Не говори, что у тебя нет доступа в интернет вообще: скажи, что поиск временно недоступен, и ответь по своим знаниям, предупредив, что данные могут быть устаревшими."})
        messages.append({"role": "user", "content": user_content})
        if visual_mode:
            response = await chat_json(messages, AI_MAX_TOKENS_VISUAL)
            raw = response.choices[0].message.content or ""
            try:
                result = extract_json(raw)
            except ValueError:
                result = salvage_json(raw) or {"text": "Не получилось собрать ответ целиком. Попробуй попросить проще или короче.", "emotion": "sad"}
        else:
            response = await llm(messages, AI_MAX_TOKENS)
            raw = response.choices[0].message.content or ""
            plain_text, plain_emo = parse_plain(raw)
            result = {"text": plain_text, "emotion": plain_emo}
            
        if not visual_mode and not str(result.get("text") or "").strip():
            log.warning("empty reply, retrying once; raw=%r", raw[:200])
            try:
                retry = await llm(messages + [{"role": "user", "content": "Ответ пустой. Дай полный ответ по существу на моё сообщение выше. Формат: метка [[ANSWER]], слово-эмоция, затем текст."}], AI_MAX_TOKENS + 800, temperature=0.3)
                t2, e2 = parse_plain(retry.choices[0].message.content or "")
                if t2.strip():
                    result = {"text": t2, "emotion": e2}
            except Exception:
                log.exception("retry failed")
                
        if not str(result.get("text") or "").strip():
            log.warning("empty reply from model, finish_reason=%s, raw=%r", getattr(response.choices[0], "finish_reason", None), raw[:200])
            result["text"] = "Не получилось ответить, попробуй ещё раз."
            refund_ask(conn, uid, *refund_pending)
            if refund_pending[1] is True: pro = pro_info(conn, uid, int(time.time()))
            refund_pending = None
            
        emotion = result.get("emotion")
        if emotion not in EMOTIONS_OK: emotion = "happy"
        visual = clean_visual(result.get("visual")) if visual_mode else None
        if visual_mode and not visual and refund_pending:  # refund_pending=None => уже вернули выше
            # визуал не получился (запрос просто совпал с «покажи/схема» и т.п.) — возвращаем доплату
            try:
                if pro and pro.get("active"):
                    refund_units(conn, uid, VISUAL_EXTRA)
                    pro = pro_info(conn, uid, int(time.time()))
                elif used_bonus:
                    conn.execute("UPDATE rate_state SET ref_bonus = ref_bonus + ? WHERE user_id = ?", (VISUAL_EXTRA, uid))
                    conn.commit()
                else:
                    conn.execute("UPDATE rate_state SET ask_count = MAX(ask_count - ?, 0) WHERE user_id = ?", (VISUAL_EXTRA, uid))
                    conn.commit()
            except Exception:
                log.exception("visual refund failed")
        return {"text": str(result.get("text") or "Что-то я задумался...").strip(), "emotion": emotion, "visual": visual, "pro": pro}
    except BannedError:
        raise
    except DBBusy:
        log.error("ask: база занята, отвечаю сообщением о нагрузке")
        if refund_pending:
            refund_ask(get_db_connection(), uid, *refund_pending)
        return JSONResponse({"text": BUSY_TEXT, "emotion": "sleepy"}, status_code=429)
    except Exception as e:
        log.exception("ask failed")
        if refund_pending:
            refund_ask(get_db_connection(), uid, *refund_pending)
        kind = api_error_kind(e)
        if kind:
            if kind == "funds": alert_api_funds(e)
            return {"text": API_LOAD_TEXT, "emotion": "sleepy"}
        return {"text": "Не получилось ответить. Попробуй ещё раз через пару секунд.", "emotion": "sad"}

@app.get("/p/{pid}")
def preview_page(pid: str):
    """Страница проекта для iframe. Плашка подмешивается здесь, пока у владельца нет Pro."""
    try:
        if not PID_RE.match(pid):
            return HTMLResponse(PREVIEW_GONE, status_code=404, headers=PREVIEW_HEADERS)
        conn = get_db_connection()
        row = conn.execute("SELECT user_id, code FROM previews WHERE pid = ?", (pid,)).fetchone()
        if not row:
            return HTMLResponse(PREVIEW_GONE, status_code=404, headers=PREVIEW_HEADERS)
        owner, code = row[0], strip_watermark(row[1])
        if not pro_info(conn, owner, int(time.time()))["active"]:
            code = add_watermark(code)
        return HTMLResponse(PREVIEW_HEAD + code, headers=PREVIEW_HEADERS)
    except Exception:
        log.exception("preview failed")
        return HTMLResponse(PREVIEW_GONE, status_code=503, headers=PREVIEW_HEADERS)

@app.post("/export")
def export_code(data: dict = Body(...)):
    """Код проекта без плашки: только для активного Pro, проверка на сервере."""
    info = validate_init_data(data.get("init_data", ""))
    if not info: return JSONResponse({"status": "error", "reason": "auth", "text": "Ошибка авторизации."}, status_code=401)
    uid = info["user"]["id"]; now = int(time.time())
    conn = get_db_connection()
    if not pro_info(conn, uid, now)["active"]:
        return JSONResponse({"status": "error", "reason": "pro_only", "text": "Код доступен только на тарифе."}, status_code=402)
    pid = data.get("pid")
    if pid:
        code = load_preview_code(conn, pid, uid)
        if not code: return JSONResponse({"status": "error", "reason": "not_found", "text": "Проект не найден."}, status_code=404)
    else:
        code = data.get("code")
        if not isinstance(code, str) or not code or len(code) > 900_000:
            return JSONResponse({"status": "error", "reason": "bad_code", "text": "Не удалось получить код."}, status_code=400)
    return {"status": "ok", "code": strip_watermark(code)}

@app.post("/pro/status")
def pro_status(data: dict = Body(...)):
    info = validate_init_data(data.get("init_data", ""))
    if not info: return {"status": "error", "reason": "auth"}
    uid = info["user"]["id"]; now = int(time.time())
    conn = get_db_connection()
    return {"status": "ok", "pro": pro_info(conn, uid, now), "price": PLANS["pro"]["price"], "limit": PLANS["pro"]["units"], "free_limit": ASK_DAY_LIMIT, "plans": plans_public(), "build_weight": BUILD_WEIGHT, "trial_build": trial_available(conn, uid)}

@app.post("/pro/invoice")
async def pro_invoice(data: dict = Body(...)):
    info = validate_init_data(data.get("init_data", ""))
    if not info: return {"error": "auth"}
    uid = info["user"]["id"]
    if not WEBHOOK_SECRET: return {"error": "payments_disabled"}
    plan_id = data.get("plan") or "pro"
    plan = PLANS.get(plan_id)
    if not plan: return {"error": "bad_plan"}
    payload = {"title": f"Holo Friend «{plan['title']}» — {plan['days']} дней", "description": f"Тариф «{plan['title']}» на {plan['days']} дней: больше запросов и сборок сайтов, автопубликация", "payload": f"{uid}|plan_{plan_id}", "currency": "XTR", "provider_token": "", "prices": [{"label": f"Тариф «{plan['title']}»", "amount": plan["price"]}]}
    r = await tg_call("createInvoiceLink", payload)
    res = r.json()
    if res.get("ok"): return {"link": res["result"]}
    return {"error": "Не удалось создать счет", "details": res}

@app.post("/pay/telegram")
async def pay_telegram(data: dict = Body(...)):
    """Счёт на тариф: currency=RUB (карта, ЮKassa, цены в копейках) или XTR (звёзды Telegram)."""
    info = validate_init_data(data.get("init_data", ""))
    if not info: return {"error": "auth"}
    uid = info["user"]["id"]
    if not WEBHOOK_SECRET: return {"error": "payments_disabled"}
    plan_id = data.get("plan_id") or "pro"
    currency = str(data.get("currency") or "XTR").upper()
    plan = PLANS.get(plan_id)
    if not plan: return {"error": "bad_plan"}
    if currency not in ("RUB", "XTR"): return {"error": "bad_currency"}
    amount = plan_amount(plan_id, currency)
    provider_token = ""
    if currency == "RUB":
        provider_token = os.getenv("YOOKASSA_PROVIDER_TOKEN", "")
        if not provider_token: return {"error": "card_disabled"}
    payload = {
        "title": f"Holo Friend: {plan['title']}"[:32],  # лимит Telegram на заголовок — 32 символа
        "description": f"Тариф «{plan['title']}» на {plan['days']} дней: больше запросов и сборок сайтов, автопубликация",
        "payload": f"{uid}|plan_{plan_id}",
        "currency": currency,
        "provider_token": provider_token,
        "prices": [{"label": f"Тариф «{plan['title']}»", "amount": amount}],
    }
    try:
        r = await tg_call("createInvoiceLink", payload)
        res = r.json()
    except Exception:
        log.exception("pay/telegram: createInvoiceLink упал uid=%s currency=%s", uid, currency)
        return {"error": "invoice_failed"}
    if res.get("ok"): return {"invoice_url": res["result"]}
    log.error("pay/telegram: Telegram отказал %s %s: %s", currency, plan_id, str(res)[:300])
    return {"error": "invoice_failed", "details": res.get("description")}

@app.post("/clear")
async def clear(request: Request):
    return {"status": "ok"}

def check_payment(from_id, payload, currency, amount):
    try: p_uid, item = str(payload).split("|", 1); p_uid = int(p_uid)
    except (ValueError, TypeError): return False, "Неверные данные платежа"
    if p_uid != from_id: return False, "Платёж не прошёл проверку"
    if item == "pro" or item.startswith("plan_"):
        # сумма должна совпасть с ценой тарифа именно в той валюте, в которой выставлен счёт
        plan_id = "pro" if item == "pro" else item[5:]
        expected = plan_amount(plan_id, currency)
        if expected is None or expected != amount: return False, "Платёж не прошёл проверку"
        return True, ""
    if currency != "XTR" or stars_price(item) != amount: return False, "Платёж не прошёл проверку"
    return True, ""

async def tg_call(method, payload):
    url = f"https://api.telegram.org/bot{BOT_TOKEN}/{method}"
    return await _http.post(url, json=payload, timeout=10)

def _invoice_blocked(uid, item_id):
    if item_id == "pro" or item_id.startswith("plan_"): return None
    conn = get_db_connection()
    return "owned" if get_owned(conn, uid).get(item_id) else None

@app.post("/invoice")
async def create_invoice(data: dict = Body(...)):
    info = validate_init_data(data.get("init_data", ""))
    if not info: return {"error": "auth"}
    uid = info["user"]["id"]; item_id = data.get("item")
    price = stars_price(item_id)
    if not price: return {"error": "item not found"}
    if not WEBHOOK_SECRET: return {"error": "payments_disabled"}
    blocked = await asyncio.to_thread(_invoice_blocked, uid, item_id)
    if blocked: return {"error": blocked}
    title = f"Покупка: {item_id}"
    payload = {"title": title, "description": "Покупка в Holo Friend", "payload": f"{uid}|{item_id}", "currency": "XTR", "provider_token": "", "prices": [{"label": "Товар", "amount": price}]}
    r = await tg_call("createInvoiceLink", payload)
    res = r.json()
    if res.get("ok"): return {"link": res["result"]}
    return {"error": "Не удалось создать счет", "details": res}

def _precheck_db(uid, item):
    if item == "pro" or item.startswith("plan_"): return True, ""
    conn = get_db_connection()
    if get_owned(conn, uid).get(item): return False, "Этот предмет у тебя уже есть"
    return True, ""

def _apply_payment(from_id, item, charge):
    conn = get_db_connection()
    if charge and not conn.execute("SELECT 1 FROM payments WHERE charge_id = ?", (charge,)).fetchone():
        conn.execute("INSERT INTO payments (charge_id, user_id, item, created_at) VALUES (?, ?, ?, ?)", (charge, from_id, item, int(time.time())))
        if item == "pro" or item.startswith("plan_"):
            conn.execute("INSERT OR IGNORE INTO users (user_id) VALUES (?)", (from_id,))
            pro_grant(conn, from_id, int(time.time()), charge, "pro" if item == "pro" else item[5:])
        else:
            grant_item(conn, from_id, item)
        conn.commit()

@app.post("/telegram/webhook")
async def telegram_webhook(request: Request):
    try:
        if not WEBHOOK_SECRET:
            data = await request.json()
            if "pre_checkout_query" in data: await tg_call("answerPreCheckoutQuery", {"pre_checkout_query_id": data["pre_checkout_query"]["id"], "ok": False, "error_message": "Покупки временно недоступны"})
            return {"ok": True}
        if not hmac.compare_digest(request.headers.get("X-Telegram-Bot-Api-Secret-Token", ""), WEBHOOK_SECRET): return {"ok": False}
        data = await request.json()
        if "pre_checkout_query" in data:
            q = data["pre_checkout_query"]
            ok, err = check_payment(q.get("from", {}).get("id"), q.get("invoice_payload", ""), q.get("currency"), q.get("total_amount"))
            if ok:
                try:
                    _uid, _item = q["invoice_payload"].split("|", 1)
                    ok, err = await asyncio.to_thread(_precheck_db, int(_uid), _item)
                except Exception: ok, err = False, "Неверные данные платежа"
            body = {"pre_checkout_query_id": q["id"], "ok": ok}
            if not ok: body["error_message"] = err
            await tg_call("answerPreCheckoutQuery", body)
            return {"ok": True}
        if "message" in data and "successful_payment" in data["message"]:
            msg = data["message"]; pay = msg["successful_payment"]; from_id = msg.get("from", {}).get("id")
            ok, _err = check_payment(from_id, pay.get("invoice_payload", ""), pay.get("currency"), pay.get("total_amount"))
            if ok:
                _uid, item = pay["invoice_payload"].split("|", 1); charge = pay.get("telegram_payment_charge_id", "")
                await asyncio.to_thread(_apply_payment, from_id, item, charge)
            await tg_call("sendMessage", {"chat_id": msg["chat"]["id"], "text": "✅ Оплата прошла успешно! Возвращайся в приложение."})
            return {"ok": True}
        return {"ok": True}
    except Exception:
        log.exception("webhook failed")
        return {"ok": True}

# --- ПОДДЕРЖКА ---
class SupportRequest(BaseModel):
    init_data: str
    message: str
    user_id: int = None

SUPPORT_GAP = 30
SUPPORT_MAX_LEN = 2000
_support_last = {}

def _one_line(v, n):
    return re.sub(r"[\r\n\t]+", " ", str(v or "")).strip()[:n]

@app.post("/support/send")
async def support_send(data: SupportRequest):
    info = validate_init_data(data.init_data)
    if not info: return {"status": "error", "reason": "auth"}
    uid = info["user"]["id"]
    message = (data.message or "").strip()
    if not message: return {"status": "error", "reason": "empty"}
    nowf = time.time()
    if nowf - _support_last.get(uid, 0) < SUPPORT_GAP: return {"status": "error", "reason": "too_often"}
    if len(_support_last) > 5000: _support_last.clear()
    _support_last[uid] = nowf
    username = _one_line(info["user"].get("username"), 40) or "нет юзернейма"
    first_name = _one_line(info["user"].get("first_name"), 60) or "Без имени"
    # обычный текст (без Markdown): пользователь не может сломать разметку или подделать ссылку.
    # Настоящий ID — в самой последней строке, её подделать нельзя.
    text_to_send = (f"🆘 Сообщение в поддержку\n👤 {first_name} (@{username})\n\n💬 Сообщение:\n{message[:SUPPORT_MAX_LEN]}\n\n"
                    f"———\n🆔 ID отправителя (по подписи Telegram): {uid}")
    try:
        r = await tg_call("sendMessage", {"chat_id": ADMIN_ID, "text": text_to_send, "disable_web_page_preview": True})
        res = r.json()
        if res.get("ok"):
            return {"status": "ok"}
        log.error("Telegram API error: %s", res)
        return {"status": "error", "reason": "telegram_api_error"}
    except Exception:
        log.exception("support_send failed")
        return {"status": "error", "reason": "send_failed"}

@app.api_route("/", methods=["GET", "HEAD"])
def health(): return {"status": "ok"}
