from fastapi import FastAPI, Request
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
from urllib.parse import parse_qsl
from pydantic import BaseModel

client = AsyncOpenAI(
    api_key=os.getenv("PROXYAPI_KEY"),
    base_url="https://api.proxyapi.ru/v1"
)

app = FastAPI()
app.add_middleware(CORSMiddleware, allow_origins=["*"], allow_methods=["*"], allow_headers=["*"])

BOT_TOKEN = os.getenv("BOT_TOKEN")
DANCE_LIST = ["disco", "dab", "floss", "robot", "party", "twist"]

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
MSG_MIN_LEN = 3
MSG_MIN_GAP = 8
INIT_DATA_MAX_AGE = 86400

# --- НАСТРОЙКИ КЕЙСОВ ---
CHANNEL_USERNAME = os.getenv("CHANNEL_USERNAME", "")
CHANNEL_LINK = os.getenv("CHANNEL_LINK", "") or (
    f"https://t.me/{CHANNEL_USERNAME.lstrip('@')}" if CHANNEL_USERNAME and not CHANNEL_USERNAME.lstrip('-').isdigit() else ""
)
CASE_ALL_PRICE = 50           # Цена платного кейса
CASE_FREE_COOLDOWN = 86400    # 24 часа
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
    conn.execute("""
        CREATE TABLE IF NOT EXISTS users (
            user_id INTEGER PRIMARY KEY,
            bal INTEGER DEFAULT 0,
            owned TEXT DEFAULT '{}',
            equip TEXT DEFAULT '{}'
        )
    """)
    conn.execute("""
        CREATE TABLE IF NOT EXISTS user_meta (
            user_id INTEGER PRIMARY KEY,
            first_seen INTEGER NOT NULL
        )
    """)
    conn.execute("INSERT OR IGNORE INTO user_meta (user_id, first_seen) SELECT user_id, 0 FROM users")
    conn.execute("""
        CREATE TABLE IF NOT EXISTS user_stats (
            user_id INTEGER PRIMARY KEY,
            msgs INTEGER DEFAULT 0,
            last_ts INTEGER DEFAULT 0
        )
    """)
    conn.execute("""
        CREATE TABLE IF NOT EXISTS referrals (
            invitee_id INTEGER PRIMARY KEY,
            referrer_id INTEGER NOT NULL,
            created_at INTEGER NOT NULL,
            activated INTEGER DEFAULT 0,
            activated_at INTEGER DEFAULT 0,
            referrer_claimed INTEGER DEFAULT 0,
            invitee_claimed INTEGER DEFAULT 0
        )
    """)
    conn.execute("CREATE INDEX IF NOT EXISTS idx_ref_referrer ON referrals (referrer_id)")
    conn.execute("""
        CREATE TABLE IF NOT EXISTS daily_state (
            user_id INTEGER PRIMARY KEY,
            last_ts INTEGER DEFAULT 0,
            streak INTEGER DEFAULT 0
        )
    """)
    conn.execute("""
        CREATE TABLE IF NOT EXISTS earn_state (
            user_id INTEGER PRIMARY KEY,
            day TEXT DEFAULT '',
            earned INTEGER DEFAULT 0
        )
    """)
    conn.execute("""
        CREATE TABLE IF NOT EXISTS payments (
            charge_id TEXT PRIMARY KEY,
            user_id INTEGER NOT NULL,
            item TEXT NOT NULL,
            created_at INTEGER NOT NULL
        )
    """)
    conn.execute("""
        CREATE TABLE IF NOT EXISTS case_state (
            user_id INTEGER PRIMARY KEY,
            last_free INTEGER DEFAULT 0,
            pending TEXT DEFAULT '',
            pending_case TEXT DEFAULT ''
        )
    """)
    conn.commit()
    conn.close()

init_db()

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

def touch_meta(conn, user_id, now=None):
    conn.execute("INSERT OR IGNORE INTO user_meta (user_id, first_seen) VALUES (?, ?)", (user_id, int(now if now is not None else time.time())))

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
        if msgs >= REF_MIN_MSGS and now - created_at >= REF_MIN_AGE:
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
        if not row: conn.execute("INSERT INTO user_stats (user_id, msgs, last_ts) VALUES (?, 1, ?)", (uid, now))
        elif now - row[1] >= MSG_MIN_GAP: conn.execute("UPDATE user_stats SET msgs = msgs + 1, last_ts = ? WHERE user_id = ?", (now, uid))
        conn.commit(); conn.close()
    except Exception: pass

def get_owned(conn, uid):
    row = conn.execute("SELECT owned FROM users WHERE user_id = ?", (uid,)).fetchone()
    try: owned = json.loads(row[0]) if row and row[0] else {}
    except ValueError: owned = {}
    return owned if isinstance(owned, dict) else {}

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

@app.post("/load_progress")
async def load_progress(request: Request):
    data = await request.json()
    info = validate_init_data(data.get("init_data", ""))
    if not info: return {"status": "error", "reason": "auth"}
    uid = info["user"]["id"]
    now = int(time.time())
    conn = get_db_connection()
    try:
        conn.execute("INSERT OR IGNORE INTO users (user_id) VALUES (?)", (uid,))
        touch_meta(conn, uid, now)
        row = conn.execute("SELECT bal, equip FROM users WHERE user_id = ?", (uid,)).fetchone()
        owned = get_owned(conn, uid)
        try: equip = json.loads(row[1]) if row[1] else {}
        except ValueError: equip = {}
        conn.commit()
        return {"status": "ok", "bal": row[0], "owned": owned, "equip": clean_equip(equip, owned), "daily": daily_info(conn, uid, now)}
    finally: conn.close()

@app.post("/save_progress")
async def save_progress(request: Request):
    data = await request.json()
    info = validate_init_data(data.get("init_data", ""))
    if not info: return {"status": "error", "reason": "auth"}
    uid = info["user"]["id"]
    now = int(time.time())
    conn = get_db_connection()
    try:
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
                conn.execute("""INSERT INTO earn_state (user_id, day, earned) VALUES (?, ?, ?) ON CONFLICT(user_id) DO UPDATE SET day = excluded.day, earned = excluded.earned""", (uid, day, earned_today + accepted))
        equip = clean_equip(data.get("equip", {}), owned)
        conn.execute("UPDATE users SET equip = ? WHERE user_id = ?", (json.dumps(equip), uid))
        conn.commit()
        return {"status": "ok", "bal": server_bal + accepted, "owned": owned, "equip": equip}
    finally: conn.close()

@app.post("/shop/buy")
async def shop_buy(request: Request):
    data = await request.json()
    info = validate_init_data(data.get("init_data", ""))
    if not info: return {"status": "error", "reason": "auth"}
    uid = info["user"]["id"]
    item = data.get("item")
    if item not in SHOP_PRICES or item == "astro": return {"status": "error", "reason": "bad_item"}
    price = SHOP_PRICES[item]
    conn = get_db_connection()
    try:
        conn.execute("INSERT OR IGNORE INTO users (user_id) VALUES (?)", (uid,))
        if get_owned(conn, uid).get(item): return {"status": "error", "reason": "owned"}
        bal = conn.execute("SELECT bal FROM users WHERE user_id = ?", (uid,)).fetchone()[0]
        if bal < price:
            conn.commit()
            return {"status": "error", "reason": "no_funds"}
        conn.execute("UPDATE users SET bal = bal - ? WHERE user_id = ?", (price, uid))
        owned = grant_item(conn, uid, item)
        conn.commit()
        return {"status": "ok", "bal": bal - price, "owned": owned}
    finally: conn.close()

@app.post("/daily/claim")
async def daily_claim(request: Request):
    data = await request.json()
    info = validate_init_data(data.get("init_data", ""))
    if not info: return {"status": "error", "reason": "auth"}
    uid = info["user"]["id"]
    now = int(time.time())
    conn = get_db_connection()
    try:
        conn.execute("INSERT OR IGNORE INTO users (user_id) VALUES (?)", (uid,))
        row = conn.execute("SELECT last_ts, streak FROM daily_state WHERE user_id = ?", (uid,)).fetchone()
        last, streak = (row[0], row[1]) if row else (0, 0)
        if now - last < DAILY_COOLDOWN: return {"status": "error", "reason": "wait", "wait": DAILY_COOLDOWN - (now - last)}
        streak = streak + 1 if last and now - last < DAILY_STREAK_KEEP else 1
        stars = _rng.choices([1, 2, 3, 4, 5], weights=[30, 28, 22, 13, 7])[0]
        owned = get_owned(conn, uid)
        drop = None
        if _rng.random() < DAILY_DROP_CHANCE:
            pool = [i for i, p in ITEM_PRICE.items() if i not in EXCLUSIVE_PRICES and not i.startswith("c_") and p <= DAILY_DROP_MAX_PRICE and not owned.get(i)]
            if pool: drop = _rng.choice(pool)
        conn.execute("""INSERT INTO daily_state (user_id, last_ts, streak) VALUES (?, ?, ?) ON CONFLICT(user_id) DO UPDATE SET last_ts = excluded.last_ts, streak = excluded.streak""", (uid, now, streak))
        conn.execute("UPDATE users SET bal = bal + ? WHERE user_id = ?", (stars, uid))
        if drop: grant_item(conn, uid, drop)
        conn.commit()
        return {"status": "ok", "stars": stars, "drop": drop, "streak": streak, "wait": DAILY_COOLDOWN}
    finally: conn.close()

@app.post("/referral/register")
async def referral_register(request: Request):
    data = await request.json()
    info = validate_init_data(data.get("init_data", ""))
    if not info: return {"status": "error", "reason": "auth"}
    user = info["user"]; uid = user["id"]; param = info["start_param"] or ""
    if not param.startswith("ref_") or not param[4:].isdigit(): return {"status": "ignored", "reason": "no_ref"}
    referrer_id = int(param[4:])
    if user.get("is_bot") or referrer_id == uid: return {"status": "ignored", "reason": "self"}
    now = int(time.time())
    conn = get_db_connection()
    try:
        if conn.execute("SELECT 1 FROM referrals WHERE invitee_id = ?", (uid,)).fetchone(): return {"status": "ignored", "reason": "already"}
        meta = conn.execute("SELECT first_seen FROM user_meta WHERE user_id = ?", (uid,)).fetchone()
        if meta is None: touch_meta(conn, uid, now)
        elif now - meta[0] > REF_NEW_WINDOW: return {"status": "ignored", "reason": "not_new"}
        if not conn.execute("SELECT 1 FROM users WHERE user_id = ?", (referrer_id,)).fetchone(): return {"status": "ignored", "reason": "bad_referrer"}
        if scalar(conn, "SELECT COUNT(*) FROM referrals WHERE referrer_id = ?", (referrer_id,)) >= REF_MAX_TOTAL: return {"status": "ignored", "reason": "limit"}
        if scalar(conn, "SELECT COUNT(*) FROM referrals WHERE referrer_id = ? AND created_at > ?", (referrer_id, now - 86400)) >= REF_MAX_PER_DAY: return {"status": "ignored", "reason": "limit_day"}
        conn.execute("INSERT INTO referrals (invitee_id, referrer_id, created_at) VALUES (?, ?, ?)", (uid, referrer_id, now))
        conn.commit()
        return {"status": "registered", "need_msgs": REF_MIN_MSGS, "reward": REF_REWARD_INVITEE}
    finally: conn.close()

@app.post("/referral/status")
async def referral_status(request: Request):
    data = await request.json()
    info = validate_init_data(data.get("init_data", ""))
    if not info: return {"status": "error", "reason": "auth"}
    uid = info["user"]["id"]; now = int(time.time())
    conn = get_db_connection()
    try:
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
    finally: conn.close()

@app.post("/referral/claim")
async def referral_claim(request: Request):
    data = await request.json()
    info = validate_init_data(data.get("init_data", ""))
    if not info: return {"status": "error", "reason": "auth"}
    uid = info["user"]["id"]; now = int(time.time())
    conn = get_db_connection()
    try:
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
    finally: conn.close()

async def is_channel_member(user_id):
    if not CHANNEL_USERNAME or not BOT_TOKEN: return None
    chat = CHANNEL_USERNAME if (CHANNEL_USERNAME.startswith("@") or CHANNEL_USERNAME.lstrip("-").isdigit()) else "@" + CHANNEL_USERNAME
    url = f"https://api.telegram.org/bot{BOT_TOKEN}/getChatMember"
    try:
        async with httpx.AsyncClient(timeout=8) as c: r = await c.post(url, json={"chat_id": chat, "user_id": user_id}); res = r.json()
    except Exception: return None
    if not res.get("ok"): return None
    member = res.get("result", {}); status = member.get("status")
    return status in ("creator", "administrator", "member") or (status == "restricted" and bool(member.get("is_member")))

@app.post("/case/status")
async def case_status(request: Request):
    data = await request.json()
    info = validate_init_data(data.get("init_data", ""))
    if not info: return {"status": "error", "reason": "auth"}
    uid = info["user"]["id"]
    member = await is_channel_member(uid) if CHANNEL_USERNAME else None
    now = int(time.time())
    conn = get_db_connection()
    try:
        row = conn.execute("SELECT last_free, pending, pending_case FROM case_state WHERE user_id = ?", (uid,)).fetchone()
        last_free, pending, pending_case = (row[0], row[1], row[2]) if row else (0, "", "")
        return {"status": "ok", "free_wait": max(0, last_free + CASE_FREE_COOLDOWN - now), "subscribed": member, "configured": bool(CHANNEL_USERNAME), "channel_link": CHANNEL_LINK, "price": CASE_ALL_PRICE, "pending": pending or None, "pending_case": pending_case or None}
    finally: conn.close()

@app.post("/case/open")
async def case_open(request: Request):
    data = await request.json()
    info = validate_init_data(data.get("init_data", ""))
    if not info: return {"status": "error", "reason": "auth"}
    uid = info["user"]["id"]; kind = data.get("case")
    if kind not in ("free", "all"): return {"status": "error", "reason": "bad_case"}
    if kind == "free":
        member = await is_channel_member(uid)
        if member is None: return {"status": "error", "reason": "channel_unavailable"}
        if not member: return {"status": "error", "reason": "not_subscribed"}
    now = int(time.time())
    conn = get_db_connection()
    try:
        row = conn.execute("SELECT last_free, pending, pending_case FROM case_state WHERE user_id = ?", (uid,)).fetchone()
        last_free, pending, pending_case = (row[0], row[1], row[2]) if row else (0, "", "")
        if pending: return {"status": "error", "reason": "pending", "item": pending, "case": pending_case}
        if kind == "free":
            if now - last_free < CASE_FREE_COOLDOWN: return {"status": "error", "reason": "cooldown", "wait": CASE_FREE_COOLDOWN - (now - last_free)}
            item = roll_free()
            conn.execute("""INSERT INTO case_state (user_id, last_free, pending, pending_case) VALUES (?, ?, ?, 'free') ON CONFLICT(user_id) DO UPDATE SET last_free = excluded.last_free, pending = excluded.pending, pending_case = 'free'""", (uid, now, item))
            conn.commit()
            return {"status": "ok", "item": item, "case": "free"}
        bal_row = conn.execute("SELECT bal FROM users WHERE user_id = ?", (uid,)).fetchone()
        bal = bal_row[0] if bal_row else 0
        if bal < CASE_ALL_PRICE: return {"status": "error", "reason": "no_funds"}
        item = roll_all()
        conn.execute("UPDATE users SET bal = bal - ? WHERE user_id = ?", (CASE_ALL_PRICE, uid))
        conn.execute("""INSERT INTO case_state (user_id, last_free, pending, pending_case) VALUES (?, 0, ?, 'all') ON CONFLICT(user_id) DO UPDATE SET pending = excluded.pending, pending_case = 'all'""", (uid, item))
        conn.commit()
        return {"status": "ok", "item": item, "case": "all", "new_bal": bal - CASE_ALL_PRICE}
    finally: conn.close()

@app.post("/case/resolve")
async def case_resolve(request: Request):
    data = await request.json()
    info = validate_init_data(data.get("init_data", ""))
    if not info: return {"status": "error", "reason": "auth"}
    uid = info["user"]["id"]; action = data.get("action")
    if action not in ("keep", "sell"): return {"status": "error", "reason": "bad_action"}
    conn = get_db_connection()
    try:
        row = conn.execute("SELECT pending FROM case_state WHERE user_id = ?", (uid,)).fetchone()
        item = row[0] if row else ""
        if not item: return {"status": "error", "reason": "nothing"}
        sell = ITEM_PRICE.get(item, 0) // SELL_DIV
        conn.execute("INSERT OR IGNORE INTO users (user_id) VALUES (?)", (uid,))
        if action == "keep" and get_owned(conn, uid).get(item): action = "sell"
        conn.execute("UPDATE case_state SET pending = '', pending_case = '' WHERE user_id = ?", (uid,))
        if action == "sell":
            if sell > 0: conn.execute("UPDATE users SET bal = bal + ? WHERE user_id = ?", (sell, uid))
        else: grant_item(conn, uid, item)
        conn.commit()
        return {"status": "ok", "action": action, "item": item, "sell": sell if action == "sell" else 0}
    finally: conn.close()

class BetData(BaseModel):
    init_data: str
    bet: int
    choice: str

@app.post("/play_rps")
async def play_rps(data: BetData):
    info = validate_init_data(data.init_data)
    if not info: return {"error": "auth"}
    user_id = info["user"]["id"]
    if data.choice not in ("rock", "paper", "scissors"): return {"error": "Неверный выбор"}
    if data.bet <= 0: return {"error": "Ставка должна быть больше нуля"}
    conn = get_db_connection()
    cursor = conn.execute("SELECT bal FROM users WHERE user_id = ?", (user_id,))
    row = cursor.fetchone()
    if not row: conn.close(); return {"error": "Пользователь не найден. Перезайди в бота."}
    current_bal = row[0]
    if current_bal < data.bet: conn.close(); return {"error": "Недостаточно звезд"}
    bot_choice = random.choice(['rock', 'paper', 'scissors'])
    win = False; tie = False
    if data.choice == bot_choice: tie = True
    elif (data.choice == 'rock' and bot_choice == 'scissors') or (data.choice == 'paper' and bot_choice == 'rock') or (data.choice == 'scissors' and bot_choice == 'paper'): win = True
    new_bal = current_bal
    if win: new_bal += data.bet  
    elif not tie: new_bal -= data.bet
    conn.execute("UPDATE users SET bal = ? WHERE user_id = ?", (new_bal, user_id))
    conn.commit(); conn.close()
    return {"status": "ok", "bot_choice": bot_choice, "win": win, "tie": tie, "new_bal": new_bal}

@app.post("/ask")
async def ask(request: Request):
    try:
        data = await request.json()
        user_text = data.get("text", "").lower()
        history_text = data.get("history", "")
        persona = data.get("persona", "hologram")
        if not user_text: return {"text": "Я не вижу текста.", "emotion": "thinking"}
        ask_info = validate_init_data(data.get("init_data", ""))
        if ask_info: count_message(ask_info["user"]["id"], len(user_text.strip()))
        system_prompt = BASE_PROMPT
        if persona == "astronaut": system_prompt = ASTRO_PROMPT
        if persona == "hologram" and ("станц" in user_text or "танец" in user_text or "танцуй" in user_text):
            chosen_dance = random.choice(DANCE_LIST)
            response = await client.chat.completions.create(model="openai/gpt-4o-mini", messages=[{"role": "system", "content": "Придумай короткую веселую фразу (1 предложение), чтобы начать танец."}, {"role": "user", "content": "Я прошу тебя станцевать."}], max_tokens=50, temperature=0.7)
            return {"text": response.choices[0].message.content, "action": "dance", "dance_name": chosen_dance}
        messages = [{"role": "system", "content": system_prompt}]
        if history_text: messages.append({"role": "user", "content": f"Вот история нашего диалога:\n{history_text}"})
        messages.append({"role": "user", "content": user_text})
        response = await client.chat.completions.create(model="openai/gpt-4o-mini", messages=messages, max_tokens=200, temperature=0.75, response_format={"type": "json_object"})
        result = json.loads(response.choices[0].message.content)
        return {"text": result.get("text", "Что-то я задумался..."), "emotion": result.get("emotion", "happy")}
    except Exception as e: return {"text": f"ОШИБКА: {str(e)}", "emotion": "sad"}

@app.post("/clear")
async def clear(request: Request): return {"status": "ok"}

def check_payment(from_id, payload, currency, amount):
    try: p_uid, item = str(payload).split("|", 1); p_uid = int(p_uid)
    except (ValueError, TypeError): return False, "Неверные данные платежа"
    if p_uid != from_id or currency != "XTR" or SHOP_PRICES.get(item) != amount: return False, "Платёж не прошёл проверку"
    return True, ""

async def tg_call(method, payload):
    url = f"https://api.telegram.org/bot{BOT_TOKEN}/{method}"
    async with httpx.AsyncClient(timeout=10) as c: return await c.post(url, json=payload)

@app.post("/invoice")
async def create_invoice(request: Request):
    data = await request.json()
    info = validate_init_data(data.get("init_data", ""))
    if not info: return {"error": "auth"}
    uid = info["user"]["id"]; item_id = data.get("item")
    price = SHOP_PRICES.get(item_id)
    if not price: return {"error": "item not found"}
    if not WEBHOOK_SECRET: return {"error": "payments_disabled"}
    conn = get_db_connection()
    try:
        if get_owned(conn, uid).get(item_id): return {"error": "owned"}
    finally: conn.close()
    payload = {"title": f"Покупка: {item_id}", "description": "Покупка в Holo Friend", "payload": f"{uid}|{item_id}", "currency": "XTR", "provider_token": "", "prices": [{"label": "Товар", "amount": price}]}
    r = await tg_call("createInvoiceLink", payload)
    res = r.json()
    if res.get("ok"): return {"link": res["result"]}
    return {"error": "Не удалось создать счет", "details": res}

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
                    conn = get_db_connection()
                    try:
                        if get_owned(conn, int(_uid)).get(_item): ok, err = False, "Этот предмет у тебя уже есть"
                    finally: conn.close()
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
                conn = get_db_connection()
                try:
                    if charge and not conn.execute("SELECT 1 FROM payments WHERE charge_id = ?", (charge,)).fetchone():
                        conn.execute("INSERT INTO payments (charge_id, user_id, item, created_at) VALUES (?, ?, ?, ?)", (charge, from_id, item, int(time.time())))
                        grant_item(conn, from_id, item); conn.commit()
                finally: conn.close()
            await tg_call("sendMessage", {"chat_id": msg["chat"]["id"], "text": "✅ Оплата прошла успешно! Возвращайся в приложение."})
            return {"ok": True}
        return {"ok": True}
    except Exception as e: return {"ok": True}

# ИСПРАВЛЕНО: добавлен HEAD метод для Render
@app.api_route("/", methods=["GET", "HEAD"])
def health(): return {"status": "ok"}
