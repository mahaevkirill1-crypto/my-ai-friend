"""Оркестратор стадий: analyze -> concept -> storyboard -> scenes -> render -> qc(+fix) -> deliver.
Каждая стадия: guard (lease, отмена, дедлайн, подписка из БД) -> реальное событие -> работа -> чекпоинт в БД.
События Activity Stream создаются ТОЛЬКО при фактическом начале стадии."""
import json, logging, os, shutil, time

from .budget import BudgetExceeded, CostTracker, est_tokens
from .llm import LLMPermanent, LLMTransient, redact
from .model_router import ModelDenied
from .render import RenderTransient, SceneCodeError, validate_scene_code

log = logging.getLogger("holo.video")

LABELS = {"analyze": "Анализирую запрос", "concept": "Формирую концепцию", "storyboard": "Создаю раскадровку",
          "scenes": "Разрабатываю сцены", "render": "Выполняю рендеринг", "qc": "Проверяю качество",
          "fix": "Исправляю обнаруженные проблемы", "deliver": "Отправляю готовое видео"}
ORDER = ("analyze", "concept", "storyboard", "scenes", "render", "qc", "deliver")


class Cancelled(Exception): pass
class LeaseLost(Exception): pass
class JobTimeout(Exception): pass
class TransientError(Exception): pass
class QcFailed(Exception): pass
class DeliveryRetry(Exception): pass
class DeliveryPermanent(Exception): pass


class InvalidOutput(Exception):
    def __init__(self, text): super().__init__(text); self.text = text


SCENE_CONTRACT = (
    "Ты пишешь ОДНУ сцену анимации как самодостаточный HTML (inline CSS и JS, без внешних ресурсов, без сети). "
    "Обязательно определи window.__setTime = function(t){...}, где t — время сцены в секундах; функция должна "
    "детерминированно рисовать кадр для момента t (никаких setInterval/requestAnimationFrame как источника времени). "
    "Размер кадра {w}x{h}. Запрещены: fetch, XMLHttpRequest, WebSocket, внешние ссылки http(s)://, eval, localStorage, iframe. "
    "Ответ: только код в блоке ```html ... ```.")


def extract_json(text):
    s = (text or "").strip()
    a, b = s.find("{"), s.rfind("}")
    if a < 0 or b <= a: raise ValueError("no json")
    return json.loads(s[a:b + 1])


def extract_html(text):
    t = text or ""
    if "```" in t:
        parts = t.split("```")
        for p in parts[1:]:
            body = p[4:] if p.lower().startswith("html") else p
            if "__setTime" in body: return body.strip()
    return t.strip()


class Ctx:
    def __init__(self, conn, job, owner):
        self.conn, self.job, self.owner = conn, job, owner
        self.jid, self.uid = job["job_id"], job["user_id"]
        self.state = json.loads(job["state_json"] or "{}")
        self.state.setdefault("done", [])
        self.dir = None


class VideoPipeline:
    def __init__(self, cfg, jobs, router, llm, renderer, qc_check, delivery, conn_factory,
                 clock=time.time, sleep=time.sleep):
        self.cfg, self.jobs, self.router, self.llm = cfg, jobs, router, llm
        self.renderer, self.qc_check, self.delivery = renderer, qc_check, delivery
        self.conn_factory, self.clock, self.sleep = conn_factory, clock, sleep
        self.tracker = CostTracker(cfg, clock)

    # ------------------------------------------------------------ общие помощники
    def guard(self, c):
        row = c.conn.execute("SELECT status, lease_owner, deadline FROM video_jobs WHERE job_id=?", (c.jid,)).fetchone()
        if not row: raise LeaseLost()
        if row[0] == "cancelled": raise Cancelled()
        if row[0] != "running" or row[1] != c.owner: raise LeaseLost()
        if row[2] and self.clock() > row[2]: raise JobTimeout()
        self.jobs.renew(c.conn, c.jid, c.owner)
        self.router.video_access(c.conn, c.uid, int(self.clock()))      # подписка из БД перед каждой стадией

    def checkpoint(self, c, stage):
        if not self.jobs.save_state(c.conn, c.jid, c.owner, c.state, stage): raise LeaseLost()

    def note(self, c, label, kind="note", stage=""):
        self.jobs.add_event(c.conn, c.jid, kind, stage, label)

    def call_model(self, c, stage, role, messages, max_tokens):
        """ЕДИНСТВЕННАЯ точка вызова моделей в Video Studio. Router проверяет подписку при каждом вызове."""
        tier = self.router.tier_of(role)
        model = self.router.route(c.conn, c.uid, role, context="video", now=int(self.clock()))
        prompt = "\n".join(m["content"] for m in messages)
        self.tracker.reserve(c.conn, c.jid, tier, prompt, max_tokens)
        try:
            text, tin, tout = self.llm(model, messages, max_tokens)
        except LLMTransient as e:
            self.tracker.record(c.conn, c.jid, stage, role, model, tier, est_tokens(prompt), 0, ok=False)
            raise TransientError(redact(e)) from None
        except LLMPermanent as e:
            self.tracker.record(c.conn, c.jid, stage, role, model, tier, 0, 0, ok=False)
            raise InvalidOutput("Не удалось получить ответ от модели.") from None
        self.tracker.record(c.conn, c.jid, stage, role, model, tier, tin or est_tokens(prompt), tout or est_tokens(text))
        return text

    def with_retries(self, c, fn):
        for i in range(self.cfg.stage_retries + 1):
            try:
                return fn()
            except TransientError:
                if i >= self.cfg.stage_retries: raise
                self.guard(c)
                self.sleep(min(2 ** i, 10))

    def json_call(self, c, stage, role, system, user, max_tokens, validate):
        msgs = [{"role": "system", "content": system}, {"role": "user", "content": user}]
        for attempt in (0, 1):                        # одна попытка исправления формата, затем отказ
            text = self.with_retries(c, lambda: self.call_model(c, stage, role, msgs, max_tokens))
            try:
                return validate(extract_json(text))
            except (ValueError, KeyError, TypeError) as e:
                if attempt: raise InvalidOutput("Не удалось построить план видео. Попробуй переформулировать запрос.")
                msgs = msgs + [{"role": "assistant", "content": text[:2000]},
                               {"role": "user", "content": f"Формат неверен ({e}). Верни только корректный JSON."}]

    # ------------------------------------------------------------ стадии
    def st_analyze(self, c):
        def v(j):
            if not isinstance(j.get("ok"), bool): raise ValueError("ok")
            return j
        j = self.json_call(c, "analyze", "simple",
                           "Оцени запрос на короткое анимированное видео. Верни JSON {\"ok\":bool,\"reason\":str}. ok=false, если "
                           "запрос не про видео, бессмысленный или просит запрещённое/вредное содержимое.",
                           c.job["prompt"], 300, v)
        if not j["ok"]: raise InvalidOutput(str(j.get("reason") or "Не могу сделать такое видео.")[:200])
        c.state["analysis"] = j

    def st_concept(self, c):
        def v(j):
            if not isinstance(j.get("title"), str) or not isinstance(j.get("narrative"), str): raise ValueError("title/narrative")
            return j
        c.state["concept"] = self.json_call(
            c, "concept", "concept",
            "Ты креативный директор. Придумай концепцию короткого анимированного видео без звука. "
            "JSON: {\"title\":str,\"mood\":str,\"palette\":[str],\"narrative\":str}", c.job["prompt"], 1200, v)

    def st_storyboard(self, c):
        cfg = self.cfg
        def v(j):
            sc = j["scenes"]
            if not isinstance(sc, list) or not (1 <= len(sc) <= cfg.max_scenes): raise ValueError("число сцен")
            out = []
            for s in sc:
                d = float(s["duration"])
                if not (1 <= d <= cfg.max_scene_seconds): raise ValueError("длительность сцены")
                out.append({"duration": d, "visual": str(s["visual"])[:600], "text": str(s.get("text", ""))[:200]})
            if sum(s["duration"] for s in out) > cfg.max_total_seconds: raise ValueError("общая длительность")
            return {"scenes": out, "complexity": "complex" if (j.get("complexity") == "complex" or len(out) >= cfg.complex_scenes) else "simple"}
        c.state["storyboard"] = self.json_call(
            c, "storyboard", "storyboard",
            f"Ты режиссёр. Составь раскадровку: от 1 до {cfg.max_scenes} сцен, каждая 1–{cfg.max_scene_seconds} с, суммарно не более "
            f"{cfg.max_total_seconds} с. JSON: {{\"scenes\":[{{\"duration\":число,\"visual\":str,\"text\":str}}],\"complexity\":\"simple|complex\"}}",
            json.dumps({"prompt": c.job["prompt"], "concept": c.state["concept"]}, ensure_ascii=False), 2500, v)

    def _gen_scene(self, c, idx, extra=""):
        cfg, sc = self.cfg, c.state["storyboard"]["scenes"][idx]
        system = SCENE_CONTRACT.replace("{w}", str(cfg.width)).replace("{h}", str(cfg.height))
        user = json.dumps({"concept": c.state["concept"], "scene": sc, "index": idx + 1, "of": len(c.state["storyboard"]["scenes"])}, ensure_ascii=False) + extra
        msgs = [{"role": "system", "content": system}, {"role": "user", "content": user}]
        role = "fix" if extra else "scene_code"
        for attempt in (0, 1):
            text = self.with_retries(c, lambda: self.call_model(c, "fix" if extra else "scenes", role, msgs, 6000))
            code = extract_html(text)
            try:
                return validate_scene_code(code)
            except SceneCodeError as e:
                if attempt: raise InvalidOutput("Не получилось создать безопасную сцену. Попробуй изменить запрос.")
                msgs = msgs + [{"role": "assistant", "content": text[:1500]}, {"role": "user", "content": f"Код отклонён: {e}. Исправь."}]

    def st_scenes(self, c):
        codes = c.state.setdefault("scene_code", {})
        n = len(c.state["storyboard"]["scenes"])
        for i in range(n):
            if str(i) in codes: continue                # resume: готовые сцены не пересоздаём (и не оплачиваем повторно)
            self.guard(c)
            codes[str(i)] = self._gen_scene(c, i)
            self.checkpoint(c, "scenes")
            self.note(c, f"Сцена {i + 1} из {n} готова")

    def _render_missing(self, c):
        sbs, codes = c.state["storyboard"]["scenes"], c.state["scene_code"]
        dirty = set(c.state.get("dirty", []))
        c.dir = os.path.join(self.cfg.workdir, c.jid); os.makedirs(c.dir, exist_ok=True)
        if _dir_size(c.dir) > self.cfg.max_workdir_bytes: raise TransientError("workdir quota")
        clips = []
        for i, sc in enumerate(sbs):
            clip = os.path.join(c.dir, f"scene_{i}.mp4")
            if i in dirty and os.path.isfile(clip): os.remove(clip)
            if not os.path.isfile(clip):
                self.guard(c)
                try: self.with_retries(c, lambda: self._render_one(codes[str(i)], sc["duration"], clip))
                except SceneCodeError: raise InvalidOutput("Сцена не прошла проверку безопасности.")
                self.note(c, f"Сцена {i + 1} из {len(sbs)} отрендерена")
            clips.append(clip)
        final = os.path.join(c.dir, "final.mp4")
        try: self.renderer.concat(clips, final)
        except RenderTransient as e: raise TransientError(redact(e))
        c.state["dirty"], c.state["final"] = [], final

    def _render_one(self, code, duration, clip):
        try: self.renderer.render_scene(code, duration, clip)
        except RenderTransient as e: raise TransientError(redact(e))

    def st_render(self, c):
        self._render_missing(c)

    def _qc(self, c):
        durs = [s["duration"] for s in c.state["storyboard"]["scenes"]]
        return self.qc_check(c.state["final"], width=self.cfg.width, height=self.cfg.height, fps=self.cfg.fps, durations=durs,
                             max_bytes=self.cfg.max_bytes, expect_audio=self.cfg.expect_audio, black_min=self.cfg.black_min_seconds)

    def _opus_review(self, c):
        """Опциональный Opus-контроль для сложных проектов: проверяет код сцен против раскадровки (картинку не видит)."""
        sb = c.state["storyboard"]
        if c.state.get("opus_qc_done") or sb["complexity"] != "complex": return []
        c.state["opus_qc_done"] = True
        payload = {"storyboard": sb["scenes"], "scenes_code": {k: v[:2500] for k, v in c.state["scene_code"].items()}}
        def v(j):
            if not isinstance(j.get("approve"), bool): raise ValueError("approve")
            return j
        try:
            j = self.json_call(c, "qc", "qc_complex", "Ты старший контролёр качества. Сверь код сцен с раскадровкой: полноту, "
                               "соответствие тексту и длительности. JSON {\"approve\":bool,\"issues\":[{\"scene\":номер с 1,\"problem\":str}]}",
                               json.dumps(payload, ensure_ascii=False), 1200, v)
        except BudgetExceeded:
            self.note(c, "Расширенная проверка пропущена: исчерпан бюджет")
            return []
        from .qc import Issue
        out = []
        for it in (j.get("issues") or [])[:4]:
            try: out.append(Issue("review", str(it["problem"])[:200], int(it["scene"]) - 1))
            except (KeyError, ValueError, TypeError): continue
        return [i for i in out if 0 <= (i.scene if i.scene is not None else -1) < len(sb["scenes"])]

    def st_qc(self, c):
        issues = self._qc(c)
        if not issues: issues = self._opus_review(c)
        rounds = 0
        while issues:
            fixable = sorted({i.scene for i in issues if i.scene is not None})
            for i in issues: self.note(c, "Найдена проблема: " + i.text())
            if not fixable or rounds >= self.cfg.max_fixes:
                raise QcFailed("; ".join(i.text() for i in issues)[:250])
            rounds += 1
            self.guard(c); self.note(c, LABELS["fix"], "stage", "fix")
            for idx in fixable:
                prob = "; ".join(i.text() for i in issues if i.scene == idx)
                c.state["scene_code"][str(idx)] = self._gen_scene(c, idx, f"\nИСПРАВЬ найденную проблему: {prob}\nТекущий код:\n{c.state['scene_code'][str(idx)][:6000]}")
            c.state["dirty"] = fixable                      # перерисовываются ТОЛЬКО затронутые сцены
            self.checkpoint(c, "qc")
            self._render_missing(c)
            issues = self._qc(c)

    def st_deliver(self, c):
        path = c.state["final"]
        if not os.path.isfile(path): raise TransientError("final file missing")
        title = (c.state.get("concept") or {}).get("title", "Видео")
        meta = {"width": self.cfg.width, "height": self.cfg.height, "duration": int(sum(s["duration"] for s in c.state["storyboard"]["scenes"]))}
        res, _ = self.delivery.deliver(c.conn, c.job, path, title, meta)
        if res in ("sent", "already_sent", "blocked", "already_blocked"):
            c.state["delivered"] = res
            return
        if res == "retryable": raise DeliveryRetry()
        raise DeliveryPermanent()

    # ------------------------------------------------------------ запуск
    def run(self, job):
        conn = self.conn_factory()
        c = Ctx(conn, job, job["lease_owner"])
        done = c.state["done"]
        terminal = True
        try:
            for stage in ORDER:
                if stage == "render" and "render" in done and not os.path.isfile(c.state.get("final", "")):
                    for s in ("render", "qc"):
                        if s in done: done.remove(s)
                if stage in done: continue
                self.guard(c)
                self.note(c, LABELS[stage], "stage", stage)
                self.checkpoint(c, stage)
                c.state.pop("delivered", None)
                getattr(self, "st_" + stage)(c)
                if not (stage == "deliver" and c.state.get("delivered", "").endswith("blocked")):
                    done.append(stage)                  # заблокированная доставка стадией «выполнено» не считается: resume повторит только отправку
                self.checkpoint(c, stage)
            if c.state.get("delivered", "").endswith("blocked"):
                self.note(c, "Telegram не принял видео: открой чат с ботом, нажми /start и повтори отправку")
                self.jobs.fail(conn, c.jid, c.owner, "delivery_retry", "Бот заблокирован или чат не начат", status="failed")
                terminal = False
            else:
                self.jobs.finish_ok(conn, c.jid, c.owner)
            return "ok"
        except LeaseLost:
            terminal = False; return "lease_lost"
        except Cancelled:
            return "cancelled"
        except ModelDenied as e:
            terminal = False
            self.jobs.fail(conn, c.jid, c.owner, "subscription", "Нужна активная подписка, чтобы продолжить", status="blocked"); return "blocked"
        except BudgetExceeded as e:
            self.jobs.fail(conn, c.jid, c.owner, "budget", "Достигнут лимит расходов на видео"); return "budget"
        except JobTimeout:
            terminal = False; self.jobs.fail(conn, c.jid, c.owner, "timeout", "Превышено время создания видео"); return "timeout"
        except TransientError as e:
            terminal = False; self.jobs.fail(conn, c.jid, c.owner, "transient", "Временная ошибка. Можно повторить"); return "transient"
        except DeliveryRetry:
            terminal = False; self.jobs.fail(conn, c.jid, c.owner, "delivery_retry", "Не удалось отправить видео. Можно повторить отправку"); return "delivery_retry"
        except DeliveryPermanent:
            self.jobs.fail(conn, c.jid, c.owner, "delivery", "Telegram отклонил видео"); return "delivery"
        except QcFailed as e:
            self.jobs.fail(conn, c.jid, c.owner, "qc_failed", "Не удалось довести качество видео до нормы"); return "qc_failed"
        except InvalidOutput as e:
            self.jobs.fail(conn, c.jid, c.owner, "invalid", e.text); return "invalid"
        except Exception as e:
            terminal = False
            log.error("video job %s internal error: %s", c.jid, redact(repr(e)))
            self.jobs.fail(conn, c.jid, c.owner, "internal", "Внутренняя ошибка. Можно повторить"); return "internal"
        finally:
            if terminal: shutil.rmtree(os.path.join(self.cfg.workdir, c.jid), ignore_errors=True)


def _dir_size(path):
    t = 0
    for r, _, fs in os.walk(path):
        for f in fs:
            try: t += os.path.getsize(os.path.join(r, f))
            except OSError: pass
    return t
