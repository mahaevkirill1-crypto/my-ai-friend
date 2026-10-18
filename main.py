from fastapi import FastAPI, Request
from fastapi.middleware.cors import CORSMiddleware
from openai import AsyncOpenAI
import os
import json
import random

client = AsyncOpenAI(
    api_key=os.getenv("PROXYAPI_KEY"),
    base_url="https://api.proxyapi.ru/v1"
)

app = FastAPI()
app.add_middleware(CORSMiddleware, allow_origins=["*"], allow_methods=["*"], allow_headers=["*"])

# Список танцев для случайного выбора на бэкенде
DANCE_LIST = ["disco", "dab", "floss", "robot", "party", "twist"]

SYSTEM_PROMPT = """
Ты — ИИ-друг, голографический робот. Тебе 17 лет. Ты спокойный, уверенный, с лёгкой иронией.
Отвечай коротко (1-3 предложения).

Ты умеешь не только говорить, но и показывать эмоции и танцевать.
Доступные эмоции: happy, sad, surprised, angry, thinking, sleepy.
Доступные танцы: disco, dab, floss, robot, party, twist.

Если пользователь просит станцевать (например, "станцуй", "покажи танец"), верни JSON:
{"text": "твой ответ (например: Лови мой флосс!)", "action": "dance"}

В остальных случаях верни JSON:
{"text": "твой ответ", "emotion": "название_эмоции"}
"""

@app.post("/ask")
async def ask(request: Request):
    try:
        data = await request.json()
        user_text = data.get("text", "").lower()
        
        if not user_text:
            return {"text": "Я не вижу текста.", "emotion": "thinking"}

        # Если просят танец, мы не спрашиваем ИИ, а сами выбираем случайный танец
        if "станц" in user_text or "танец" in user_text or "танцуй" in user_text:
            chosen_dance = random.choice(DANCE_LIST)
            # Просим ИИ придумать короткую фразу
            response = await client.chat.completions.create(
                model="openai/gpt-4o-mini",
                messages=[
                    {"role": "system", "content": "Придумай короткую веселую фразу (1 предложение), чтобы начать танец."},
                    {"role": "user", "content": "Я прошу тебя станцевать."}
                ],
                max_tokens=50,
                temperature=0.7
            )
            text = response.choices[0].message.content
            return {"text": text, "action": "dance", "dance_name": chosen_dance}

        # Для обычных вопросов
        response = await client.chat.completions.create(
            model="openai/gpt-4o-mini",
            messages=[
                {"role": "system", "content": SYSTEM_PROMPT},
                {"role": "user", "content": user_text}
            ],
            max_tokens=200,
            temperature=0.7,
            response_format={"type": "json_object"}
        )
        
        result = json.loads(response.choices[0].message.content)
        return {"text": result.get("text", "Что-то я задумался..."), "emotion": result.get("emotion", "happy")}
        
    except Exception as e:
        return {"text": f"ОШИБКА: {str(e)}", "emotion": "sad"}

@app.get("/")
def health():
    return {"status": "ok"}
