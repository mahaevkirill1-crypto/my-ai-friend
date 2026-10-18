from fastapi import FastAPI, Request
from fastapi.middleware.cors import CORSMiddleware
from openai import AsyncOpenAI
import os

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
Ты — ИИ-друг. Тебе 17 лет. Ты спокойный, уверенный, с лёгкой иронией, но без грубости. 
Ты не токсичный, не понтуешься. Говоришь просто, по делу, иногда с шуткой.
Отвечай коротко (1-3 предложения), если не просят подробностей.
Если пользователь грустит — поддержи, но без сюсюканья.
Если не знаешь — честно скажи.
Никогда не груби, не унижай.
"""

@app.post("/ask")
async def ask(request: Request):
    try:
        # Читаем "сырые" данные, которые прислал Telegram
        data = await request.json()
        user_text = data.get("text", "")
        
        if not user_text:
            return {"text": "Я не вижу текста. Попробуй еще раз."}

        response = await client.chat.completions.create(
            model="openai/gpt-4o-mini",
            messages=[
                {"role": "system", "content": SYSTEM_PROMPT},
                {"role": "user", "content": user_text}
            ],
            max_tokens=200,
            temperature=0.7,
        )
        answer = response.choices[0].message.content
        return {"text": answer}
    except Exception as e:
        return {"text": f"ОШИБКА: {str(e)}"}

@app.get("/")
def health():
    return {"status": "ok"}
