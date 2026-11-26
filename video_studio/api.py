"""HTTP-маршруты /video/*. Авторизация — существующий validate_init_data; тариф — только из БД на сервере."""
import asyncio, hashlib, time

from fastapi import APIRouter, Body
from fastapi.responses import JSONResponse

from .jobs import UPSELL_TEXT, VideoDenied

HTTP = {"not_paid": 402, "limit": 402, "plan_not_allowed": 402, "active_job_exists": 409, "bad_prompt": 400,
        "not_found": 404, "not_resumable": 409, "resume_limit": 409, "access_unverifiable": 503,
        "not_configured": 503, "video_disabled": 503, "child_limit": 409}


def deny_response(e, pro=None):
    body = {"status": "error", "reason": e.reason, "text": e.text or UPSELL_TEXT}
    if e.reason in ("not_paid", "limit", "plan_not_allowed"): body["limit_reached"] = True
    if pro is not None: body["pro"] = pro
    body.update({k: v for k, v in e.extra.items() if k == "job_id"})
    return JSONResponse(body, status_code=HTTP.get(e.reason, 400))


def ask_idem_key(uid, text, now):
    return hashlib.sha256(f"{uid}|{text}|{int(now) // 60}".encode()).hexdigest()[:32]


def create_from_ask(d, conn, uid, text, pro):
    """Вызывается из /ask. Возвращает (payload, status). Подписка перепроверяется внутри jobs.create."""
    try:
        r = d["jobs"].create(conn, uid, text, ask_idem_key(uid, text, time.time()))
    except VideoDenied as e:
        resp = deny_response(e, pro)
        import json
        return json.loads(resp.body), resp.status_code
    return {"text": "Принял! Делаю видео, слежу за этапами.", "emotion": "happy", "pro": d["pro_info"](conn, uid, int(time.time())),
            "video_job": {"job_id": r["job_id"]}}, 200


def build_router(get_deps):
    router = APIRouter(prefix="/video")

    async def _call(data, fn):
        d = get_deps()
        if not d["cfg"].enabled:
            return JSONResponse({"status": "error", "reason": "video_disabled", "text": "Video Studio пока недоступна."}, status_code=503)
        info = d["validate_init_data"](data.get("init_data", ""))
        if not info:
            return JSONResponse({"status": "error", "text": "Ошибка авторизации. Открой приложение из Telegram."}, status_code=401)
        uid = info["user"]["id"]                      # единственный источник личности — подписанные данные Telegram
        def work():
            conn = d["get_conn"]()
            try:
                return fn(d, conn, uid)
            except VideoDenied as e:
                try: pro = d["pro_info"](conn, uid, int(time.time()))
                except Exception: pro = None
                return deny_response(e, pro)
        return await asyncio.to_thread(work)

    @router.post("/create")
    async def create(data: dict = Body(...)):
        def fn(d, conn, uid):
            r = d["jobs"].create(conn, uid, str(data.get("prompt", "")), str(data.get("idem_key", "")))
            return {"status": "ok", **r}
        return await _call(data, fn)

    @router.post("/status")
    async def status(data: dict = Body(...)):
        def fn(d, conn, uid):
            return {"status": "ok", "job": d["jobs"].status(conn, uid, data.get("job_id") or None, data.get("since", 0))}
        return await _call(data, fn)

    @router.post("/resume")
    async def resume(data: dict = Body(...)):
        return await _call(data, lambda d, conn, uid: {"status": "ok", **d["jobs"].resume(conn, uid, str(data.get("job_id", "")))})

    @router.post("/retry")
    async def retry(data: dict = Body(...)):
        return await _call(data, lambda d, conn, uid: {"status": "ok", **d["jobs"].retry(conn, uid, str(data.get("job_id", "")))})

    @router.post("/cancel")
    async def cancel(data: dict = Body(...)):
        return await _call(data, lambda d, conn, uid: {"status": "ok", **d["jobs"].cancel(conn, uid, str(data.get("job_id", "")))})

    return router
