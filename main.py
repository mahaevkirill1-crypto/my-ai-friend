from fastapi import FastAPI, Request
from fastapi.middleware.cors import CORSMiddleware
from openai import AsyncOpenAI
import os
import json
import random
import httpx

client = AsyncOpenAI(
    api_key=os.getenv("PROXYAPI_KEY"),
    base_url="https://api.proxyapi.ru/v1"
)

app = FastAPI()
app.add_middleware(CORSMiddleware, allow_origins=["*"], allow_methods=["*"], allow_headers=["*"])

BOT_TOKEN = os.getenv("BOT_TOKEN")
DANCE_LIST = ["disco", "dab", "floss", "robot", "party", "twist"]

BASE_PROMPT = """
Ты — ИИ-друг, голографический робот. Тебе 17 лет. Отвечай коротко (1-3 предложения).
Ты умеешь показывать эмоции и танцевать.
Эмоции: happy, sad, surprised, angry, thinking, sleepy.
Танцы: disco, dab, floss, robot, party, twist.

Если тебя просят станцевать, верни JSON:
{"text": "твой ответ", "action": "dance"}

В остальных случаях верни JSON:
{"text": "твой ответ", "emotion": "название_эмоции"}
"""

ASTRO_PROMPT = """
Ты — ИИ-друг, но сейчас ты в облике КОСМОНАВТА. Ты находишься на орбите, в невесомости.
Ты говоришь загадочно, иногда упоминаешь звезды, планеты, космос и невесомость.
Ты спокойный и мудрый. Отвечай коротко (1-3 предложения).
У тебя есть эмоции: happy, sad, surprised, angry, thinking, sleepy.

В остальных случаях верни JSON:
{"text": "твой ответ", "emotion": "название_эмоции"}
"""

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

        # Если просят танец
        if persona == "hologram" and ("станц" in user_text or "танец" in user_text or "танцуй" in user_text):
            chosen_dance = random.choice(DANCE_LIST)
            response = await client.chat.completions.create(
                model="openai/gpt-4o-mini",
                messages=[
                    {"role": "system", "content": system_prompt},
                    {"role": "user", "content": "Я прошу тебя станцевать."}
                ],
                max_tokens=50, temperature=0.7
            )
            text = response.choices[0].message.content
            return {"text": text, "action": "dance", "dance_name": chosen_dance}

        # Обычный диалог с учетом памяти
        messages = [{"role": "system", "content": system_prompt}]
        if history_text:
            messages.append({"role": "user", "content": f"Вот история нашего диалога:\n{history_text}"})
        messages.append({"role": "user", "content": user_text})

        response = await client.chat.completions.create(
            model="openai/gpt-4o-mini",
            messages=messages, max_tokens=200, temperature=0.7,
            response_format={"type": "json_object"}
        )
        
        result = json.loads(response.choices[0].message.content)
        
        # Возвращаем ТОЛЬКО текст и эмоцию. Обиду обрабатывает клиент.
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

@app.get("/")
def health():
    return {"status": "ok"}
