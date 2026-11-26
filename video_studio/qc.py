"""Quality Control: ffprobe + ffmpeg. Проверяет разрешение, FPS, длительность, аудио (если ожидается),
чёрные кадры, повреждённые кадры/ошибки декодирования, размер, возможность воспроизведения."""
import json, os, re, subprocess
from dataclasses import dataclass


@dataclass
class Issue:
    code: str            # missing|too_big|unplayable|corrupt|resolution|fps|duration|no_audio|black
    detail: str = ""
    scene: int = None    # индекс сцены (0..n-1), если проблему можно привязать к сцене

    def text(self):
        return f"{self.code}: {self.detail}" + (f" (сцена {self.scene + 1})" if self.scene is not None else "")


def _run(cmd, timeout=120):
    return subprocess.run(cmd, capture_output=True, text=True, timeout=timeout)


def probe(path):
    r = _run(["ffprobe", "-v", "error", "-print_format", "json", "-show_streams", "-show_format", path])
    if r.returncode != 0: return None
    try: return json.loads(r.stdout)
    except ValueError: return None


def _fps(rate):
    try:
        a, b = rate.split("/")
        return float(a) / float(b) if float(b) else 0.0
    except Exception:
        return 0.0


BLACK_RE = re.compile(r"black_start:([\d.]+)\s+black_end:([\d.]+)")


def parse_blackdetect(stderr):
    return [(float(a), float(b)) for a, b in BLACK_RE.findall(stderr or "")]


def scene_at(t, durations):
    acc = 0.0
    for i, d in enumerate(durations):
        acc += d
        if t < acc - 1e-6: return i
    return len(durations) - 1 if durations else None


def check_video(path, *, width, height, fps, durations, max_bytes, expect_audio=False, black_min=0.5):
    """Возвращает список Issue (пусто = всё хорошо)."""
    if not os.path.isfile(path): return [Issue("missing", "файл не создан")]
    size = os.path.getsize(path)
    if size <= 0: return [Issue("missing", "пустой файл")]
    issues = []
    if size > max_bytes: issues.append(Issue("too_big", f"{size} > {max_bytes} байт"))
    info = probe(path)
    v = next((s for s in (info or {}).get("streams", []) if s.get("codec_type") == "video"), None)
    if not v: return issues + [Issue("unplayable", "нет видеопотока / ffprobe не прочитал файл")]
    dec = _run(["ffmpeg", "-v", "error", "-i", path, "-f", "null", "-"], timeout=300)
    if dec.returncode != 0 or (dec.stderr or "").strip():
        issues.append(Issue("corrupt", (dec.stderr or "ошибка декодирования").strip()[:200]))
    if (int(v.get("width", 0)), int(v.get("height", 0))) != (width, height):
        issues.append(Issue("resolution", f"{v.get('width')}x{v.get('height')} вместо {width}x{height}"))
    if abs(_fps(v.get("avg_frame_rate", "0/1")) - fps) > 0.5:
        issues.append(Issue("fps", f"{v.get('avg_frame_rate')} вместо {fps}"))
    expected = sum(durations)
    try: dur = float((info.get("format") or {}).get("duration") or v.get("duration") or 0)
    except ValueError: dur = 0.0
    if abs(dur - expected) > max(0.5, expected * 0.05):
        issues.append(Issue("duration", f"{dur:.1f}с вместо {expected:.1f}с"))
    if expect_audio and not any(s.get("codec_type") == "audio" for s in info["streams"]):
        issues.append(Issue("no_audio", "звуковая дорожка ожидалась, но её нет"))
    bd = _run(["ffmpeg", "-v", "info", "-i", path, "-vf", f"blackdetect=d={black_min}:pix_th=0.10", "-an", "-f", "null", "-"], timeout=300)
    for start, end in parse_blackdetect(bd.stderr):
        issues.append(Issue("black", f"чёрные кадры {start:.1f}–{end:.1f}с", scene_at(start, durations)))
    return issues
