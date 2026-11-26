"""Dedicated Render Worker: отдельный процесс/контейнер. Запуск: python -m video_studio.worker
Берёт задачи из очереди (таблица video_jobs, lease), сам НЕ принимает HTTP-запросов."""
import logging, os, shutil, socket, time, uuid

from .config import VideoConfig
from .jobs import JobManager
from .model_router import ModelRouter
from .subscription import pro_state_info

log = logging.getLogger("holo.video.worker")


class Worker:
    def __init__(self, jobs, pipeline, conn_factory, owner=None, clock=time.time, sleep=time.sleep, poll=3.0):
        self.jobs, self.pipeline, self.conn_factory = jobs, pipeline, conn_factory
        self.owner = owner or f"{socket.gethostname()}-{uuid.uuid4().hex[:6]}"
        self.clock, self.sleep, self.poll, self._last_hk = clock, sleep, poll, 0

    def run_once(self):
        conn = self.conn_factory()
        if self.clock() - self._last_hk > 300:
            self._last_hk = self.clock()
            try: self.jobs.housekeeping(conn); self.prune_workdir(conn)
            except Exception: log.exception("housekeeping failed")
        job = self.jobs.claim(conn, self.owner)
        if not job: return False
        log.info("worker %s took job %s attempt %s", self.owner, job["job_id"], job["attempts"])
        self.pipeline.run(job)
        return True

    def prune_workdir(self, conn):
        wd = self.jobs.cfg.workdir
        if not os.path.isdir(wd): return
        for name in os.listdir(wd):
            r = conn.execute("SELECT status FROM video_jobs WHERE job_id=?", (name,)).fetchone()
            if not r or r[0] in ("done", "cancelled") or (r[0] == "failed"):
                if not r or r[0] in ("done", "cancelled"): shutil.rmtree(os.path.join(wd, name), ignore_errors=True)

    def run_forever(self):
        while True:
            try:
                if not self.run_once(): self.sleep(self.poll)
            except Exception:
                log.exception("worker loop error"); self.sleep(self.poll * 2)


def main():
    logging.basicConfig(level=logging.INFO)
    for n in ("httpx", "httpcore"): logging.getLogger(n).setLevel(logging.WARNING)    # в URL запросов бывает токен бота
    import libsql
    from .delivery import DeliveryManager
    from .llm import ProxyApiLLM
    from .pipeline import VideoPipeline
    from .qc import check_video
    from .render import ChromiumFfmpegRenderer
    cfg = VideoConfig.from_env()
    if not cfg.enabled: raise SystemExit("VIDEO_ENABLED is off: worker does not start")
    ok, miss = cfg.ready()
    if not ok: raise SystemExit("video config incomplete: " + ", ".join(miss))
    models = {"haiku": os.getenv("MODEL_HAIKU", "anthropic/claude-haiku-5-5"),
              "sonnet": os.getenv("MODEL_FREE", "anthropic/claude-sonnet-5-5"),
              "opus": os.getenv("MODEL_PRO", "anthropic/claude-opus-5-5")}
    router = ModelRouter(models, pro_state_info, cfg.allowed_plans)
    jobs = JobManager(cfg, router)
    def conn_factory():
        return libsql.connect(database=os.environ["TURSO_DATABASE_URL"], auth_token=os.environ["TURSO_AUTH_TOKEN"])
    pipeline = VideoPipeline(cfg, jobs, router, ProxyApiLLM(), ChromiumFfmpegRenderer(cfg), check_video,
                             DeliveryManager(os.environ["BOT_TOKEN"], log=log.warning), conn_factory)
    Worker(jobs, pipeline, conn_factory).run_forever()


if __name__ == "__main__":
    main()
