"""Delivery Manager: отправка готового видео в Telegram. Идемпотентна: повторный вызов не шлёт и не генерирует заново."""
import time

from .llm import redact

SENT, BLOCKED, RETRYABLE, PERMANENT = "sent", "blocked", "retryable", "permanent"
STALE_SENDING = 600


def telegram_send_video(token, uid, path, caption, meta, post=None):
    """post(url, data, files) -> (status_code, json_dict); подменяется в тестах. Токен нигде не логируется."""
    if post is None:
        import httpx
        def post(url, data, files):
            with open(files["video"], "rb") as f:
                r = httpx.post(url, data=data, files={"video": ("video.mp4", f, "video/mp4")}, timeout=120)
            try: return r.status_code, r.json()
            except ValueError: return r.status_code, {}
    data = {"chat_id": uid, "caption": caption[:900], "supports_streaming": "true", **{k: str(v) for k, v in meta.items()}}
    return post(f"https://api.telegram.org/bot{token}/sendVideo", data, {"video": path})


class DeliveryManager:
    def __init__(self, token, post=None, sleep=time.sleep, clock=time.time, max_tries=4, max_sleep=30, log=None):
        self._token, self.post, self.sleep, self.clock = token, post, sleep, clock
        self.max_tries, self.max_sleep, self.log = max_tries, max_sleep, log

    def _log(self, msg):
        if self.log: self.log(redact(msg))

    def _send_with_retries(self, uid, path, caption, meta):
        for attempt in range(1, self.max_tries + 1):
            try:
                code, j = telegram_send_video(self._token, uid, path, caption, meta, self.post)
            except Exception as e:
                self._log(f"telegram network error: {type(e).__name__}")
                code, j = 599, {}
            if code == 200 and j.get("ok"):
                return SENT, int((j.get("result") or {}).get("message_id") or 0)
            desc = str(j.get("description", "")).lower()
            if code == 403 or (code == 400 and ("chat not found" in desc or "bot was blocked" in desc)):
                return BLOCKED, 0
            if code == 429:
                ra = int((j.get("parameters") or {}).get("retry_after", 2))
                if attempt == self.max_tries: break
                self.sleep(min(ra + 1, self.max_sleep)); continue
            if code >= 500:
                if attempt == self.max_tries: break
                self.sleep(min(2 ** attempt, self.max_sleep)); continue
            self._log(f"telegram permanent error {code}")
            return PERMANENT, 0                      # 400 (кроме blocked), 413, 401 и т.п.
        return RETRYABLE, 0

    def deliver(self, conn, job, path, caption, meta):
        """Атомарно «занимает» отправку; если уже отправлено — возвращает прежний результат без нового вызова API."""
        jid, now = job["job_id"], int(self.clock())
        cur = conn.execute("UPDATE video_jobs SET delivery_status='sending', delivery_claim_at=?, delivery_attempts=delivery_attempts+1 "
                           "WHERE job_id=? AND (delivery_status IN ('none','failed') OR (delivery_status='sending' AND delivery_claim_at < ?))",
                           (now, jid, now - STALE_SENDING))
        conn.commit()
        if cur.rowcount != 1:
            st = conn.execute("SELECT delivery_status, tg_message_id FROM video_jobs WHERE job_id=?", (jid,)).fetchone()
            if st and st[0] in ("sent", "blocked"): return ("already_" + st[0]), st[1]
            return RETRYABLE, 0                      # кто-то другой прямо сейчас отправляет
        status, mid = self._send_with_retries(job["user_id"], path, caption, meta)
        db = {SENT: "sent", BLOCKED: "blocked", RETRYABLE: "failed", PERMANENT: "failed_permanent"}[status]
        conn.execute("UPDATE video_jobs SET delivery_status=?, tg_message_id=? WHERE job_id=?", (db, mid, jid))
        conn.commit()
        return status, mid
