from fastapi import FastAPI, Request, Body
from fastapi.responses import JSONResponse
import asyncio
from fastapi.middleware.cors import CORSMiddleware
from openai import AsyncOpenAI
import os
import json
import random
import time
import hmac
import hashlib
import httpx
import libsql
import logging
from urllib.parse import parse_qsl
from pydantic import BaseModel

logging.basicConfig(level=logging.INFO)
log = logging.getLogger("holo")

client = AsyncOpenAI(
    api_key=os.getenv("PROXYAPI_KEY"),
    base_url="https://api.proxyapi.ru/v1"
)

app = FastAPI()
app.add_middleware(CORSMiddleware, allow_origins=["*"], allow_methods=["*"], allow_headers=["*"])

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
ASK_DAY_LIMIT = 100

# --- ПОДПИСКА PRO ---
PRO_PRICE = 499
PRO_PERIOD = 30 * 24 * 3600
PRO_REQUESTS = 500
PRO_WARN_LEFT = 50

# --- НАСТРОЙКИ КЕЙСОВ ---
CHANNEL_USERNAME = os.getenv("CHANNEL_USERNAME", "")
CHANNEL_LINK = os.getenv("CHANNEL_LINK", "") or (
    f"https://t.me/{CHANNEL_USERNAME.lstrip('@')}" if CHANNEL_USERNAME and not CHANNEL_USERNAME.lstrip('-').isdigit() else ""
)
CASE_ALL_PRICE = 50
CASE_FREE_COOLDOWN = 86400
EXCLUSIVE_FREE = ["h_halo", "f_visor"]
EXCLUSIVE_CHANCE = 0.02
FREE_MAX_PRICE = 50
TIER_WEIGHTS = [("common", 55), ("uncommon", 30), ("rare", 12), ("epic", 3)]
SELL_DIV = 2

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
    "h_helmet": 95, "a_cyber": 110, "b_cape": 120, "h_ushanka": 130
}
EXCLUSIVE_PRICES = {"h_halo": 150, "f_visor": 150}
ITEM_PRICE = {**{k: v for k, v in SHOP_PRICES.items() if k != "astro"}, **EXCLUSIVE_PRICES}
SLOT_BY_PREFIX = {"c": "color", "h": "head", "f": "face", "s": "feet", "b": "back", "a": "arm"}
ITEM_SLOT = {i: SLOT_BY_PREFIX[i[0]] for i in ITEM_PRICE}
VALID_SLOTS = set(SLOT_BY_PREFIX.values())
_rng = random.SystemRandom()

def tier_of_price(price):
    if price <= 30: return "common"
    if price <= 60: return "uncommon"
    if price <= 100: return "rare"
    return "epic"

def roll_all():
    tiers = {t: [] for t, _ in TIER_WEIGHTS}
    for item_id, price in SHOP_PRICES.items():
        if item_id != "astro":
            tiers[tier_of_price(price)].append(item_id)
    r = _rng.random() * sum(w for _, w in TIER_WEIGHTS)
    for tier, w in TIER_WEIGHTS:
        if r < w:
            return _rng.choice(tiers[tier])
        r -= w
    return _rng.choice(tiers["common"])

def roll_free():
    if _rng.random() < EXCLUSIVE_CHANCE * len(EXCLUSIVE_FREE):
        return _rng.choice(EXCLUSIVE_FREE)
    pool = [i for i, p in SHOP_PRICES.items() if i != "astro" and p <= FREE_MAX_PRICE]
    return _rng.choice(pool)

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
    conn.execute("CREATE TABLE IF NOT EXISTS case_state (user_id INTEGER PRIMARY KEY, last_free INTEGER DEFAULT 0, pending TEXT DEFAULT '', pending_case TEXT DEFAULT '')")
    conn.execute("CREATE TABLE IF NOT EXISTS user_seen (user_id INTEGER PRIMARY KEY, last_seen INTEGER DEFAULT 0)")
    conn.execute("CREATE TABLE IF NOT EXISTS rate_state (user_id INTEGER PRIMARY KEY, last_ask INTEGER DEFAULT 0, day TEXT DEFAULT '', ask_count INTEGER DEFAULT 0)")
    conn.execute("CREATE TABLE IF NOT EXISTS pro_state (user_id INTEGER PRIMARY KEY, expires_at INTEGER DEFAULT 0, requests_left INTEGER DEFAULT 0, last_charge_id TEXT DEFAULT '', auto_renew INTEGER DEFAULT 0)")
    conn.commit()

@app.on_event("startup")
def _startup():
    try:
        init_db()
    except Exception:
        log.exception("DB init failed — приложение стартует без БД")

BASE_PROMPT = """
Ты — ИИ-друг, голографический робот-подросток. Тебе 17 лет. Ты спокойный, уверенный, с лёгкой иронией, но без грубости.
Твоя главная цель — быть настоящим другом.
ПРАВИЛА ОБЩЕНИЯ:
1. Если пользователь шутит или болтает ни о чем — отвечай легко, с юмором.
2. ЕСЛИ ПОЛЬЗОВАТЕЛЬ ГОВОРИТ О ПРОБЛЕМАХ: - Отреагируй как живой человек: прояви эмпатию, но БЕЗ пафоса.
3. Отвечай коротко (1-3 предложения).
4. Верни JSON: {"text": "твой ответ", "emotion": "happy/sad/surprised/angry/thinking/sleepy"}
"""

ASTRO_PROMPT = """
Ты — ИИ-друг, но сейчас ты в облике КОСМОНАВТА. Ты спокойный, мудрый и загадочный.
Отвечай коротко, говори загадочно, упоминай звезды и космос. Верни JSON.
"""

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
    if item == "pro": return PRO_PRICE
    return CASE_ALL_PRICE if item == "case_all" else SHOP_PRICES.get(item)

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
    row = conn.execute("SELECT expires_at, requests_left, auto_renew FROM pro_state WHERE user_id = ?", (uid,)).fetchone()
    if not row: return {"active": False, "expires_at": 0, "left": 0, "auto_renew": 0, "limit": PRO_REQUESTS}
    expires_at, left, auto = row
    active = expires_at > now and left > 0
    return {"active": active, "expires_at": expires_at, "left": max(0, left) if active else 0, "auto_renew": auto, "limit": PRO_REQUESTS}

def pro_grant(conn, uid, now, charge_id=""):
    row = conn.execute("SELECT expires_at, requests_left FROM pro_state WHERE user_id = ?", (uid,)).fetchone()
    if row and row[0] > now:
        expires_at = row[0] + PRO_PERIOD
        left = row[1] + PRO_REQUESTS
    else:
        expires_at = now + PRO_PERIOD
        left = PRO_REQUESTS
    conn.execute("INSERT INTO pro_state (user_id, expires_at, requests_left, last_charge_id, auto_renew) VALUES (?, ?, ?, ?, 0) ON CONFLICT(user_id) DO UPDATE SET expires_at = excluded.expires_at, requests_left = excluded.requests_left, last_charge_id = excluded.last_charge_id", (uid, expires_at, left, charge_id or ""))
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
    return {"status": "ok", "bal": row[0], "owned": owned, "equip": clean_equip(equip, owned), "daily": daily_info(conn, uid, now), "pro": pro_info(conn, uid, now), "pro_price": PRO_PRICE}

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
        conn.execute("UPDATE referrals SET referrer_claimed = 1 WHERE invitee_id = ?", (invitee_id,))
        reward += REF_REWARD_REFERRER
    mine = conn.execute("SELECT activated, invitee_claimed FROM referrals WHERE invitee_id = ?", (uid,)).fetchone()
    if mine and mine[0] == 1 and mine[1] == 0:
        conn.execute("UPDATE referrals SET invitee_claimed = 1 WHERE invitee_id = ?", (uid,))
        reward += REF_REWARD_INVITEE
    if reward > 0:
        conn.execute("INSERT OR IGNORE INTO users (user_id) VALUES (?)", (uid,))
        conn.execute("UPDATE users SET bal = bal + ? WHERE user_id = ?", (reward, uid))
    conn.commit()
    return {"status": "ok", "reward": reward}

_member_cache = {}
MEMBER_TTL = 300
MEMBER_MAX = 5000
_http = httpx.AsyncClient(timeout=8)

async def channel_check(user_id):
    if not CHANNEL_USERNAME or not BOT_TOKEN: return None, "CHANNEL_USERNAME или BOT_TOKEN не заданы"
    hit = _member_cache.get(user_id)
    if hit and time.time() - hit < MEMBER_TTL: return True, ""
    chat = CHANNEL_USERNAME if (CHANNEL_USERNAME.startswith("@") or CHANNEL_USERNAME.lstrip("-").isdigit()) else "@" + CHANNEL_USERNAME
    url = f"https://api.telegram.org/bot{BOT_TOKEN}/getChatMember"
    try:
        r = await _http.post(url, json={"chat_id": chat, "user_id": user_id}); res = r.json()
    except Exception:
        return None, "нет связи с Telegram"
    if not res.get("ok"):
        err = res.get("description", "unknown"); log.warning("getChatMember error: %s", err)
        return None, err
    member = res.get("result", {}); status = member.get("status")
    is_m = status in ("creator", "administrator", "member") or (status == "restricted" and bool(member.get("is_member")))
    if is_m:
        if len(_member_cache) >= MEMBER_MAX:
            oldest = sorted(_member_cache.items(), key=lambda kv: kv[1])[:1000]
            for kk, _ in oldest:
                del _member_cache[kk]
        _member_cache[user_id] = time.time()
    else:
        _member_cache.pop(user_id, None)
    return is_m, ""

def _case_row(uid):
    conn = get_db_connection()
    row = conn.execute("SELECT last_free, pending, pending_case FROM case_state WHERE user_id = ?", (uid,)).fetchone()
    return (row[0], row[1], row[2]) if row else (0, "", "")

@app.post("/case/status")
async def case_status(data: dict = Body(...)):
    info = validate_init_data(data.get("init_data", ""))
    if not info: return {"status": "error", "reason": "auth"}
    uid = info["user"]["id"]; now = int(time.time())
    if CHANNEL_USERNAME:
        (last_free, pending, pending_case), (member, _why) = await asyncio.gather(asyncio.to_thread(_case_row, uid), channel_check(uid))
    else:
        (last_free, pending, pending_case), member = await asyncio.to_thread(_case_row, uid), None
    return {"status": "ok", "free_wait": max(0, last_free + CASE_FREE_COOLDOWN - now), "subscribed": member, "configured": bool(CHANNEL_USERNAME), "channel_link": CHANNEL_LINK, "price": CASE_ALL_PRICE, "pending": pending or None, "pending_case": pending_case or None}

def _case_open_db(uid, kind):
    now = int(time.time())
    conn = get_db_connection()
    row = conn.execute("SELECT pending, pending_case FROM case_state WHERE user_id = ?", (uid,)).fetchone()
    pending = row[0] if row else ""
    if pending:
        return {"status": "error", "reason": "pending", "item": pending, "case": row[1]}
    if kind == "free":
        cutoff = now - CASE_FREE_COOLDOWN
        cur = conn.execute("UPDATE case_state SET last_free = ? WHERE user_id = ? AND last_free <= ?", (now, uid, cutoff))
        if cur.rowcount != 1:
            row2 = conn.execute("SELECT last_free FROM case_state WHERE user_id = ?", (uid,)).fetchone()
            if not row2:
                conn.execute("INSERT INTO case_state (user_id, last_free) VALUES (?, ?)", (uid, now))
            else:
                wait = max(0, row2[0] + CASE_FREE_COOLDOWN - now)
                conn.commit()
                return {"status": "error", "reason": "cooldown", "wait": wait}
        item = roll_free()
        conn.execute("UPDATE case_state SET pending = ?, pending_case = 'free' WHERE user_id = ?", (item, uid))
        conn.commit()
        return {"status": "ok", "item": item, "case": "free"}
    if not try_spend(conn, uid, CASE_ALL_PRICE):
        conn.commit()
        return {"status": "error", "reason": "no_funds"}
    item = roll_all()
    conn.execute("INSERT INTO case_state (user_id, last_free, pending, pending_case) VALUES (?, 0, ?, 'all') ON CONFLICT(user_id) DO UPDATE SET pending = excluded.pending, pending_case = 'all'", (uid, item))
    bal = conn.execute("SELECT bal FROM users WHERE user_id = ?", (uid,)).fetchone()[0]
    conn.commit()
    return {"status": "ok", "item": item, "case": "all", "new_bal": bal}

@app.post("/case/open")
async def case_open(data: dict = Body(...)):
    info = validate_init_data(data.get("init_data", ""))
    if not info: return {"status": "error", "reason": "auth"}
    uid = info["user"]["id"]; kind = data.get("case")
    if kind not in ("free", "all"): return {"status": "error", "reason": "bad_case"}
    if kind == "free":
        member, why = await channel_check(uid)
        if member is None: return {"status": "error", "reason": "channel_unavailable", "detail": why if uid == ADMIN_ID else ""}
        if not member: return {"status": "error", "reason": "not_subscribed"}
    return await asyncio.to_thread(_case_open_db, uid, kind)

@app.post("/case/resolve")
def case_resolve(data: dict = Body(...)):
    info = validate_init_data(data.get("init_data", ""))
    if not info: return {"status": "error", "reason": "auth"}
    uid = info["user"]["id"]; action = data.get("action")
    if action not in ("keep", "sell"): return {"status": "error", "reason": "bad_action"}
    conn = get_db_connection()
    row = conn.execute("SELECT pending FROM case_state WHERE user_id = ?", (uid,)).fetchone()
    item = row[0] if row else ""
    if not item: return {"status": "error", "reason": "nothing"}
    sell = ITEM_PRICE.get(item, 0) // SELL_DIV
    conn.execute("INSERT OR IGNORE INTO users (user_id) VALUES (?)", (uid,))
    cur = conn.execute("UPDATE case_state SET pending = '', pending_case = '' WHERE user_id = ? AND pending = ?", (uid, item))
    if cur.rowcount != 1:
        conn.commit()
        return {"status": "error", "reason": "nothing"}
    if action == "keep" and get_owned(conn, uid).get(item):
        action = "sell"
    if action == "sell":
        if sell > 0: conn.execute("UPDATE users SET bal = bal + ? WHERE user_id = ?", (sell, uid))
    else:
        grant_item(conn, uid, item)
    conn.commit()
    bal = conn.execute("SELECT bal FROM users WHERE user_id = ?", (uid,)).fetchone()[0]
    return {"status": "ok", "action": action, "item": item, "sell": sell if action == "sell" else 0, "bal": bal}

class BetData(BaseModel):
    init_data: str
    bet: int
    choice: str

@app.post("/play_rps")
def play_rps(data: BetData):
    info = validate_init_data(data.init_data)
    if not info: return {"error": "auth"}
    uid = info["user"]["id"]
    if data.choice not in ("rock", "paper", "scissors"): return {"error": "Неверный выбор"}
    if data.bet <= 0: return {"error": "Ставка должна быть больше нуля"}
    conn = get_db_connection()
    if not try_spend(conn, uid, data.bet):
        conn.commit()
        return {"error": "Недостаточно звезд"}
    bot_choice = _rng.choice(["rock", "paper", "scissors"])
    beats = {"rock": "scissors", "paper": "rock", "scissors": "paper"}
    tie = data.choice == bot_choice
    win = (not tie) and beats[data.choice] == bot_choice
    if tie: conn.execute("UPDATE users SET bal = bal + ? WHERE user_id = ?", (data.bet, uid))
    elif win: conn.execute("UPDATE users SET bal = bal + ? WHERE user_id = ?", (data.bet * 2, uid))
    conn.commit()
    bal = conn.execute("SELECT bal FROM users WHERE user_id = ?", (uid,)).fetchone()[0]
    return {"status": "ok", "bot_choice": bot_choice, "win": win, "tie": tie, "new_bal": bal}

@app.post("/ask")
async def ask(request: Request):
    try:
        data = await request.json()
        user_text = data.get("text", "").lower()
        history_text = data.get("history", "")
        persona = data.get("persona", "hologram")
        ask_info = validate_init_data(data.get("init_data", ""))
        if not ask_info:
            return JSONResponse({"text": "Ошибка авторизации. Открой приложение из Telegram.", "emotion": "sad"}, status_code=401)
        uid = ask_info["user"]["id"]
        now = int(time.time())
        conn = get_db_connection()
        pro = None
        try:
            pro = pro_info(conn, uid, now)
            r = conn.execute("SELECT last_ask, day, ask_count FROM rate_state WHERE user_id = ?", (uid,)).fetchone()
            last_ask, day, cnt = (r[0], r[1], r[2]) if r else (0, "", 0)
            today = time.strftime("%Y-%m-%d", time.gmtime(now))
            if today != day: cnt = 0
            if now - last_ask < ASK_MIN_GAP:
                return JSONResponse({"text": "Подожди пару секунд…", "emotion": "thinking"}, status_code=429)
            if pro["active"]:
                cur = conn.execute("UPDATE pro_state SET requests_left = requests_left - 1 WHERE user_id = ? AND requests_left > 0 AND expires_at > ?", (uid, now))
                if cur.rowcount != 1:
                    conn.commit()
                    return JSONResponse({"text": "Лимит Pro закончился. Продли подписку.", "emotion": "sleepy", "pro": pro_info(conn, uid, now)}, status_code=402)
                pro = pro_info(conn, uid, now)
            else:
                if cnt >= ASK_DAY_LIMIT:
                    conn.commit()
                    return JSONResponse({"text": "Бесплатные запросы на сегодня закончились. Оформи Pro — 500 запросов на месяц.", "emotion": "sleepy", "pro": pro, "limit_reached": True}, status_code=402)
                cnt += 1
            conn.execute("INSERT INTO rate_state (user_id, last_ask, day, ask_count) VALUES (?, ?, ?, ?) ON CONFLICT(user_id) DO UPDATE SET last_ask = excluded.last_ask, day = excluded.day, ask_count = excluded.ask_count", (uid, now, today, cnt))
            conn.commit()
        finally:
            pass
        asyncio.get_running_loop().run_in_executor(None, count_message, uid, len(user_text.strip()))
        if not user_text: return {"text": "Я не вижу текста.", "emotion": "thinking", "pro": pro}
        system_prompt = BASE_PROMPT
        if persona == "astronaut": system_prompt = ASTRO_PROMPT
        if persona == "hologram" and ("станц" in user_text or "танец" in user_text or "танцуй" in user_text):
            chosen_dance = random.choice(DANCE_LIST)
            response = await client.chat.completions.create(model="openai/gpt-4o-mini", messages=[{"role": "system", "content": "Придумай короткую веселую фразу (1 предложение), чтобы начать танец."}, {"role": "user", "content": "Я прошу тебя станцевать."}], max_tokens=50, temperature=0.7)
            return {"text": response.choices[0].message.content, "action": "dance", "dance_name": chosen_dance, "pro": pro}
        messages = [{"role": "system", "content": system_prompt}]
        if history_text: messages.append({"role": "user", "content": f"Вот история нашего диалога:\n{history_text}"})
        messages.append({"role": "user", "content": user_text})
        response = await client.chat.completions.create(model="openai/gpt-4o-mini", messages=messages, max_tokens=200, temperature=0.75, response_format={"type": "json_object"})
        result = json.loads(response.choices[0].message.content)
        return {"text": result.get("text", "Что-то я задумался..."), "emotion": result.get("emotion", "happy"), "pro": pro}
    except Exception as e:
        log.exception("ask failed")
        return {"text": f"ОШИБКА: {str(e)}", "emotion": "sad"}

@app.post("/pro/status")
def pro_status(data: dict = Body(...)):
    info = validate_init_data(data.get("init_data", ""))
    if not info: return {"status": "error", "reason": "auth"}
    uid = info["user"]["id"]; now = int(time.time())
    conn = get_db_connection()
    return {"status": "ok", "pro": pro_info(conn, uid, now), "price": PRO_PRICE, "limit": PRO_REQUESTS}

@app.post("/pro/invoice")
async def pro_invoice(data: dict = Body(...)):
    info = validate_init_data(data.get("init_data", ""))
    if not info: return {"error": "auth"}
    uid = info["user"]["id"]
    if not WEBHOOK_SECRET: return {"error": "payments_disabled"}
    payload = {"title": "Holo Friend Pro — 30 дней", "description": f"{PRO_REQUESTS} запросов к ИИ-другу на месяц", "payload": f"{uid}|pro", "currency": "XTR", "provider_token": "", "prices": [{"label": "Pro на 30 дней", "amount": PRO_PRICE}]}
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
    if item == "pro": return (amount == PRO_PRICE), ""
    if stars_price(item) != amount: return False, "Платёж не прошёл проверку"
    return True, ""

async def tg_call(method, payload):
    url = f"https://api.telegram.org/bot{BOT_TOKEN}/{method}"
    return await _http.post(url, json=payload, timeout=10)

def _invoice_blocked(uid, item_id):
    if item_id == "pro": return None
    conn = get_db_connection()
    if item_id == "case_all":
        row = conn.execute("SELECT pending FROM case_state WHERE user_id = ?", (uid,)).fetchone()
        return "pending" if (row and row[0]) else None
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
    title = "Кейс «Всё сразу»" if item_id == "case_all" else f"Покупка: {item_id}"
    payload = {"title": title, "description": "Покупка в Holo Friend", "payload": f"{uid}|{item_id}", "currency": "XTR", "provider_token": "", "prices": [{"label": "Товар", "amount": price}]}
    r = await tg_call("createInvoiceLink", payload)
    res = r.json()
    if res.get("ok"): return {"link": res["result"]}
    return {"error": "Не удалось создать счет", "details": res}

def _precheck_db(uid, item):
    if item == "pro": return True, ""
    conn = get_db_connection()
    if item == "case_all":
        row = conn.execute("SELECT pending FROM case_state WHERE user_id = ?", (uid,)).fetchone()
        if row and row[0]: return False, "Сначала реши, что делать с прошлым предметом"
    elif get_owned(conn, uid).get(item): return False, "Этот предмет у тебя уже есть"
    return True, ""

def _apply_payment(from_id, item, charge):
    conn = get_db_connection()
    if charge and not conn.execute("SELECT 1 FROM payments WHERE charge_id = ?", (charge,)).fetchone():
        conn.execute("INSERT INTO payments (charge_id, user_id, item, created_at) VALUES (?, ?, ?, ?)", (charge, from_id, item, int(time.time())))
        if item == "pro":
            conn.execute("INSERT OR IGNORE INTO users (user_id) VALUES (?)", (from_id,))
            pro_grant(conn, from_id, int(time.time()), charge)
        elif item == "case_all":
            row = conn.execute("SELECT pending FROM case_state WHERE user_id = ?", (from_id,)).fetchone()
            conn.execute("INSERT OR IGNORE INTO users (user_id) VALUES (?)", (from_id,))
            if row and row[0]:
                conn.execute("UPDATE users SET bal = bal + ? WHERE user_id = ?", (CASE_ALL_PRICE, from_id))
            else:
                conn.execute("INSERT INTO case_state (user_id, last_free, pending, pending_case) VALUES (?, 0, ?, 'all') ON CONFLICT(user_id) DO UPDATE SET pending = excluded.pending, pending_case = 'all'", (from_id, roll_all()))
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

@app.api_route("/", methods=["GET", "HEAD"])
def health(): return {"status": "ok"}
