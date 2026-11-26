"""Клиент ProxyAPI (OpenAI-совместимый). Ключ берётся из env и нигде не логируется."""
import os, re


class LLMTransient(Exception):
    """429 / 5xx / сеть / таймаут — можно повторить."""


class LLMPermanent(Exception):
    """4xx кроме 429 — повтор бессмысленен."""


_SECRET = re.compile(r"(bot\d{6,}:[A-Za-z0-9_-]{20,}|sk-[A-Za-z0-9_-]{16,}|Bearer\s+[A-Za-z0-9._-]{12,})")


def redact(s):
    """Вырезает токены (бот, ключи) из любой строки перед логированием."""
    return _SECRET.sub("[REDACTED]", str(s))


class ProxyApiLLM:
    def __init__(self, api_key=None, base_url="https://api.proxyapi.ru/v1", timeout=120):
        self._key = api_key or os.getenv("PROXYAPI_KEY") or ""
        self.base_url, self.timeout = base_url.rstrip("/"), timeout

    def __call__(self, model, messages, max_tokens, temperature=0.4):
        import httpx   # лениво: тесты работают с подменой и не требуют httpx
        if not self._key: raise LLMPermanent("PROXYAPI_KEY is not set")
        try:
            r = httpx.post(f"{self.base_url}/chat/completions", timeout=self.timeout,
                           headers={"Authorization": f"Bearer {self._key}"},
                           json={"model": model, "messages": messages, "max_tokens": max_tokens, "temperature": temperature})
        except Exception as e:
            raise LLMTransient(redact(type(e).__name__)) from None
        if r.status_code == 429 or r.status_code >= 500: raise LLMTransient(f"http {r.status_code}")
        if r.status_code >= 400: raise LLMPermanent(f"http {r.status_code}")
        try:
            j = r.json(); u = j.get("usage") or {}
            return (j["choices"][0]["message"]["content"] or ""), int(u.get("prompt_tokens") or 0), int(u.get("completion_tokens") or 0)
        except Exception:
            raise LLMTransient("bad response body") from None
