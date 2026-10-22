from fastapi import FastAPI, Request
from fastapi.middleware.cors import CORSMiddleware
from openai import AsyncOpenAI
import os
import json
import random
import httpx
import libsql

client = AsyncOpenAI(
    api_key=os.getenv("PROXYAPI_KEY"),
    base_url="https://api.proxyapi.ru/v1"
)

app = FastAPI()
app.add_middleware(CORSMiddleware, allow_origins=["*"], allow_methods=["*"], allow_headers=["*"])

BOT_TOKEN = os.getenv("BOT_TOKEN")
DANCE_LIST = ["disco", "dab", "floss", "robot", "party", "twist"]

# --- НАСТРОЙКА БАЗЫ ДАННЫХ TURSO ---
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

# === ЭНДПОИНТЫ ДЛЯ СОХРАНЕНИЯ ПРОГРЕССА ===
@app.get("/load_progress/{user_id}")
async def load_progress(user_id: int):
    conn = get_db_connection()
    cursor = conn.execute("SELECT bal, owned, equip FROM users WHERE user_id = ?", (user_id,))
    row = cursor.fetchone()
    conn.close()
    
    if row:
        return {
            "bal": row[0],
            "owned": json.loads(row[1]),
            "equip": json.loads(row[2])
        }
    return {"bal": 0, "owned": {}, "equip": {}}

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
    conn.commit()
    conn.close()
    return {"status": "ok"}

@app.post("/ask")
async def ask(request: Request):
    try:
        data = await request.json()
        user_text = data.get("text", "").lower()
        history_text = data.get("history", "")
        persona = data.get("persona", "hologram")
        
        if not user_text:
            return {"text": "Я не вижу текста.", "emotion": "thinking"}

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
