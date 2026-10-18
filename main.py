from fastapi import FastAPI
from fastapi.middleware.cors import CORSMiddleware
from pydantic import BaseModel
from openai import AsyncOpenAI
import os

# Используем новый единый шлюз ProxyAPI и стандартную рабочую модель
client = AsyncOpenAI(
    api_key=os.getenv("PROXYAPI_KEY"),
    base_url="https://api.proxyapi.ru/v1"
)

app = FastAPI()

# Разрешаем запросы с GitHub Pages и из самого Telegram
app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"], # Для надежности оставляем *, чтобы Telegram точно пропустил
    allow_methods=["*"],
    allow_headers=["*"],
)

class UserRequest(BaseModel):
    text: str
    user_id: str = "default"

SYSTEM_PROMPT = """
Ты — ИИ-друг. Тебе 17 лет. Ты спокойный, уверенный, с лёгкой иронией, но без грубости. 
Ты не токсичный, не понтуешься. Говоришь просто, по делу, иногда с шуткой.
Отвечай коротко (1-3 предложения), если не просят подробностей.
Если пользователь грустит — поддержи, но без сюсюканья.
Если не знаешь — честно скажи.
Никогда не груби, не унижай.
"""

@app.post("/ask")
async def ask(request: UserRequest):
    try:
        response = await client.chat.completions.create(
            model="openai/gpt-4o-mini",
            messages=[
                {"role": "system", "content": SYSTEM_PROMPT},
                {"role": "user", "content": request.text}
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
