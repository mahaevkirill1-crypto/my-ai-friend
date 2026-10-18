from fastapi import FastAPI, Request
from fastapi.middleware.cors import CORSMiddleware
from openai import AsyncOpenAI
import os
import json

client = AsyncOpenAI(
    api_key=os.getenv("PROXYAPI_KEY"),
    base_url="https://api.proxyapi.ru/v1"
)

app = FastAPI()

app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],
    allow_methods=["*"],
    allow_headers=["*"],
)

SYSTEM_PROMPT = """
Ты — ИИ-друг, голографический робот. Тебе 17 лет. Ты спокойный, уверенный, с лёгкой иронией, но без грубости.
Отвечай коротко (1-3 предложения), если не просят подробностей.
Никогда не груби, не унижай.

Твоя задача — не только ответить, но и выбрать свою эмоцию для анимации.
Доступные эмоции: 
- happy (радость, улыбка)
- sad (грусть, печаль)
- surprised (удивление)
- angry (злость)
- thinking (задумчивость)
- sleepy (сон, усталость)

Верни ответ СТРОГО в формате JSON:
{"text": "твой ответ", "emotion": "название_эмоции"}
"""

@app.post("/ask")
async def ask(request: Request):
    try:
        data = await request.json()
        user_text = data.get("text", "")
        
        if not user_text:
            return {"text": "Я не вижу текста.", "emotion": "thinking"}

        response = await client.chat.completions.create(
            model="openai/gpt-4o-mini",
            messages=[
                {"role": "system", "content": SYSTEM_PROMPT},
                {"role": "user", "content": user_text}
            ],
            max_tokens=200,
            temperature=0.7,
            response_format={"type": "json_object"} # Заставляем вернуть JSON
        )
        
        # Парсим JSON, который вернула нейросеть
        result = json.loads(response.choices[0].message.content)
        return {
            "text": result.get("text", "Что-то я задумался..."),
            "emotion": result.get("emotion", "happy")
        }
        
    except Exception as e:
        return {"text": f"ОШИБКА: {str(e)}", "emotion": "sad"}

@app.get("/")
def health():
    return {"status": "ok"}
