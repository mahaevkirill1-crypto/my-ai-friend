"""Video Job Manager: создание, списание, очередь (lease), resume/retry, возвраты, события."""
import json, time, uuid

from .model_router import ModelDenied

ACTIVE = ("queued", "running")
RESUMABLE_KINDS = {"transient", "timeout", "internal", "subscription", "delivery_retry"}
JOB_COLS = ("job_id,user_id,status,stage,prompt,idem_key,weight,attempts,resume_count,state_json,error,failure_kind,"
            "created_at,updated_at,deadline,lease_owner,lease_until,delivery_status,delivery_attempts,"
            "delivery_claim_at,tg_message_id,video_path,parent_job_id,children")
_KEYS = JOB_COLS.split(",")

UPSELL_TEXT = "Video Studio доступна на платных тарифах. Выбери тариф, и я смогу сделать видео."


class VideoDenied(Exception):
    def __init__(self, reason, text="", **extra):
        super().__init__(reason)
        self.reason, self.text, self.extra = reason, text, extra


def _unique_violation(e):
    m = str(e).lower()
    return "unique" in m or "constraint" in m


def _row(row):
    return dict(zip(_KEYS, row)) if row else None


class JobManager:
    def __init__(self, cfg, router, clock=time.time):
        self.cfg, self.router, self.clock = cfg, router, clock

    # ---------- чтение ----------
    def load(self, conn, job_id):
        return _row(conn.execute(f"SELECT {JOB_COLS} FROM video_jobs WHERE job_id = ?", (job_id,)).fetchone())

    def load_owned(self, conn, uid, job_id):
        j = self.load(conn, job_id)
        return j if j and j["user_id"] == uid else None

    def spent_micro(self, conn, job_id):
        r = conn.execute("SELECT COALESCE(SUM(cost_micro),0) FROM video_costs WHERE job_id = ?", (job_id,)).fetchone()
        return int(r[0] or 0)

    # ---------- события (Activity Stream) ----------
    def add_event(self, conn, job_id, kind, stage="", label="", commit=True):
        conn.execute("INSERT INTO video_events (job_id, ts, kind, stage, label) VALUES (?,?,?,?,?)",
                     (job_id, int(self.clock()), kind, stage, label))
        if commit: conn.commit()

    # ---------- доступ ----------
    def _require_access(self, conn, uid):
        """Сервер сам читает подписку из БД. Любая неопределённость = отказ."""
        try:
            return self.router.video_access(conn, uid, int(self.clock()))
        except ModelDenied as e:
            if e.reason == "access_unverifiable":
                raise VideoDenied("access_unverifiable", "Не удалось проверить тариф. Попробуй чуть позже.")
            raise VideoDenied("not_paid", UPSELL_TEXT)

    def _require_ready(self):
        if not self.cfg.enabled: raise VideoDenied("video_disabled", "Video Studio пока недоступна.")
        ok, miss = self.cfg.ready()
        if not ok: raise VideoDenied("not_configured", "Video Studio ещё не настроена.", missing=miss)

    # ---------- создание + списание (одна транзакция) ----------
    def create(self, conn, uid, prompt, idem_key):
        self._require_ready()
        self._require_access(conn, uid)
        prompt = (prompt or "").strip()
        if not (3 <= len(prompt) <= self.cfg.max_prompt_chars):
            raise VideoDenied("bad_prompt", f"Опиши видео текстом (до {self.cfg.max_prompt_chars} символов).")
        idem_key = (idem_key or "")[:100] or uuid.uuid4().hex
        ex = conn.execute("SELECT job_id FROM video_jobs WHERE user_id = ? AND idem_key = ?", (uid, idem_key)).fetchone()
        if ex: return {"job_id": ex[0], "existing": True}
        now = int(self.clock())
        job_id = uuid.uuid4().hex
        try:
            conn.execute("INSERT INTO video_jobs (job_id,user_id,status,prompt,idem_key,weight,created_at,updated_at,deadline) "
                         "VALUES (?,?,?,?,?,?,?,?,?)",
                         (job_id, uid, "queued", prompt, idem_key, self.cfg.weight, now, now, now + self.cfg.job_seconds))
            cur = conn.execute("UPDATE pro_state SET requests_left = requests_left - ? "
                               "WHERE user_id = ? AND requests_left >= ? AND expires_at > ?",
                               (self.cfg.weight, uid, self.cfg.weight, now))
            if cur.rowcount != 1:
                conn.rollback()
                raise VideoDenied("limit", "Не хватает лимита для видео. Пополни тариф.")
            conn.execute("INSERT INTO video_ledger (job_id, kind, units, ts) VALUES (?,?,?,?)",
                         (job_id, "charge", self.cfg.weight, now))
            conn.execute("INSERT INTO video_events (job_id, ts, kind, stage, label) VALUES (?,?,?,?,?)",
                         (job_id, now, "queued", "", "Задача принята"))
            conn.commit()
        except VideoDenied:
            raise
        except Exception as e:
            try: conn.rollback()
            except Exception: pass
            if not _unique_violation(e): raise
            ex = conn.execute("SELECT job_id FROM video_jobs WHERE user_id = ? AND idem_key = ?", (uid, idem_key)).fetchone()
            if ex: return {"job_id": ex[0], "existing": True}
            act = conn.execute("SELECT job_id FROM video_jobs WHERE user_id = ? AND status IN ('queued','running')", (uid,)).fetchone()
            raise VideoDenied("active_job_exists", "У тебя уже идёт создание видео. Дождись результата.",
                              job_id=act[0] if act else None)
        return {"job_id": job_id, "existing": False}

    # ---------- resume / retry ----------
    def resume(self, conn, uid, job_id):
        """Возобновление без повторного списания. Заново проверяет подписку, владельца, лимиты попыток."""
        self._require_ready()
        self._require_access(conn, uid)
        j = self.load_owned(conn, uid, job_id)
        if not j: raise VideoDenied("not_found", "Задача не найдена.")
        if j["status"] not in ("failed", "blocked"):
            raise VideoDenied("not_resumable", "Эту задачу нельзя возобновить.")
        if j["failure_kind"] not in RESUMABLE_KINDS:
            raise VideoDenied("not_resumable", "Эту задачу нельзя возобновить.")
        if j["resume_count"] >= self.cfg.max_resumes:
            raise VideoDenied("resume_limit", "Лимит повторных попыток исчерпан.")
        now = int(self.clock())
        try:
            cur = conn.execute("UPDATE video_jobs SET status='queued', resume_count=resume_count+1, error='', failure_kind='', "
                               "delivery_status=CASE WHEN delivery_status='blocked' THEN 'failed' ELSE delivery_status END, "
                               "lease_owner='', lease_until=0, deadline=?, updated_at=? "
                               "WHERE job_id=? AND user_id=? AND status IN ('failed','blocked')",
                               (now + self.cfg.job_seconds, now, job_id, uid))
            conn.commit()
        except Exception as e:
            try: conn.rollback()
            except Exception: pass
            if _unique_violation(e):
                raise VideoDenied("active_job_exists", "У тебя уже идёт создание видео.")
            raise
        if cur.rowcount != 1: raise VideoDenied("not_resumable", "Задачу уже возобновили.")
        self.add_event(conn, job_id, "resumed", j["stage"], "Продолжаю с того же места")
        return {"job_id": job_id}

    retry = resume   # retry и resume — один и тот же защищённый путь

    def cancel(self, conn, uid, job_id):
        j = self.load_owned(conn, uid, job_id)
        if not j: raise VideoDenied("not_found", "Задача не найдена.")
        if j["status"] in ("done", "cancelled"): return {"job_id": job_id, "status": j["status"]}
        now = int(self.clock())
        conn.execute("UPDATE video_jobs SET status='cancelled', failure_kind='cancelled', updated_at=?, lease_owner='' "
                     "WHERE job_id=? AND user_id=? AND status IN ('queued','running','failed','blocked')", (now, job_id, uid))
        conn.commit()
        self.refund_for_job(conn, job_id)
        self.add_event(conn, job_id, "cancelled", "", "Задача отменена")
        return {"job_id": job_id, "status": "cancelled"}

    def spawn_child(self, conn, parent_id, purpose):
        """Дочерняя задача: НЕ наследует права родителя. Подписка владельца проверяется заново, списаний нет,
        расходы пишутся в журнал родителя (общий бюджет), число детей ограничено."""
        p = self.load(conn, parent_id)
        if not p: raise VideoDenied("not_found", "Родительская задача не найдена.")
        self._require_ready()
        self._require_access(conn, p["user_id"])
        if p["parent_job_id"]: raise VideoDenied("nested_child", "Вложенные дочерние задачи запрещены.")
        if p["children"] >= 8: raise VideoDenied("child_limit", "Слишком много дочерних задач.")
        cur = conn.execute("UPDATE video_jobs SET children = children + 1 WHERE job_id = ? AND children < 8", (parent_id,))
        conn.commit()
        if cur.rowcount != 1: raise VideoDenied("child_limit", "Слишком много дочерних задач.")
        return {"parent_job_id": parent_id, "user_id": p["user_id"], "purpose": purpose, "cost_job_id": parent_id}

    # ---------- очередь (воркер) ----------
    def claim(self, conn, owner):
        now = int(self.clock())
        cur = conn.execute(
            "UPDATE video_jobs SET status='running', lease_owner=?, lease_until=?, attempts=attempts+1, updated_at=? "
            "WHERE job_id = (SELECT job_id FROM video_jobs WHERE status='queued' OR (status='running' AND lease_until < ?) "
            "ORDER BY created_at LIMIT 1) AND (status='queued' OR (status='running' AND lease_until < ?))",
            (owner, now + self.cfg.lease_seconds, now, now, now))
        conn.commit()
        if cur.rowcount != 1: return None
        row = conn.execute(f"SELECT {JOB_COLS} FROM video_jobs WHERE lease_owner=? AND status='running' ORDER BY updated_at DESC LIMIT 1",
                           (owner,)).fetchone()
        j = _row(row)
        if j and j["attempts"] > self.cfg.max_attempts:
            self.fail(conn, j["job_id"], owner, "attempts", "Превышено число запусков задачи.")
            return None
        return j

    def renew(self, conn, job_id, owner):
        now = int(self.clock())
        cur = conn.execute("UPDATE video_jobs SET lease_until=?, updated_at=? WHERE job_id=? AND lease_owner=? AND status='running'",
                           (now + self.cfg.lease_seconds, now, job_id, owner))
        conn.commit()
        return cur.rowcount == 1

    def save_state(self, conn, job_id, owner, state, stage):
        cur = conn.execute("UPDATE video_jobs SET state_json=?, stage=?, updated_at=? WHERE job_id=? AND lease_owner=? AND status='running'",
                           (json.dumps(state, ensure_ascii=False), stage, int(self.clock()), job_id, owner))
        conn.commit()
        return cur.rowcount == 1

    # ---------- завершение ----------
    def finish_ok(self, conn, job_id, owner):
        conn.execute("UPDATE video_jobs SET status='done', stage='done', updated_at=?, lease_owner='' WHERE job_id=? AND lease_owner=?",
                     (int(self.clock()), job_id, owner))
        conn.commit()
        self.add_event(conn, job_id, "done", "done", "Готово")

    def fail(self, conn, job_id, owner, kind, error, status="failed"):
        """kind из RESUMABLE_KINDS — задачу можно возобновить (возврата пока нет); иначе — финал и возврат."""
        resumable = kind in RESUMABLE_KINDS
        conn.execute("UPDATE video_jobs SET status=?, failure_kind=?, error=?, updated_at=?, lease_owner='' "
                     "WHERE job_id=? AND (lease_owner=? OR ?='')", (status, kind, (error or "")[:300], int(self.clock()), job_id, owner, owner))
        conn.commit()
        self.add_event(conn, job_id, "error", "", (error or "Ошибка")[:200])
        if not resumable: self.refund_for_job(conn, job_id)

    def refund_for_job(self, conn, job_id):
        """Возврат пропорционально НЕпотраченному бюджету. Идемпотентно: второй раз ничего не вернёт."""
        j = self.load(conn, job_id)
        if not j or not j["weight"]: return 0
        has_charge = conn.execute("SELECT 1 FROM video_ledger WHERE job_id=? AND kind='charge'", (job_id,)).fetchone()
        if not has_charge: return 0
        if j["delivery_status"] in ("sent", "blocked"): return 0     # видео уже сделано/доставлено — возврата нет
        budget = int((self.cfg.budget_usd or 0) * 1_000_000)
        spent = self.spent_micro(conn, job_id)
        ratio = 1.0 if budget <= 0 else max(0.0, 1.0 - min(1.0, spent / budget))
        units = int(j["weight"] * ratio)
        now = int(self.clock())
        try:
            cur = conn.execute("INSERT OR IGNORE INTO video_ledger (job_id, kind, units, ts) VALUES (?,?,?,?)", (job_id, "refund", units, now))
            if cur.rowcount == 1 and units > 0:
                conn.execute("UPDATE pro_state SET requests_left = requests_left + ? WHERE user_id = ?", (units, j["user_id"]))
            conn.commit()
            return units if cur.rowcount == 1 else 0
        except Exception:
            try: conn.rollback()
            except Exception: pass
            raise

    def housekeeping(self, conn):
        """Закрывает «зависшие» возобновляемые задачи по истечении окна resume (с возвратом)."""
        now = int(self.clock())
        rows = conn.execute("SELECT job_id FROM video_jobs WHERE status IN ('failed','blocked') AND failure_kind IN "
                            "('transient','timeout','internal','subscription','delivery_retry') AND updated_at < ?",
                            (now - self.cfg.resume_window,)).fetchall()
        for (jid,) in rows:
            conn.execute("UPDATE video_jobs SET failure_kind='expired' WHERE job_id=?", (jid,))
            conn.commit()
            self.refund_for_job(conn, jid)
            self.add_event(conn, jid, "error", "", "Время на возобновление истекло")
        return len(rows)

    # ---------- статус для клиента ----------
    def status(self, conn, uid, job_id=None, since=0):
        if job_id:
            j = self.load_owned(conn, uid, job_id)
        else:
            r = conn.execute("SELECT job_id FROM video_jobs WHERE user_id=? AND status IN ('queued','running') LIMIT 1", (uid,)).fetchone()
            j = self.load(conn, r[0]) if r else None
        if not j: return None
        evs = conn.execute("SELECT id, kind, stage, label FROM video_events WHERE job_id=? AND id>? ORDER BY id LIMIT 100",
                           (j["job_id"], int(since or 0))).fetchall()
        return {"job_id": j["job_id"], "status": j["status"], "stage": j["stage"], "failure_kind": j["failure_kind"],
                "error": j["error"] if j["status"] in ("failed", "blocked") else "", "delivery": j["delivery_status"],
                "resumable": j["status"] in ("failed", "blocked") and j["failure_kind"] in RESUMABLE_KINDS and j["resume_count"] < self.cfg.max_resumes,
                "events": [{"id": e[0], "kind": e[1], "stage": e[2], "label": e[3]} for e in evs]}
