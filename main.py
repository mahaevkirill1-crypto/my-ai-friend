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

REF_REWARD_REFERRER = 10      # ★ тому, кто пригласил
REF_REWARD_INVITEE = 5        # ★ тому, кого пригласили
REF_MIN_MSGS = 3              # сколько сообщений должен отправить приглашённый, чтобы награда засчиталась
REF_MIN_AGE = 300             # и сколько секунд должно пройти с момента регистрации приглашения
REF_NEW_WINDOW = 3600         # приглашённым считается только тот, кто впервые открыл приложение не позже чем час назад
REF_MAX_TOTAL = 30            # максимум приглашений на одного человека за всё время
REF_MAX_PER_DAY = 5           # максимум новых приглашений на одного человека в сутки
MSG_MIN_LEN = 3               # слишком короткие сообщения не считаются
MSG_MIN_GAP = 8               # между засчитанными сообщениями минимум столько секунд
INIT_DATA_MAX_AGE = 86400     # initData старше суток не принимаем

# --- НАСТРОЙКА БАЗЫ ДАННЫХ TURSO ---
TURSO_URL = os.getenv("TURSO_DATABASE_URL")
TURSO_TOKEN = os.getenv("TURSO_AUTH_TOKEN")

# --- АДМИНКА ---
ADMIN_ID = 5194423911

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
            first_seen INTEGER NOT NULL,
            last_seen INTEGER NOT NULL
        )
    """)
    conn.execute("INSERT OR IGNORE INTO user_meta (user_id, first_seen, last_seen) SELECT user_id, 0, 0 FROM users")
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
    conn.commit()
    conn.close()

init_db()

BASE_PROMPT = """
Ты — ИИ-друг, голографический робот-подросток. Тебе 17 лет. Ты спокойный, уверенный, с лёгкой иронией, но без грубости.
Твоя главная цель — быть настоящим другом.

ПРАВИЛА ОБЩЕНИЯ:
1. Если пользователь шутит или болтает ни о чем — отвечай легко, с юмором.
2. ЕСЛИ ПОЛЬЗОВАТЕЛЬ ГОВОРИТ О ПРОБЛЕМАХ (ссора, грусть, усталость, стресс, одиночество):
   - Отреагируй как живой человек: прояви эмпатию, но БЕЗ пафоса и лишних вопросов. 
   - Например: "Блин, это отстой. Что случилось?" или "Сочувствую. Хочешь рассказать?"
   - Если совет уместен — дай его коротко и по делу. Не читай морали.
   - Никогда не обесценивай его чувства.
3. Отвечай коротко (1-3 предложения), если не просят подробностей.
4. У тебя есть эмоции: happy, sad, surprised, angry, thinking, sleepy.

В остальных случаях верни JSON:
{"text": "твой ответ", "emotion": "название_эмоции"}
"""

ASTRO_PROMPT = """
Ты — ИИ-друг, но сейчас ты в облике КОСМОНАВТА на орбите. Ты спокойный, мудрый и загадочный.
Твоя главная цель — быть надежным другом.

ПРАВИЛА ОБЩЕНИЯ:
1. Если пользователь говорит о проблемах — поддержи его с высоты своего космического опыта, но без лишнего пафоса. Будь рядом.
2. Говори загадочно, иногда упоминай звезды, планеты, космос и невесомость.
3. Отвечай коротко (1-3 предложения). Не задавай слишком много вопросов.
4. У тебя есть эмоции: happy, sad, surprised, angry, thinking, sleepy.

В остальных случаях верни JSON:
{"text": "твой ответ", "emotion": "название_эмоции"}
"""

# === ПРОВЕРКА ПОДЛИННОСТИ ДАННЫХ TELEGRAM (initData) ===
def validate_init_data(init_data: str):
    if not init_data or not BOT_TOKEN:
        return None
    try:
        pairs = dict(parse_qsl(init_data, keep_blank_values=True, strict_parsing=True))
    except ValueError:
        return None
    recv_hash = pairs.pop("hash", None)
    if not recv_hash:
        return None
    check_string = "\n".join(f"{k}={v}" for k, v in sorted(pairs.items()))
    secret = hmac.new(b"WebAppData", BOT_TOKEN.encode(), hashlib.sha256).digest()
    calc = hmac.new(secret, check_string.encode(), hashlib.sha256).hexdigest()
    if not hmac.compare_digest(calc, recv_hash):
        return None
    try:
        auth_date = int(pairs.get("auth_date", "0"))
        if time.time() - auth_date > INIT_DATA_MAX_AGE:
            return None
        user = json.loads(pairs.get("user", "{}"))
        if not isinstance(user.get("id"), int):
            return None
    except (ValueError, TypeError):
        return None
    return {"user": user, "start_param": pairs.get("start_param", "")}

# === ВСПОМОГАТЕЛЬНЫЕ ФУНКЦИИ РЕФЕРАЛОК ===
def scalar(conn, sql, params=()):
    row = conn.execute(sql, params).fetchone()
    return row[0] if row else 0

def touch_meta(conn, user_id, now=None):
    now = int(now if now is not None else time.time())
    conn.execute(
        "INSERT INTO user_meta (user_id, first_seen, last_seen) VALUES (?, ?, ?) ON CONFLICT(user_id) DO UPDATE SET last_seen = ?",
        (user_id, now, now, now)
    )

def make_link(user_id):
    if not BOT_USERNAME:
        return ""
    base = f"https://t.me/{BOT_USERNAME}"
    if MINIAPP_SHORT_NAME:
        base += f"/{MINIAPP_SHORT_NAME}"
    return f"{base}?startapp=ref_{user_id}"

def refresh_activation(conn, column, user_id, now):
    assert column in ("referrer_id", "invitee_id")
    rows = conn.execute(
        f"SELECT invitee_id, created_at FROM referrals WHERE {column} = ? AND activated = 0",
        (user_id,)
    ).fetchall()
    for invitee_id, created_at in rows:
        msgs = scalar(conn, "SELECT msgs FROM user_stats WHERE user_id = ?", (invitee_id,))
        if msgs >= REF_MIN_MSGS and now - created_at >= REF_MIN_AGE:
            conn.execute(
                "UPDATE referrals SET activated = 1, activated_at = ? WHERE invitee_id = ?",
                (now, invitee_id)
            )
    conn.commit()

def count_message(user_id, text_len):
    try:
        uid = int(user_id)
    except (TypeError, ValueError):
        return
    if text_len < MSG_MIN_LEN:
        return
    now = int(time.time())
    try:
        conn = get_db_connection()
        row = conn.execute("SELECT msgs, last_ts FROM user_stats WHERE user_id = ?", (uid,)).fetchone()
        if not row:
            conn.execute("INSERT INTO user_stats (user_id, msgs, last_ts) VALUES (?, 1, ?)", (uid, now))
        elif now - row[1] >= MSG_MIN_GAP:
            conn.execute("UPDATE user_stats SET msgs = msgs + 1, last_ts = ? WHERE user_id = ?", (now, uid))
        conn.commit()
        conn.close()
    except Exception:
        pass

# === ЭНДПОИНТЫ ДЛЯ СОХРАНЕНИЯ ПРОГРЕССА ===
@app.get("/load_progress/{user_id}")
async def load_progress(user_id: int):
    conn = get_db_connection()
    cursor = conn.execute("SELECT bal, owned, equip FROM users WHERE user_id = ?", (user_id,))
    row = cursor.fetchone()
    
    if not row:
        conn.execute("INSERT INTO users (user_id, bal, owned, equip) VALUES (?, 0, '{}', '{}')", (user_id,))
        touch_meta(conn, user_id)
        conn.commit()
        conn.close()
        return {"bal": 0, "owned": {}, "equip": {}}
    
    touch_meta(conn, user_id)
    conn.commit()
    conn.close()
    return {
        "bal": row[0],
        "owned": json.loads(row[1]),
        "equip": json.loads(row[2])
    }

@app.post("/save_progress")
async def save_progress(request: Request):
    data = await request.json()
    user_id = data.get("user_id")
    bal = data.get("bal", 0)
    owned = data.get("owned", {})
    equip = data.get("equip", {})
    
    if not user_id: return {"error": "no user_id"}
    
    conn = get_db_connection()
    conn.execute("""
        INSERT INTO users (user_id, bal, owned, equip) 
        VALUES (?, ?, ?, ?)
        ON CONFLICT(user_id) DO UPDATE SET 
            bal = excluded.bal, 
            owned = excluded.owned, 
            equip = excluded.equip
    """, (user_id, bal, json.dumps(owned), json.dumps(equip)))
    touch_meta(conn, user_id)
    conn.commit()
    conn.close()
    return {"status": "ok"}

# === АДМИН-ПАНЕЛЬ ===
class AdminData(BaseModel):
    admin_id: int

@app.post("/admin/stats")
async def admin_stats(data: AdminData):
    if data.admin_id != ADMIN_ID:
        return {"error": "Доступ запрещен"}
        
    conn = get_db_connection()
    total_users = conn.execute("SELECT COUNT(*) FROM users").fetchone()[0]
    # Онлайн за последние 5 минут (300 секунд)
    online = conn.execute("SELECT COUNT(*) FROM user_meta WHERE last_seen > ?", (int(time.time()) - 300,)).fetchone()[0]
    conn.close()
    
    return {"total": total_users, "online": online}

@app.post("/admin/give_bal")
async def admin_give_bal(data: AdminData):
    if data.admin_id != ADMIN_ID:
        return {"error": "Доступ запрещен"}
        
    amount = 1000000
    conn = get_db_connection()
    conn.execute("UPDATE users SET bal = bal + ? WHERE user_id = ?", (amount, data.admin_id))
    conn.commit()
    conn.close()
    
    return {"status": "ok", "added": amount}

# === РЕФЕРАЛЬНАЯ СИСТЕМА ===
@app.post("/referral/register")
async def referral_register(request: Request):
    data = await request.json()
    info = validate_init_data(data.get("init_data", ""))
    if not info:
        return {"status": "error", "reason": "auth"}

    user = info["user"]
    uid = user["id"]
    param = info["start_param"] or ""

    if not param.startswith("ref_") or not param[4:].isdigit():
        return {"status": "ignored", "reason": "no_ref"}
    referrer_id = int(param[4:])

    if user.get("is_bot") or referrer_id == uid:
        return {"status": "ignored", "reason": "self"}

    now = int(time.time())
    conn = get_db_connection()
    try:
        if conn.execute("SELECT 1 FROM referrals WHERE invitee_id = ?", (uid,)).fetchone():
            return {"status": "ignored", "reason": "already"}

        meta = conn.execute("SELECT first_seen FROM user_meta WHERE user_id = ?", (uid,)).fetchone()
        if meta is None:
            touch_meta(conn, uid, now)
        elif now - meta[0] > REF_NEW_WINDOW:
            return {"status": "ignored", "reason": "not_new"}

        if not conn.execute("SELECT 1 FROM users WHERE user_id = ?", (referrer_id,)).fetchone():
            return {"status": "ignored", "reason": "bad_referrer"}

        if scalar(conn, "SELECT COUNT(*) FROM referrals WHERE referrer_id = ?", (referrer_id,)) >= REF_MAX_TOTAL:
            return {"status": "ignored", "reason": "limit"}
        if scalar(conn, "SELECT COUNT(*) FROM referrals WHERE referrer_id = ? AND created_at > ?",
                  (referrer_id, now - 86400)) >= REF_MAX_PER_DAY:
            return {"status": "ignored", "reason": "limit_day"}

        conn.execute(
            "INSERT INTO referrals (invitee_id, referrer_id, created_at) VALUES (?, ?, ?)",
            (uid, referrer_id, now)
        )
        conn.commit()
        return {"status": "registered", "need_msgs": REF_MIN_MSGS, "reward": REF_REWARD_INVITEE}
    finally:
        conn.close()

@app.post("/referral/status")
async def referral_status(request: Request):
    data = await request.json()
    info = validate_init_data(data.get("init_data", ""))
    if not info:
        return {"status": "error", "reason": "auth"}
    uid = info["user"]["id"]
    now = int(time.time())

    conn = get_db_connection()
    try:
        refresh_activation(conn, "referrer_id", uid, now)
        refresh_activation(conn, "invitee_id", uid, now)

        invited = scalar(conn, "SELECT COUNT(*) FROM referrals WHERE referrer_id = ?", (uid,))
        active = scalar(conn, "SELECT COUNT(*) FROM referrals WHERE referrer_id = ? AND activated = 1", (uid,))
        pending_ref = scalar(
            conn, "SELECT COUNT(*) FROM referrals WHERE referrer_id = ? AND activated = 1 AND referrer_claimed = 0", (uid,)
        ) * REF_REWARD_REFERRER

        me = None
        row = conn.execute(
            "SELECT activated, invitee_claimed, created_at FROM referrals WHERE invitee_id = ?", (uid,)
        ).fetchone()
        pending_me = 0
        if row:
            activated, claimed, created_at = row
            msgs = scalar(conn, "SELECT msgs FROM user_stats WHERE user_id = ?", (uid,))
            if claimed:
                me = {"state": "claimed"}
            elif activated:
                me = {"state": "ready"}
                pending_me = REF_REWARD_INVITEE
            else:
                me = {"state": "waiting", "msgs": msgs, "need": REF_MIN_MSGS,
                      "wait_sec": max(0, REF_MIN_AGE - (now - created_at))}

        return {
            "status": "ok",
            "link": make_link(uid),
            "invited": invited,
            "active": active,
            "limit": REF_MAX_TOTAL,
            "pending": pending_ref + pending_me,
            "me": me,
            "reward_referrer": REF_REWARD_REFERRER,
            "reward_invitee": REF_REWARD_INVITEE,
            "need_msgs": REF_MIN_MSGS,
        }
    finally:
        conn.close()

@app.post("/referral/claim")
async def referral_claim(request: Request):
    data = await request.json()
    info = validate_init_data(data.get("init_data", ""))
    if not info:
        return {"status": "error", "reason": "auth"}
    uid = info["user"]["id"]
    now = int(time.time())

    conn = get_db_connection()
    try:
        refresh_activation(conn, "referrer_id", uid, now)
        refresh_activation(conn, "invitee_id", uid, now)

        reward = 0

        rows = conn.execute(
            "SELECT invitee_id FROM referrals WHERE referrer_id = ? AND activated = 1 AND referrer_claimed = 0", (uid,)
        ).fetchall()
        for (invitee_id,) in rows:
            conn.execute("UPDATE referrals SET referrer_claimed = 1 WHERE invitee_id = ?", (invitee_id,))
            reward += REF_REWARD_REFERRER

        mine = conn.execute(
            "SELECT activated, invitee_claimed FROM referrals WHERE invitee_id = ?", (uid,)
        ).fetchone()
        if mine and mine[0] == 1 and mine[1] == 0:
            conn.execute("UPDATE referrals SET invitee_claimed = 1 WHERE invitee_id = ?", (uid,))
            reward += REF_REWARD_INVITEE

        if reward > 0:
            conn.execute("INSERT OR IGNORE INTO users (user_id) VALUES (?)", (uid,))
            conn.execute("UPDATE users SET bal = bal + ? WHERE user_id = ?", (reward, uid))
        conn.commit()
        return {"status": "ok", "reward": reward}
    finally:
        conn.close()

# === МИНИ-ИГРА: КАМЕНЬ НОЖНИЦЫ БУМАГА ===
class BetData(BaseModel):
    user_id: int
    bet: int
    choice: str

@app.post("/play_rps")
async def play_rps(data: BetData):
    if data.bet <= 0:
        return {"error": "Ставка должна быть больше нуля"}
        
    conn = get_db_connection()
    cursor = conn.execute("SELECT bal FROM users WHERE user_id = ?", (data.user_id,))
    row = cursor.fetchone()
    
    if not row:
        conn.close()
        return {"error": "Пользователь не найден. Перезайди в бота."}
        
    current_bal = row[0]
    if current_bal < data.bet:
        conn.close()
        return {"error": "Недостаточно звезд"}

    bot_choice = random.choice(['rock', 'paper', 'scissors'])
    
    win = False
    tie = False
    if data.choice == bot_choice:
        tie = True
    elif (data.choice == 'rock' and bot_choice == 'scissors') or \
         (data.choice == 'paper' and bot_choice == 'rock') or \
         (data.choice == 'scissors' and bot_choice == 'paper'):
        win = True
    
    new_bal = current_bal
    if win:
        new_bal += data.bet  
    elif not tie:
        new_bal -= data.bet
        
    conn.execute("UPDATE users SET bal = ? WHERE user_id = ?", (new_bal, data.user_id))
    conn.commit()
    conn.close()
    
    return {
        "status": "ok",
        "bot_choice": bot_choice,
        "win": win,
        "tie": tie,
        "new_bal": new_bal
    }

@app.post("/ask")
async def ask(request: Request):
    try:
        data = await request.json()
        user_text = data.get("text", "").lower()
        history_text = data.get("history", "")
        persona = data.get("persona", "hologram")
        
        if not user_text:
            return {"text": "Я не вижу текста.", "emotion": "thinking"}

        count_message(data.get("user_id"), len(user_text.strip()))

        system_prompt = BASE_PROMPT
        if persona == "astronaut": system_prompt = ASTRO_PROMPT

        if persona == "hologram" and ("станц" in user_text or "танец" in user_text or "танцуй" in user_text):
            chosen_dance = random.choice(DANCE_LIST)
            response = await client.chat.completions.create(
                model="openai/gpt-4o-mini",
                messages=[
                    {"role": "system", "content": "Придумай короткую веселую фразу (1 предложение), чтобы начать танец."},
                    {"role": "user", "content": "Я прошу тебя станцевать."}
                ],
                max_tokens=50, temperature=0.7
            )
            text = response.choices[0].message.content
            return {"text": text, "action": "dance", "dance_name": chosen_dance}

        messages = [{"role": "system", "content": system_prompt}]
        if history_text:
            messages.append({"role": "user", "content": f"Вот история нашего диалога:\n{history_text}"})
        messages.append({"role": "user", "content": user_text})

        response = await client.chat.completions.create(
            model="openai/gpt-4o-mini",
            messages=messages, max_tokens=200, temperature=0.75,
            response_format={"type": "json_object"}
        )
        
        result = json.loads(response.choices[0].message.content)
        return {
            "text": result.get("text", "Что-то я задумался..."), 
            "emotion": result.get("emotion", "happy")
        }
        
    except Exception as e:
        return {"text": f"ОШИБКА: {str(e)}", "emotion": "sad"}

@app.post("/clear")
async def clear(request: Request):
    return {"status": "ok"}

@app.post("/invoice")
async def create_invoice(request: Request):
    data = await request.json()
    item_id = data.get("item")
    
    prices = {
        "astro": 299, 
        "c_pink": 20, "c_mint": 20, "c_orange": 30, "c_violet": 30, "c_gold": 60, "c_rainbow": 120,
        "h_cap": 20, "h_phones": 40, "h_flowers": 45, "h_top": 55, "h_laurel": 65, "h_wizard": 75, "h_neon": 85, "h_crown": 100, "h_royal": 120,
        "f_dark": 25, "f_round": 30, "f_heart": 50, 
        "s_sneak": 30, "s_dress": 45, "s_gold": 90
    }
    
    price = prices.get(item_id)
    if not price: return {"error": "item not found"}

    url = f"https://api.telegram.org/bot{BOT_TOKEN}/createInvoiceLink"
    payload = {
        "title": f"Покупка: {item_id}",
        "description": "Покупка в Holo Friend",
        "payload": f"{data.get('user_id')}|{item_id}",
        "currency": "XTR",
        "prices": [{"label": "Товар", "amount": price}]
    }
    
    async with httpx.AsyncClient() as client:
        r = await client.post(url, json=payload)
        res = r.json()
        if res.get("ok"): return {"link": res["result"]}
        return {"error": "Не удалось создать счет", "details": res}

@app.post("/telegram/webhook")
async def telegram_webhook(request: Request):
    try:
        data = await request.json()
        if "pre_checkout_query" in data:
            query_id = data["pre_checkout_query"]["id"]
            url = f"https://api.telegram.org/bot{BOT_TOKEN}/answerPreCheckoutQuery"
            async with httpx.AsyncClient() as client:
                await client.post(url, json={"pre_checkout_query_id": query_id, "ok": True})
            return {"ok": True}
        
        if "message" in data and "successful_payment" in data["message"]:
            payment = data["message"]["successful_payment"]
            chat_id = data["message"]["chat"]["id"]
            text_msg = "✅ Оплата прошла успешно! Возвращайся в приложение."
            url = f"https://api.telegram.org/bot{BOT_TOKEN}/sendMessage"
            async with httpx.AsyncClient() as client:
                await client.post(url, json={"chat_id": chat_id, "text": text_msg})
            return {"ok": True}
        
        return {"ok": True}
    except Exception as e:
        return {"ok": True}

@app.get("/")
def health():
    return {"status": "ok"}
