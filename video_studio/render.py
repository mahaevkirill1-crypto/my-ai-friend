"""Render Engine: сцена (HTML) -> клип через headless Chromium + FFmpeg; сборка клипов в итоговый файл.
Пользовательский/сгенерированный JS выполняется ТОЛЬКО в отдельном процессе render_runner (offline-контекст,
CSP, чистое окружение без секретов, лимиты CPU/файлов, таймаут)."""
import json, os, re, resource, subprocess, sys

FORBIDDEN = [r"\bfetch\s*\(", r"XMLHttpRequest", r"WebSocket", r"EventSource", r"navigator\.sendBeacon", r"\bimport\s*\(",
             r"<script[^>]+src\s*=", r"<link[^>]+href\s*=\s*[\"']?https?:", r"https?://", r"\beval\s*\(", r"new\s+Function",
             r"document\.cookie", r"localStorage", r"sessionStorage", r"window\.open", r"\blocation\s*[.=]", r"<iframe", r"<object", r"<embed"]
_FORBIDDEN_RE = [re.compile(p, re.I) for p in FORBIDDEN]
MAX_SCENE_CODE = 40_000


class SceneCodeError(Exception):
    pass


class RenderTransient(Exception):
    pass


def validate_scene_code(code):
    """Статическая проверка (вторая линия обороны; основная — изоляция Chromium)."""
    if not isinstance(code, str) or len(code) < 50: raise SceneCodeError("слишком короткий код сцены")
    if len(code) > MAX_SCENE_CODE: raise SceneCodeError("код сцены слишком большой")
    if "__setTime" not in code: raise SceneCodeError("нет функции window.__setTime(t)")
    for rx in _FORBIDDEN_RE:
        m = rx.search(code)
        if m: raise SceneCodeError(f"запрещённая конструкция: {m.group(0)[:30]}")
    return code


def _limits(cpu_seconds, fsize):
    def f():
        resource.setrlimit(resource.RLIMIT_CPU, (cpu_seconds, cpu_seconds))
        resource.setrlimit(resource.RLIMIT_FSIZE, (fsize, fsize))
        os.setsid()
    return f


class ChromiumFfmpegRenderer:
    def __init__(self, cfg, python=sys.executable):
        self.cfg, self.python = cfg, python

    def render_scene(self, code, duration, out_path):
        validate_scene_code(code)
        os.makedirs(os.path.dirname(out_path), exist_ok=True)
        payload = json.dumps({"html": code, "duration": duration, "fps": self.cfg.fps, "w": self.cfg.width,
                              "h": self.cfg.height, "out": out_path})
        env = {"PATH": os.environ.get("PATH", "/usr/bin:/bin"), "HOME": os.path.dirname(out_path),   # БЕЗ секретов из окружения
               "PLAYWRIGHT_BROWSERS_PATH": os.environ.get("PLAYWRIGHT_BROWSERS_PATH", "")}
        t = self.cfg.scene_render_timeout
        try:
            r = subprocess.run([self.python, "-m", "video_studio.render_runner"], input=payload, capture_output=True, text=True,
                               timeout=t, env=env, preexec_fn=_limits(t * 2, self.cfg.max_bytes * 3))
        except subprocess.TimeoutExpired:
            raise RenderTransient("render timeout") from None
        if r.returncode != 0 or not os.path.isfile(out_path):
            raise RenderTransient(("render failed: " + (r.stderr or "")[-200:]).strip())
        return out_path

    def concat(self, clips, out_path):
        return concat_clips(clips, out_path, self.cfg)


def concat_clips(clips, out_path, cfg, timeout=300):
    lst = out_path + ".txt"
    with open(lst, "w") as f:
        for c in clips: f.write("file '" + c.replace("'", "'\\''") + "'\n")
    r = subprocess.run(["ffmpeg", "-y", "-v", "error", "-f", "concat", "-safe", "0", "-i", lst, "-c:v", "libx264", "-pix_fmt", "yuv420p",
                        "-r", str(cfg.fps), "-movflags", "+faststart", "-an", out_path], capture_output=True, text=True, timeout=timeout)
    try: os.remove(lst)
    except OSError: pass
    if r.returncode != 0: raise RenderTransient("concat failed: " + (r.stderr or "")[-200:])
    return out_path
