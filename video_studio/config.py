"""Настройки Video Studio. Всё, что связано с деньгами, НЕ имеет значений по умолчанию:
пока владелец не задал их, создание видео-задач отклоняется (cfg.ready() -> False)."""
import json, os
from dataclasses import dataclass, field

TIERS = ("haiku", "sonnet", "opus")


def _int(env, name, default):
    try: return int(env.get(name, default))
    except (TypeError, ValueError): return default


def _float_or_none(env, name):
    v = env.get(name)
    if v in (None, ""): return None
    try: return float(v)
    except ValueError: return None


def _json(env, name, default):
    v = env.get(name)
    if not v: return default
    try:
        d = json.loads(v)
        return d if isinstance(d, type(default)) else default
    except ValueError:
        return default


@dataclass
class VideoConfig:
    enabled: bool = False                    # VIDEO_ENABLED, по умолчанию ВЫКЛЮЧЕНО
    # --- деньги: значений по умолчанию нет, утверждает владелец ---
    weight: int = None                       # VIDEO_JOB_WEIGHT: сколько единиц лимита списывается за задачу
    budget_usd: float = None                 # VIDEO_JOB_BUDGET_USD: потолок расходов на модели на одну задачу
    pricing: dict = None                     # VIDEO_PRICING_JSON: {"haiku":{"in":x,"out":y},...} USD за 1 млн токенов
    allowed_plans: list = None               # VIDEO_ALLOWED_PLANS: ["pro"]; None = любой активный платный тариф
    # --- технические лимиты (предложенные значения, владелец может изменить) ---
    max_calls: dict = field(default_factory=lambda: {"haiku": 4, "sonnet": 24, "opus": 4})
    max_fixes: int = 2
    max_scenes: int = 8
    max_total_seconds: int = 60
    max_scene_seconds: int = 12
    max_prompt_chars: int = 1500
    fps: int = 30
    width: int = 1280
    height: int = 720
    expect_audio: bool = False               # звук не реализован: проверка аудио выключена
    max_bytes: int = 45 * 1024 * 1024        # Bot API: sendVideo до 50 МБ
    max_attempts: int = 4                    # запусков одной задачи воркером (включая после падений)
    max_resumes: int = 3                     # ручных resume/retry пользователем
    stage_retries: int = 2
    job_seconds: int = 1800
    lease_seconds: int = 300
    scene_render_timeout: int = 150
    resume_window: int = 86400               # через сутки неоконченная задача закрывается с возвратом
    max_workdir_bytes: int = 2 * 1024 ** 3
    black_min_seconds: float = 0.5
    complex_scenes: int = 5                  # раскадровка от N сцен считается сложной (Opus-проверка)
    workdir: str = "/tmp/holo_video"

    def ready(self):
        miss = []
        if not isinstance(self.weight, int) or self.weight <= 0: miss.append("VIDEO_JOB_WEIGHT")
        if not self.budget_usd or self.budget_usd <= 0: miss.append("VIDEO_JOB_BUDGET_USD")
        p = self.pricing or {}
        if not all(isinstance(p.get(t), dict) and "in" in p[t] and "out" in p[t] for t in TIERS):
            miss.append("VIDEO_PRICING_JSON")
        return (not miss), miss

    @classmethod
    def from_env(cls, env=None):
        env = os.environ if env is None else env
        c = cls()
        c.enabled = str(env.get("VIDEO_ENABLED", "0")).strip().lower() in ("1", "true", "yes", "on")
        w = env.get("VIDEO_JOB_WEIGHT")
        c.weight = int(w) if (w or "").strip().isdigit() else None
        c.budget_usd = _float_or_none(env, "VIDEO_JOB_BUDGET_USD")
        c.pricing = _json(env, "VIDEO_PRICING_JSON", {}) or None
        pl = _json(env, "VIDEO_ALLOWED_PLANS", [])
        c.allowed_plans = pl or None
        c.max_calls = {**c.max_calls, **_json(env, "VIDEO_MAX_CALLS_JSON", {})}
        for name, attr in (("VIDEO_MAX_FIXES", "max_fixes"), ("VIDEO_MAX_SCENES", "max_scenes"),
                           ("VIDEO_MAX_TOTAL_SECONDS", "max_total_seconds"), ("VIDEO_FPS", "fps"),
                           ("VIDEO_WIDTH", "width"), ("VIDEO_HEIGHT", "height"),
                           ("VIDEO_MAX_ATTEMPTS", "max_attempts"), ("VIDEO_MAX_RESUMES", "max_resumes"),
                           ("VIDEO_JOB_SECONDS", "job_seconds"), ("VIDEO_MAX_BYTES", "max_bytes")):
            setattr(c, attr, _int(env, name, getattr(c, attr)))
        c.workdir = env.get("VIDEO_WORKDIR", c.workdir)
        return c
