from fastapi import FastAPI, Request, Body
from fastapi.responses import JSONResponse, StreamingResponse
import asyncio
from fastapi.middleware.cors import CORSMiddleware
from openai import AsyncOpenAI
import os
import io
import re
import json
import base64
import random
import time
import hmac
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

REF_REWARD_REFERRER = 10
REF_REWARD_INVITEE = 5
REF_MIN_MSGS = 3
REF_MIN_AGE = 300
REF_NEW_WINDOW = 3600
REF_MAX_TOTAL = 30
REF_MAX_PER_DAY = 5
REF_REQUIRE_PURCHASE = True
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

OTHER_BODY_LIMIT = 1024 * 1024  # все эндпоинты кроме /ask

class BodyLimitMiddleware:
    """Режет слишком большие тела запросов: и по Content-Length, и по факту при chunked-передаче."""
    def __init__(self, app):
        self.app = app

    async def __call__(self, scope, receive, send):
        if scope["type"] != "http" or scope["method"] not in ("POST", "PUT", "PATCH"):
            return await self.app(scope, receive, send)
        limit = MAX_BODY_BYTES if scope["path"] == "/ask" else OTHER_BODY_LIMIT
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
BUILD_MODEL = os.getenv("BUILD_MODEL", "anthropic/claude-sonnet-5-5")
BUILD_MAX_TOKENS = int(os.getenv("BUILD_MAX_TOKENS", "20000")) 
BUILD_TIMEOUT = 170

# ВЕСА: 100 за новую сборку, 150 за правку
BUILD_WEIGHT = int(os.getenv("BUILD_WEIGHT", "100"))
BUILD_WEIGHT_EDIT = int(os.getenv("BUILD_WEIGHT_EDIT", "150")) 
BUILD_WEIGHT_SIMPLE = int(os.getenv("BUILD_WEIGHT_SIMPLE", "60"))    # простой сайт / лендинг / визитка
BUILD_WEIGHT_MEDIUM = int(os.getenv("BUILD_WEIGHT_MEDIUM", "120"))   # приложение, калькулятор, дашборд, бот
BUILD_WEIGHT_COMPLEX = int(os.getenv("BUILD_WEIGHT_COMPLEX", "180")) # игра, презентация, 3D, анимации
HISTORY_LONG_CHARS = 20000
HISTORY_LONG_EXTRA = 10

MAX_BUILD_LEN = 70000

# ДОБАВЛЕНЫ ПРЕЗЕНТАЦИИ И СЛАЙДЫ
BUILD_THING_RE = re.compile(r"презентаци|слайд|сайт|лендинг|landing|веб-?страниц|страниц[ауы] |приложени|игр[ауы]\b|мини-?игр|калькулятор|визитк|портфолио|дашборд|интерфейс|тетрис|змейк|сапёр|сапер|секундомер|таймер|тудулист|to-?do|конвертер|генератор", re.I)
BUILD_ACTION_RE = re.compile(r"сдела[йюемт]|созда[йюемт]|напиш[иу]|разработ|собер[иу]|запил|сгенер|сверста|набросай|нарису|покаж|мне нужен|мне нужна|мне нужно|нужен мне|нужна мне", re.I)
BUILD_NEG_RE = re.compile(r"как (сделать|создать|написать|разработать)|что такое|чем отлича|объясни|посоветуй|идеи для|расскажи", re.I)
# (не используется: правка включается кнопкой «Править проект», а не по словам)
BUILD_EDIT_RE = re.compile(r"измени|поменяй|замени|добавь|убери|удали|исправь|поправь|переделай|перепиши|обнови|улучши|выровня|выравни|по центру|центр|подвинь|сдвинь|перенеси|не переноси|переноси|поставь|вынеси|шапк|header|меню|навигац|пункт|раздел|секци|блок|футер|подвал|заголов|отступ|размер|шрифт|цвет|фон|кнопк|анимаци|адаптив|мобил|тёмн|темн|светл|ярче|крупнее|мельче|красивее|круче|больше|меньше|не\s+ровн|крив|съехал|сломал|не работает|вау|перелив|свет|неон|пульс|мерцан|градиент|стильн|современн|сделай так|хочу чтобы", re.I)
SIMPLE_BUILD_RE = re.compile(r"сайт|лендинг|landing|визитк|портфолио|веб-?страниц|страниц[ауы] ", re.I)
DEPLOY_RE = re.compile(r"запуск|запуст|запущ|хостинг|хостит|render|оплат|юкасс|yookassa|yokassa|опубликов|публикац|выложи|выкладыв|netlify|vercel|github|гитхаб|сервер|деплой|deploy|домен|app ?store|апп ?стор|как открыть|как использовать", re.I)
VISUAL_RE = re.compile(r"покаж|как выгляд|нарису|нарисов|изобраз|иллюстр|визуал|превью|предпросмотр|отрисуй|отрендер|схем|диаграмм|график|макет|логотип", re.I)
# Регулярка для сложных проектов (игры, презентации)
COMPLEX_RE = re.compile(r"игр[ауы]\b|мини-?игр|тетрис|змейк|сапёр|сапер|презентаци|слайд|3d|анимаци", re.I)

# --- ПОДПИСКА PRO (ОБНОВЛЕННЫЕ ЦЕНЫ) ---
PLANS = {
    "start":    {"title": "Старт",  "price": 899,  "units": 500,  "days": 30},
    "pro":      {"title": "Про",    "price": 1999, "units": 1500, "days": 30},
    "business": {"title": "Максимум", "price": 4999, "units": 4000, "days": 365},
}
PRO_WARN_LEFT = 100

def plans_public():
    return [{"id": k, "title": v["title"], "price": v["price"], "units": v["units"], "days": v["days"], "builds": v["units"] // BUILD_WEIGHT} for k, v in PLANS.items()]

# --- ЭКОНОМИКА И ЗАЩИТА ---
QUEST_EARN_CAP = 20
DAILY_COOLDOWN = 20 * 3600
DAILY_STREAK_KEEP = 48 * 3600
DAILY_DROP_CHANCE = 0.15
DAILY_DROP_MAX_PRICE = 65
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
_rng = random.SystemRandom()

TURSO_URL = os.getenv("TURSO_DATABASE_URL")
TURSO_TOKEN = os.getenv("TURSO_AUTH_TOKEN")

def get_db_connection():
    return libsql.connect(database=TURSO_URL, auth_token=TURSO_TOKEN)

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
    conn.execute("CREATE TABLE IF NOT EXISTS daily_state (user_id INTEGER PRIMARY KEY, last_ts INTEGER DEFAULT 0, streak INTEGER DEFAULT 0)")
    conn.execute("CREATE TABLE IF NOT EXISTS earn_state (user_id INTEGER PRIMARY KEY, day TEXT DEFAULT '', earned INTEGER DEFAULT 0)")
    conn.execute("CREATE TABLE IF NOT EXISTS payments (charge_id TEXT PRIMARY KEY, user_id INTEGER NOT NULL, item TEXT NOT NULL, created_at INTEGER NOT NULL)")
    conn.execute("CREATE TABLE IF NOT EXISTS user_seen (user_id INTEGER PRIMARY KEY, last_seen INTEGER DEFAULT 0)")
    conn.execute("CREATE TABLE IF NOT EXISTS rate_state (user_id INTEGER PRIMARY KEY, last_ask INTEGER DEFAULT 0, day TEXT DEFAULT '', ask_count INTEGER DEFAULT 0)")
    conn.execute("CREATE TABLE IF NOT EXISTS pro_state (user_id INTEGER PRIMARY KEY, expires_at INTEGER DEFAULT 0, requests_left INTEGER DEFAULT 0, last_charge_id TEXT DEFAULT '', auto_renew INTEGER DEFAULT 0)")
    conn.execute("CREATE TABLE IF NOT EXISTS trial_build (user_id INTEGER PRIMARY KEY, used_at INTEGER DEFAULT 0)")
    conn.execute("CREATE TABLE IF NOT EXISTS banned_users (user_id INTEGER PRIMARY KEY, reason TEXT DEFAULT '', banned_at INTEGER DEFAULT 0)")
    for col in ("plan TEXT DEFAULT ''", "units_total INTEGER DEFAULT 0"):
        try:
            conn.execute(f"ALTER TABLE pro_state ADD COLUMN {col}")
        except Exception:
            pass
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

BUILD_SYSTEM = """
Ты — Holo в режиме СТУДИИ: ведущий веб-дизайнер и фронтенд-разработчик уровня топовой студии. Пользователь просит сайт, лендинг, веб-приложение, игру или интерфейс — ты выдаёшь законченную работу, от которой хочется сказать «вау». index.html — всегда обязательный файл.

КРИТИЧЕСКИЕ ПРИНЦИПЫ ДИЗАЙНА (КАК В CLAUDE ARTIFACTS):
- Смелость: Всегда выбирай смелые и неожиданные дизайнерские решения вместо безопасных и скучных. Используй яркие цвета, динамичные сетки, выразительную типографику.
- Интерактивность: Статичный дизайн — это исключение. Обязательно добавляй анимации, hover-эффекты и интерактивные элементы. Создавай рабочие демо, а не картинки.
- Чистота: Весь код в одном файле. Не используй localStorage или sessionStorage. Храни состояние в переменных JS.
- Контент: Пиши осмысленные тексты на языке пользователя. Никакого lorem ipsum.

ВИЗУАЛ (самое важное):
- Сначала придумай концепцию: настроение, 2–3 акцентных цвета + нейтрали, одну сильную идею первого экрана.
- Цвета и размеры — через CSS-переменные (:root). Используй современные приёмы: градиентные меши и размытые блобы, стекломорфизм (backdrop-filter), мягкие тени и свечение, тонкие границы rgba, скругления 16–28px, крупную типографику с clamp().
- Типографика: 1–2 шрифта Google Fonts через <link> (Manrope, Inter, Unbounded) + fallback system-ui. Чёткая иерархия.
- Сетка: CSS grid/flex, отступы по шкале 8px, контейнер max-width ~1120px, много воздуха.
- «Картинки» и иллюстрации рисуй сам: inline SVG, CSS-градиенты, canvas, паттерны. Внешние изображения запрещены. Иконки — inline SVG.
- Движение: плавные hover/active-состояния, появление блоков при прокрутке (IntersectionObserver), 1–2 фирменные анимации.
- Тёмная или светлая тема — по смыслу; контраст текста не ниже AA.
- Адаптивность mobile-first: отлично выглядит от 320px до 1440px.

ТРЕБОВАНИЯ К ДИЗАЙНУ (ОБЯЗАТЕЛЬНО):
- Шрифты: Google Fonts через <link> (Manrope, Inter, Unbounded). Никаких Arial/Times.
- Цвета: Никакого чисто белого фона. Используй CSS-переменные, градиенты или темную тему.
- Формы: border-radius минимум 12px для кнопок и карточек.
- Кнопки: Обязательный hover-эффект.
- Сетка: Flexbox/Grid, много отступов (padding).
- Картинки: Вместо пустых заглушек — inline SVG или CSS-градиенты.

СОДЕРЖАНИЕ:
- Настоящие тексты на языке пользователя, без lorem ipsum: конкретные заголовки, выгоды, цифры, отзывы, цены.
- Сайт: навигация, первый экран с призывом к действию, 3–6 секций, финальный призыв, подвал.
- Приложение, игра: полностью рабочая логика на JS: состояния, клавиатура и тач, анимации, счёт, пауза.
- Если пользователь просит очень сложный сайт (интернет-магазин, соцсеть), сделай только ГЛАВНУЮ СТРАНИЦУ с красивым дизайном. Не пытайся написать весь сайт целиком — сделай идеальную обложку.
- ПРЕЗЕНТАЦИИ: Если просят презентацию, используй чистый CSS для переключения слайдов (например, :target или скрытые чекбоксы), чтобы не раздувать код JavaScript. Максимум 3-5 слайдов.

РЕЖИМ ПРАВОК (если дана «ТЕКУЩАЯ ВЕРСИЯ ПРОЕКТА»):
- Если пользователь пишет неточно («не ровно», «криво», «сделай красиво», «сделай вау», «добавь неон»), ты должен сам проанализировать текущий HTML-код и внести правки.
- КРИТИЧЕСКИ ВАЖНО: Если передана «ТЕКУЩАЯ ВЕРСИЯ ПРОЕКТА», ты ОБЯЗАН вернуть полную копию файла от <!doctype html> до </html> с внесенными правками. Нельзя возвращать только фрагмент CSS или HTML. Если ты вернешь неполный код, пользователь увидит белый экран.

ФАЙЛЫ ПРОЕКТА:
- Если просят простой сайт или простую игру (Змейка, 2048) — генерируй ТОЛЬКО index.html.
- Если просят Telegram-бота или Мини-апп с логикой — генерируй ТРИ файла: main.py, requirements.txt и index.html.
- Всегда объясняй в чате, какие файлы ты создал.

ТЕХНИКА:
- Один самодостаточный файл: <!doctype html>, весь CSS в <style>, JS в <script> перед </body>.
- Запросы к сети (fetch, XHR), отправка форм, alert/confirm/prompt, window.open и переходы по внешним ссылкам не работают: используй <button type="button">. Навигация только внутри одной страницы: якоря href="#id" на секции этого же файла или переключение секций через JS. Ссылок на другие файлы и страницы (about.html, /contacts) не делай.
- Чистый код без ошибок в консоли.
- КРИТИЧЕСКОЕ ОГРАНИЧЕНИЕ: Код должен быть ОЧЕНЬ компактным. Не делай огромных отступов, не дублируй стили, не пиши длинные комментарии. Максимальный размер кода — 2000 строк. Если нужно написать больше, пиши более эффективно, используя классы и общие стили.

ФОРМАТ ОТВЕТА — строго так, без лишнего текста вокруг:
[[TEXT]]
1–3 коротких предложения для чата: что сделано и какие файлы ты создал.
[[EMOTION]]
happy
[[HTML]]
<!doctype html>
...полный файл index.html...
[[FILE:main.py]]
...только если нужен бот или мини-апп с логикой...
[[FILE:requirements.txt]]
...только если нужен бот или мини-апп с логикой...
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
        if is_pro:
            conn.execute("UPDATE pro_state SET requests_left = requests_left + ? WHERE user_id = ?", (weight, uid))
        else:
            conn.execute("UPDATE rate_state SET ask_count = MAX(ask_count - ?, 0) WHERE user_id = ?", (weight, uid))
        conn.commit()
    except Exception:
        log.exception("ask refund failed")

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

async def llm(messages, max_tokens, json_mode=False, temperature=None):
    base = dict(model=AI_MODEL, messages=messages, max_tokens=max_tokens, temperature=AI_TEMPERATURE if temperature is None else temperature)
    if json_mode: base["response_format"] = {"type": "json_object"}
    try:
        return await client.chat.completions.create(**base)
    except Exception:
        if not json_mode: raise
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

def wants_build(user_text):
    # только НОВАЯ сборка. Правка включается явно: кнопка «Править проект» -> edit_project=true в /ask
    if BUILD_NEG_RE.search(user_text): return False
    return bool(BUILD_THING_RE.search(user_text) and BUILD_ACTION_RE.search(user_text))

BUILD_FAIL = {"text": "Не получилось собрать проект. Попробуй написать задачу чуть проще или покороче.", "emotion": "sad"}
BUILD_TRIAL_NOTE = " Это была бесплатная пробная сборка — дальше студия доступна на тарифах."

def build_messages(user_content, history_text, current_code):
    messages = [{"role": "system", "content": BUILD_SYSTEM}]
    if history_text: messages.append({"role": "user", "content": f"Контекст: предыдущие сообщения диалога:\n{history_text[-4000:]}"})
    if current_code:
        messages.append({"role": "user", "content": "ТЕКУЩАЯ ВЕРСИЯ ПРОЕКТА (HTML):\n" + current_code})
    else:
        messages.append({"role": "user", "content": "Проект создается с нуля."})
    messages.append({"role": "user", "content": f"ЗАДАЧА: {user_content}"})
    return messages

def build_continue_messages(messages, raw):
    tail = raw[-500:]
    return messages + [
        {"role": "assistant", "content": raw},
        {"role": "user", "content": f"Ты оборвался на полуслове: ...{tail}\nПродолжи генерацию ровно с этого места и закончи код тегом </html>. Не пиши ничего лишнего, только продолжение кода."}
    ]

def build_error_result(conn, uid, weight, e):
    refund_units(conn, uid, weight)
    err_text = str(e).lower()
    pro_now = pro_info(conn, uid, int(time.time()))
    if "429" in err_text or "quota" in err_text or "overloaded" in err_text:
        return {"text": "Сейчас огромный спрос, сервера перегружены. Я вернул тебе сборку, попробуй через пару минут.", "emotion": "sleepy", "pro": pro_now}
    if "402" in err_text or "insufficient" in err_text or "balance" in err_text:
        return {"text": "У бота закончились ресурсы на генерацию. Я вернул тебе сборку, попробуй чуть позже.", "emotion": "sad", "pro": pro_now}
    return {**BUILD_FAIL, "pro": pro_now}

def finalize_build(conn, uid, raw, weight):
    parsed = parse_build(raw)
    visual = None
    if parsed:
        text, emotion, html = parsed
        visual = clean_visual({"type": "html", "code": html}, MAX_BUILD_LEN)
    if not parsed or not visual:
        refund_units(conn, uid, weight)
        log.error(f"Build failed to parse. Raw: {(raw or '')[:200]}")
        return {**BUILD_FAIL, "pro": pro_info(conn, uid, int(time.time()))}
    if emotion not in EMOTIONS_OK: emotion = "happy"
    return {"text": text or "Готово! Смотри превью.", "emotion": emotion, "visual": visual, "pro": pro_info(conn, uid, int(time.time()))}

async def run_build(conn, uid, user_content, history_text, current_code, weight):
    messages = build_messages(user_content, history_text, current_code)
    try:
        resp = await client.with_options(timeout=BUILD_TIMEOUT).chat.completions.create(
            model=BUILD_MODEL,
            messages=messages,
            max_tokens=BUILD_MAX_TOKENS,
            temperature=0.7
        )
        raw = resp.choices[0].message.content or ""
        if "</html>" not in raw.lower() and len(raw) > 100:
            log.warning("Код оборвался, запускаю авто-докрутку...")
            resp2 = await client.with_options(timeout=BUILD_TIMEOUT).chat.completions.create(
                model=BUILD_MODEL,
                messages=build_continue_messages(messages, raw),
                max_tokens=BUILD_MAX_TOKENS,
                temperature=0.7
            )
            raw = raw + (resp2.choices[0].message.content or "")
    except Exception as e:
        log.exception("build request failed")
        return build_error_result(conn, uid, weight, e)
    return finalize_build(conn, uid, raw, weight)

# ---------- СТРИМИНГ СБОРКИ ----------
_TRAIL = "`\n\r\t "

class BuildStreamParser:
    """Режет поток [[TEXT]]..[[EMOTION]]..[[HTML]]..[[FILE:..]]..[[END]] на события ("text", ..) и ("html", ..).
    В html попадает только чистый код: без всего до [[HTML]], без ```-оград и без всего после [[END]] / [[FILE:."""
    def __init__(self):
        self.buf = ""
        self.state = "pre"   # pre -> text -> emotion -> head -> html
        self.ended = False

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
            if self.state == "pre":
                i = self.buf.find("[[TEXT]]")
                if i < 0:
                    self.buf = self.buf[-8:]
                    break
                self.buf = self.buf[i + 8:]; self.state = "text"; continue
            if self.state == "text":
                found = [(self.buf.find(m), m) for m in ("[[EMOTION]]", "[[HTML]]")]
                found = [(i, m) for i, m in found if i >= 0]
                if not found:
                    safe = len(self.buf) - self._hold(self.buf, ("[[EMOTION]]", "[[HTML]]"))
                    if safe > 0:
                        out.append(("text", self.buf[:safe])); self.buf = self.buf[safe:]
                    break
                i, m = min(found)
                if i: out.append(("text", self.buf[:i]))
                self.buf = self.buf[i + len(m):]
                self.state = "emotion" if m == "[[EMOTION]]" else "head"
                continue
            if self.state == "emotion":
                i = self.buf.find("[[HTML]]")
                if i < 0: break
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
            b = self.buf[:len(self.buf) - self._hold(self.buf, ("[[EMOTION]]", "[[HTML]]"))]
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

async def _llm_stream(messages):
    stream = await client.with_options(timeout=BUILD_TIMEOUT).chat.completions.create(
        model=BUILD_MODEL, messages=messages, max_tokens=BUILD_MAX_TOKENS, temperature=0.7, stream=True)
    async for chunk in stream:
        if not chunk.choices: continue
        d = chunk.choices[0].delta.content
        if d: yield d

def _trial_adjust(conn, uid, res, trial):
    if trial:
        if res.get("visual"):
            res["text"] = ((res.get("text") or "Готово!") + BUILD_TRIAL_NOTE).strip()
        else:
            try:
                conn.execute("DELETE FROM trial_build WHERE user_id = ?", (uid,)); conn.commit()
            except Exception:
                log.exception("trial rollback failed")
    return res

async def run_build_stream(conn, uid, user_content, history_text, current_code, weight, trial):
    """NDJSON-поток: {"t":"start"} {"t":"text","d"} {"t":"html","d"} ... {"t":"done"|"error", ...итог как у /ask}"""
    messages = build_messages(user_content, history_text, current_code)
    parser = BuildStreamParser()
    st = {"raw": "", "hbuf": "", "last": time.monotonic()}
    done = False

    def handle(d):
        st["raw"] += d
        out = []
        for kind, part in parser.feed(d):
            if kind == "text": out.append(_nd({"t": "text", "d": part}))
            else: st["hbuf"] += part
        now_m = time.monotonic()
        if st["hbuf"] and (now_m - st["last"] >= 0.15 or len(st["hbuf"]) > 4000):  # склеиваем мелкие куски, чтобы не дёргать клиента на каждый токен
            out.append(_nd({"t": "html", "d": st["hbuf"]})); st["hbuf"] = ""; st["last"] = now_m
        return out

    def flush():
        out = []
        for kind, part in parser.finish():
            if kind == "text": out.append(_nd({"t": "text", "d": part}))
            else: st["hbuf"] += part
        if st["hbuf"]:
            out.append(_nd({"t": "html", "d": st["hbuf"]})); st["hbuf"] = ""
        return out

    try:
        yield _nd({"t": "start"})
        try:
            async for d in _llm_stream(messages):
                for b in handle(d): yield b
            if "</html>" not in st["raw"].lower() and len(st["raw"]) > 100:
                log.warning("Код оборвался, запускаю авто-докрутку (stream)...")
                async for d in _llm_stream(build_continue_messages(messages, st["raw"])):
                    for b in handle(d): yield b
        except Exception as e:
            if st["raw"]:
                log.exception("build stream failed mid-way")
                res = _trial_adjust(conn, uid, build_error_result(conn, uid, weight, e), trial)
                done = True
                yield _nd({"t": "error", **res}); return
            log.warning("stream failed before first token, fallback to non-stream", exc_info=True)
            res = _trial_adjust(conn, uid, await run_build(conn, uid, user_content, history_text, current_code, weight), trial)
            done = True
            yield _nd({"t": "done" if res.get("visual") else "error", **res}); return
        for b in flush(): yield b
        res = _trial_adjust(conn, uid, finalize_build(conn, uid, st["raw"], weight), trial)
        done = True
        yield _nd({"t": "done" if res.get("visual") else "error", **res})
    finally:
        if not done:  # клиент ушёл / поток оборвался: возвращаем списанное и пробную попытку
            refund_units(conn, uid, weight)
            if trial:
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

def try_spend(conn, uid, amount):
    cur = conn.execute("UPDATE users SET bal = bal - ? WHERE user_id = ? AND bal >= ?", (amount, uid, amount))
    return cur.rowcount == 1

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

def refresh_activation(conn, column, user_id, now):
    assert column in ("referrer_id", "invitee_id")
    rows = conn.execute(f"SELECT invitee_id, created_at FROM referrals WHERE {column} = ? AND activated = 0", (user_id,)).fetchall()
    for invitee_id, created_at in rows:
        msgs = scalar(conn, "SELECT msgs FROM user_stats WHERE user_id = ?", (invitee_id,))
        if msgs < REF_MIN_MSGS or now - created_at < REF_MIN_AGE:
            continue
        if REF_REQUIRE_PURCHASE:
            paid = scalar(conn, "SELECT COUNT(*) FROM payments WHERE user_id = ?", (invitee_id,)) > 0
            if not paid:
                owned = get_owned(conn, invitee_id)
                if not owned: continue
        conn.execute("UPDATE referrals SET activated = 1, activated_at = ? WHERE invitee_id = ?", (now, invitee_id))
    conn.commit()

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

def daily_info(conn, uid, now):
    row = conn.execute("SELECT last_ts, streak FROM daily_state WHERE user_id = ?", (uid,)).fetchone()
    last, streak = (row[0], row[1]) if row else (0, 0)
    return {"wait": max(0, last + DAILY_COOLDOWN - now), "streak": streak if last and now - last < DAILY_STREAK_KEEP else 0}

def pro_info(conn, uid, now):
    row = conn.execute("SELECT expires_at, requests_left, auto_renew, plan, units_total FROM pro_state WHERE user_id = ?", (uid,)).fetchone()
    default_limit = PLANS["pro"]["units"]
    if not row: return {"active": False, "expires_at": 0, "left": 0, "auto_renew": 0, "limit": default_limit, "plan": "", "plan_id": ""}
    expires_at, left, auto, plan, total = row
    active = expires_at > now and left > 0
    title = PLANS[plan]["title"] if plan in PLANS else "Pro"
    limit = max(total or 0, left if active else 0) or default_limit
    return {"active": active, "expires_at": expires_at, "left": max(0, left) if active else 0, "auto_renew": auto, "limit": limit, "plan": title, "plan_id": plan or ""}

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
    row = conn.execute("SELECT bal, equip FROM users WHERE user_id = ?", (uid,)).fetchone()
    owned = get_owned(conn, uid)
    try: equip = json.loads(row[1]) if row[1] else {}
    except ValueError: equip = {}
    conn.commit()
    return {"status": "ok", "bal": row[0], "owned": owned, "equip": clean_equip(equip, owned), "daily": daily_info(conn, uid, now), "pro": pro_info(conn, uid, now), "pro_price": PLANS["pro"]["price"], "free_limit": ASK_DAY_LIMIT, "plans": plans_public(), "build_weight": BUILD_WEIGHT, "trial_build": trial_available(conn, uid)}

@app.post("/ping")
def ping(data: dict = Body(...)):
    info = validate_init_data(data.get("init_data", ""))
    if not info: return {"status": "error", "reason": "auth"}
    conn = get_db_connection()
    mark_seen(conn, info["user"]["id"], int(time.time()))
    conn.commit()
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

@app.post("/admin/give")
def admin_give(data: dict = Body(...)):
    info = validate_init_data(data.get("init_data", ""))
    if not info or info["user"]["id"] != ADMIN_ID: return {"status": "error", "reason": "forbidden"}
    try: amount = int(data.get("amount", 0))
    except (TypeError, ValueError): return {"status": "error", "reason": "bad_amount"}
    if amount <= 0 or amount > 10**9: return {"status": "error", "reason": "bad_amount"}
    uid = info["user"]["id"]
    conn = get_db_connection()
    conn.execute("INSERT OR IGNORE INTO users (user_id) VALUES (?)", (uid,))
    conn.execute("UPDATE users SET bal = bal + ? WHERE user_id = ?", (amount, uid))
    conn.commit()
    return {"status": "ok", "bal": scalar(conn, "SELECT bal FROM users WHERE user_id = ?", (uid,))}

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
    server_bal = conn.execute("SELECT bal FROM users WHERE user_id = ?", (uid,)).fetchone()[0]
    owned = get_owned(conn, uid)
    try: client_bal = int(data.get("bal", server_bal))
    except (TypeError, ValueError): client_bal = server_bal
    accepted = 0
    delta = client_bal - server_bal
    if delta > 0:
        day = time.strftime("%Y-%m-%d", time.gmtime(now))
        e = conn.execute("SELECT day, earned FROM earn_state WHERE user_id = ?", (uid,)).fetchone()
        earned_today = e[1] if e and e[0] == day else 0
        accepted = min(delta, max(0, QUEST_EARN_CAP - earned_today))
        if accepted > 0:
            conn.execute("UPDATE users SET bal = bal + ? WHERE user_id = ?", (accepted, uid))
            conn.execute("INSERT INTO earn_state (user_id, day, earned) VALUES (?, ?, ?) ON CONFLICT(user_id) DO UPDATE SET day = excluded.day, earned = excluded.earned", (uid, day, earned_today + accepted))
    equip = clean_equip(data.get("equip", {}), owned)
    conn.execute("UPDATE users SET equip = ? WHERE user_id = ?", (json.dumps(equip), uid))
    conn.commit()
    return {"status": "ok", "bal": server_bal + accepted, "owned": owned, "equip": equip}

@app.post("/shop/buy")
def shop_buy(data: dict = Body(...)):
    info = validate_init_data(data.get("init_data", ""))
    if not info: return {"status": "error", "reason": "auth"}
    uid = info["user"]["id"]; item = data.get("item")
    if item not in SHOP_PRICES or item == "astro": return {"status": "error", "reason": "bad_item"}
    price = SHOP_PRICES[item]
    conn = get_db_connection()
    conn.execute("INSERT OR IGNORE INTO users (user_id) VALUES (?)", (uid,))
    if get_owned(conn, uid).get(item): return {"status": "error", "reason": "owned"}
    if not try_spend(conn, uid, price):
        conn.commit()
        return {"status": "error", "reason": "no_funds"}
    owned = grant_item(conn, uid, item)
    conn.commit()
    bal = conn.execute("SELECT bal FROM users WHERE user_id = ?", (uid,)).fetchone()[0]
    return {"status": "ok", "bal": bal, "owned": owned}

@app.post("/daily/claim")
def daily_claim(data: dict = Body(...)):
    info = validate_init_data(data.get("init_data", ""))
    if not info: return {"status": "error", "reason": "auth"}
    uid = info["user"]["id"]; now = int(time.time())
    conn = get_db_connection()
    conn.execute("INSERT OR IGNORE INTO users (user_id) VALUES (?)", (uid,))
    cutoff = now - DAILY_COOLDOWN
    cur = conn.execute("UPDATE daily_state SET last_ts = ?, streak = streak + 1 WHERE user_id = ? AND last_ts <= ?", (now, uid, cutoff))
    if cur.rowcount != 1:
        row = conn.execute("SELECT last_ts, streak FROM daily_state WHERE user_id = ?", (uid,)).fetchone()
        if not row:
            conn.execute("INSERT INTO daily_state (user_id, last_ts, streak) VALUES (?, ?, 1)", (uid, now))
        else:
            last, streak = row
            wait = max(0, last + DAILY_COOLDOWN - now)
            conn.commit()
            return {"status": "error", "reason": "wait", "wait": wait}
    row = conn.execute("SELECT streak FROM daily_state WHERE user_id = ?", (uid,)).fetchone()
    streak = row[0] if row else 1
    stars = _rng.choices([1, 2, 3, 4, 5], weights=[30, 28, 22, 13, 7])[0]
    owned = get_owned(conn, uid)
    drop = None
    if _rng.random() < DAILY_DROP_CHANCE:
        pool = [i for i, p in ITEM_PRICE.items() if i not in EXCLUSIVE_PRICES and not i.startswith("c_") and p <= DAILY_DROP_MAX_PRICE and not owned.get(i)]
        if pool: drop = _rng.choice(pool)
    conn.execute("UPDATE users SET bal = bal + ? WHERE user_id = ?", (stars, uid))
    if drop: grant_item(conn, uid, drop)
    conn.commit()
    return {"status": "ok", "stars": stars, "drop": drop, "streak": streak, "wait": DAILY_COOLDOWN}

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
    return {"status": "registered", "need_msgs": REF_MIN_MSGS, "reward": REF_REWARD_INVITEE}

@app.post("/referral/status")
def referral_status(data: dict = Body(...)):
    info = validate_init_data(data.get("init_data", ""))
    if not info: return {"status": "error", "reason": "auth"}
    uid = info["user"]["id"]; now = int(time.time())
    conn = get_db_connection()
    refresh_activation(conn, "referrer_id", uid, now)
    refresh_activation(conn, "invitee_id", uid, now)
    invited = scalar(conn, "SELECT COUNT(*) FROM referrals WHERE referrer_id = ?", (uid,))
    active = scalar(conn, "SELECT COUNT(*) FROM referrals WHERE referrer_id = ? AND activated = 1", (uid,))
    pending_ref = scalar(conn, "SELECT COUNT(*) FROM referrals WHERE referrer_id = ? AND activated = 1 AND referrer_claimed = 0", (uid,)) * REF_REWARD_REFERRER
    me = None
    row = conn.execute("SELECT activated, invitee_claimed, created_at FROM referrals WHERE invitee_id = ?", (uid,)).fetchone()
    pending_me = 0
    if row:
        activated, claimed, created_at = row
        msgs = scalar(conn, "SELECT msgs FROM user_stats WHERE user_id = ?", (uid,))
        if claimed: me = {"state": "claimed"}
        elif activated: me = {"state": "ready"}; pending_me = REF_REWARD_INVITEE
        else: me = {"state": "waiting", "msgs": msgs, "need": REF_MIN_MSGS, "wait_sec": max(0, REF_MIN_AGE - (now - created_at))}
    return {"status": "ok", "link": make_link(uid), "invited": invited, "active": active, "limit": REF_MAX_TOTAL, "pending": pending_ref + pending_me, "me": me, "reward_referrer": REF_REWARD_REFERRER, "reward_invitee": REF_REWARD_INVITEE, "need_msgs": REF_MIN_MSGS}

@app.post("/referral/claim")
def referral_claim(data: dict = Body(...)):
    info = validate_init_data(data.get("init_data", ""))
    if not info: return {"status": "error", "reason": "auth"}
    uid = info["user"]["id"]; now = int(time.time())
    conn = get_db_connection()
    refresh_activation(conn, "referrer_id", uid, now)
    refresh_activation(conn, "invitee_id", uid, now)
    reward = 0
    rows = conn.execute("SELECT invitee_id FROM referrals WHERE referrer_id = ? AND activated = 1 AND referrer_claimed = 0", (uid,)).fetchall()
    for (invitee_id,) in rows:
        # условный UPDATE: награду получит только тот запрос, который реально переключил флаг
        cur = conn.execute("UPDATE referrals SET referrer_claimed = 1 WHERE invitee_id = ? AND referrer_id = ? AND activated = 1 AND referrer_claimed = 0", (invitee_id, uid))
        if cur.rowcount == 1:
            reward += REF_REWARD_REFERRER
    cur = conn.execute("UPDATE referrals SET invitee_claimed = 1 WHERE invitee_id = ? AND activated = 1 AND invitee_claimed = 0", (uid,))
    if cur.rowcount == 1:
        reward += REF_REWARD_INVITEE
    if reward > 0:
        conn.execute("INSERT OR IGNORE INTO users (user_id) VALUES (?)", (uid,))
        conn.execute("UPDATE users SET bal = bal + ? WHERE user_id = ?", (reward, uid))
    conn.commit()
    return {"status": "ok", "reward": reward}

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
        raw_text = str(data.get("text", "") or "").strip()[:MAX_TEXT_LEN]
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
        visual_mode = bool(VISUAL_RE.search(user_text))
        weight = ask_weight(att, visual_mode)
        if visual_mode:
            weight += VISUAL_EXTRA  # доплата за визуал берётся ДО генерации
        lv = data.get("last_visual")
        current_code = None
        if isinstance(lv, dict) and lv.get("type") == "html" and isinstance(lv.get("code"), str):
            # Проверяем длину И то, что это реально HTML-код, а не мусор
            if 400 <= len(lv["code"]) <= MAX_BUILD_LEN and is_valid_html(lv["code"]):
                current_code = lv["code"]
        edit_req = bool(data.get("edit_project"))
        if edit_req and not current_code:
            return {"text": "Не нашёл проект для правки. Нажми «Править проект» под нужным сайтом ещё раз.", "emotion": "sad", "pro": pro_pre}
        is_edit = edit_req and bool(current_code)
        build_mode = is_edit or wants_build(user_text)
        search_mode = (not att) and (not build_mode) and (not visual_mode) and wants_search(raw_text)
        if search_mode: weight = max(weight, SEARCH_WEIGHT)
        
        # длинная история (> 20 000 симв.): +10 лимитов к обычному чату.
        # Считаем по history_len (полная история от клиента): history_text обрезан до 12 000 и не может быть > 20 000.
        # Сборки Студии не трогаем: у них фиксированные цены 60/120/180/150, а в модель уходит лишь 4000 симв. истории.
        if not build_mode and history_len > HISTORY_LONG_CHARS:
            weight += HISTORY_LONG_EXTRA

        trial = False
        if build_mode:
            if is_edit:
                weight = BUILD_WEIGHT_EDIT  # 150 за правку (только по кнопке «Править проект»)
            elif COMPLEX_RE.search(user_text):
                weight = BUILD_WEIGHT_COMPLEX  # 180: игра, презентация, 3D
            elif SIMPLE_BUILD_RE.search(user_text):
                weight = BUILD_WEIGHT_SIMPLE   # 60: простой сайт, лендинг, визитка
            else:
                weight = BUILD_WEIGHT_MEDIUM   # 120: приложение, калькулятор, дашборд, бот
            
            # ЗАЩИТА БЕСПЛАТНИКОВ: если сложный проект, не даем делать его бесплатно
            if not pro_pre["active"]:
                if COMPLEX_RE.search(user_text):
                    return JSONResponse({"text": "Сложные проекты (игры, презентации, 3D) доступны на платных тарифах. На бесплатной пробной сборке можно сделать только простой сайт-визитку.", "emotion": "sleepy", "pro": pro_pre, "limit_reached": True}, status_code=402)
                
                # ЗАЩИТА: правки недоступны на бесплатной пробной сборке
                if is_edit:
                    return JSONResponse({"text": "Правки проектов доступны только на платных тарифах. Оформи подписку, чтобы вносить изменения.", "emotion": "sleepy", "pro": pro_pre, "limit_reached": True}, status_code=402)

                if trial_available(conn, uid):
                    trial = True
                    weight = 0
                else:
                    return JSONResponse({"text": "Бесплатную пробную сборку ты уже использовал. Дальше студия сайтов и приложений доступна на тарифах — выбери подходящий!", "emotion": "happy", "pro": pro_pre, "limit_reached": True}, status_code=402)
            elif pro_pre["left"] < weight:
                return JSONResponse({"text": f"На эту задачу нужно минимум {weight} единиц, а осталось {pro_pre['left']}. Пополни тариф.", "emotion": "sleepy", "pro": pro_pre}, status_code=402)
                
        if att:
            user_content, att_err = await asyncio.to_thread(build_user_content, raw_text, att, pro_pre["active"])
            if att_err:
                return {"text": att_err, "emotion": "sad", "pro": pro_info(conn, uid, now)}
        else:
            user_content = raw_text
            
        pro = None
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
                        msg = f"На этот запрос нужно {weight} единиц, а осталось {pro_now['left']}. Пополни тариф."
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
                if cur.rowcount != 1:
                    conn.commit()
                    return JSONResponse({"text": "Бесплатный лимит на сегодня закончился. Выбери тариф — и пиши без дневных ограничений.", "emotion": "sleepy", "pro": pro, "limit_reached": True}, status_code=402)
            conn.commit()
        finally:
            pass
        
        log.info("ask uid=%s weight=%s build=%s visual=%s search=%s history_len=%s", uid, weight, build_mode, visual_mode, search_mode, history_len)
        asyncio.get_running_loop().run_in_executor(None, count_message, uid, max(len(raw_text), MSG_MIN_LEN if att else 0))
        if build_mode:
            if trial:
                # занимаем пробную попытку атомарно: из N параллельных запросов пройдёт один
                cur = conn.execute("INSERT OR IGNORE INTO trial_build (user_id, used_at) VALUES (?, ?)", (uid, int(time.time())))
                conn.commit()
                if cur.rowcount != 1:
                    return JSONResponse({"text": "Бесплатную пробную сборку ты уже использовал. Выбери тариф, чтобы продолжить.", "emotion": "happy", "pro": pro, "limit_reached": True}, status_code=402)
            if data.get("stream"):
                return StreamingResponse(
                    run_build_stream(conn, uid, user_content, history_text, current_code if is_edit else None, weight, trial),
                    media_type="application/x-ndjson",
                    headers={"Cache-Control": "no-cache, no-transform", "X-Accel-Buffering": "no"})
            res = await run_build(conn, uid, user_content, history_text, current_code if is_edit else None, weight)
            if trial:
                if res.get("visual"):
                    res["text"] = ((res.get("text") or "Готово!") + " Это была бесплатная пробная сборка — дальше студия доступна на тарифах.").strip()
                else:
                    conn.execute("DELETE FROM trial_build WHERE user_id = ?", (uid,))
                    conn.commit()
            return res
        
        refund_pending = (weight, bool(pro and pro.get("active")))
            
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
            if ctx: messages.append({"role": "user", "content": ctx})
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
            if refund_pending[1]: pro = pro_info(conn, uid, int(time.time()))
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
                else:
                    conn.execute("UPDATE rate_state SET ask_count = MAX(ask_count - ?, 0) WHERE user_id = ?", (VISUAL_EXTRA, uid))
                    conn.commit()
            except Exception:
                log.exception("visual refund failed")
        return {"text": str(result.get("text") or "Что-то я задумался...").strip(), "emotion": emotion, "visual": visual, "pro": pro}
    except BannedError:
        raise
    except Exception:
        log.exception("ask failed")
        if refund_pending:
            refund_ask(get_db_connection(), uid, *refund_pending)
        return {"text": "Не получилось ответить. Попробуй ещё раз через пару секунд.", "emotion": "sad"}

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
    payload = {"title": f"Holo Friend «{plan['title']}» — {plan['days']} дней", "description": f"{plan['units']} единиц запросов ({plan['units'] // BUILD_WEIGHT} сборок сайтов и приложений), без дневных лимитов", "payload": f"{uid}|plan_{plan_id}", "currency": "XTR", "provider_token": "", "prices": [{"label": f"Тариф «{plan['title']}»", "amount": plan["price"]}]}
    r = await tg_call("createInvoiceLink", payload)
    res = r.json()
    if res.get("ok"): return {"link": res["result"]}
    return {"error": "Не удалось создать счет", "details": res}

@app.post("/clear")
async def clear(request: Request):
    return {"status": "ok"}

def check_payment(from_id, payload, currency, amount):
    try: p_uid, item = str(payload).split("|", 1); p_uid = int(p_uid)
    except (ValueError, TypeError): return False, "Неверные данные платежа"
    if p_uid != from_id or currency != "XTR": return False, "Платёж не прошёл проверку"
    if item == "pro" or item.startswith("plan_"): return (stars_price(item) == amount), ""
    if stars_price(item) != amount: return False, "Платёж не прошёл проверку"
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
