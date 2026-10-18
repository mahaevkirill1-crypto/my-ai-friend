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
app.add_middleware(CORSMiddleware, allow_origins=["*"], allow_methods=["*"], allow_headers=["*"])

SYSTEM_PROMPT = """
Ты — ИИ-друг, голографический робот. Тебе 17 лет. Ты спокойный, уверенный, с лёгкой иронией.
Отвечай коротко (1-3 предложения).

Ты умеешь не только говорить, но и показывать эмоции и танцевать.
Доступные эмоции: happy, sad, surprised, angry, thinking, sleepy.
Доступные танцы: disco, dab, floss, robot, party, twist.

Если пользователь просит станцевать (например, "станцуй", "покажи танец"), выбери ОДИН танец и верни JSON:
{"text": "твой ответ (например: Лови мой флосс!)", "action": "dance", "dance_name": "название_танца"}

В остальных случаях верни JSON:
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
            response_format={"type": "json_object"}
        )
        
        result = json.loads(response.choices[0].message.content)
        
        if result.get("action") == "dance":
            return {"text": result.get("text", "Смотри!"), "action": "dance", "dance_name": result.get("dance_name", "floss")}
        else:
            return {"text": result.get("text", "Что-то я задумался..."), "emotion": result.get("emotion", "happy")}
        
    except Exception as e:
        return {"text": f"ОШИБКА: {str(e)}", "emotion": "sad"}

@app.get("/")
def health():
    return {"status": "ok"}
