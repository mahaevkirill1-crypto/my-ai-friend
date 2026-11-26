"""Подключение Video Studio к main.py одной функцией. Ничего не включает: VIDEO_ENABLED по умолчанию выключен."""
import os

from .api import build_router, create_from_ask
from .config import VideoConfig
from .jobs import JobManager
from .model_router import ModelRouter
from .schema import init_video_schema


class VideoStudio:
    def __init__(self, models, pro_info, validate_init_data, get_db_connection, env=None):
        self.cfg = VideoConfig.from_env(env)
        self.router = ModelRouter(models, pro_info, self.cfg.allowed_plans)
        self.jobs = JobManager(self.cfg, self.router)
        self.deps = {"cfg": self.cfg, "jobs": self.jobs, "pro_info": pro_info,
                     "validate_init_data": validate_init_data, "get_conn": get_db_connection}

    def init_schema(self, conn): init_video_schema(conn)
    def api_router(self): return build_router(lambda: self.deps)
    def create_from_ask(self, conn, uid, text, pro): return create_from_ask(self.deps, conn, uid, text, pro)
