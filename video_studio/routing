"""Умный выбор модели по реальной сложности задачи (без платных вызовов: эвристика на сервере).
Правила: Haiku — вопросы; Sonnet — основной исполнитель; Opus — только режимы Code/Video и только для сложных частей.
Итоговое слово за ModelRouter (подписка читается из БД при каждом вызове); здесь только РЕШЕНИЕ «какой уровень нужен»."""
import re

# признаки высокой сложности (каждый совпавший сигнал добавляет баллы)
COMPLEX_SIGNALS = [
    (r"webgl|three\.?js|threejs|babylon|шейдер|shader|glsl|raymarch|ray.?trac", 3, "WebGL/3D-графика"),
    (r"3d|3д|трёхмерн|трехмерн", 2, "3D"),
    (r"галактик|вселенн|солнечн\w+ систем|n-?body|гравитац", 2, "сложная визуальная система"),
    (r"тысяч\w* частиц|\d{4,}\s*частиц|частиц\w* (?:и )?физик|физик\w+ движени|physics|rapier|cannon", 3, "физика/частицы"),
    (r"архитектур|микросервис|масштабируем|высоконагруж|event.?sourc", 2, "архитектура"),
    (r"оптимизаци\w+ (?:под )?(?:мобильн|производительн)|производительност\w+|60 ?fps|fps|утечк\w+ памят|memory leak", 3, "производительность"),
    (r"алгоритм\w* (?:поиска|маршрут|сжати|шифр)|динамическ\w+ программир|граф\w* алгоритм|a\*|dijkstra|ml-?модел|нейросет\w+ (?:с нуля|своя)", 3, "сложный алгоритм"),
    (r"multiplayer|мультиплеер|websocket|реальн\w+ времени.*синхрон|crdt|webrtc", 3, "реальное время/сеть"),
    (r"webassembly|wasm|web ?worker|offscreencanvas|gpu", 2, "низкоуровневая оптимизация"),
    (r"игр[ауы] (?:с|на) физик|игровой движок|шахмат\w+ (?:движок|ai)|симуляци\w+", 2, "игра/симуляция"),
]
HARD_WORDS = re.compile(r"серьёзн|серьезн|критичн|сложн|нестандартн|экспертн|продвинут|сложнейш|комплексн", re.I)
BUILD_VERBS = re.compile(r"созд|сдела|напиш|построй|разработ|реализу|сгенер|собер|build|make|create|write|implement|исправ|почин|добав|измени|переделай|fix|refactor|рефактор", re.I)
BUILD_OBJECTS = re.compile(r"сайт|лендинг|страниц|приложени|игр[уаы]|скрипт|бот|компонент|форм[уы]|калькулятор|код|функци|html|css|javascript|js\b|python|react|api|проект|анимаци|верстк|сцен|3d|3д|визуализац|симуляц|галактик|шейдер|частиц|эффект|демо|модел", re.I)
SITE_WORDS = re.compile(r"сайт|лендинг|страниц|html|css|верстк|визитк|анимаци|3d|3д|галактик|сцен|игр[уаы]|шейдер|webgl|three|частиц|визуализац|интерфейс|калькулятор|дашборд|веб-?приложени", re.I)
VIDEO_WORDS = re.compile(r"\bвидео\b|видеоролик|ролик|клип\b|анимированн\w+ видео|mp4|раскадровк|монтаж", re.I)
COMPLEX_THRESHOLD = 4        # сумма баллов, с которой задача считается сложной
NORMAL_THRESHOLD = 1


def assess(text, is_edit=False, errors_in_text=False):
    """Возвращает {"level": simple|normal|complex, "score": n, "reasons": [...], "is_build": bool, "is_video": bool}."""
    t = (text or "")[:6000]
    low = t.lower()
    score, reasons = 0, []
    for rx, pts, why in COMPLEX_SIGNALS:
        if re.search(rx, low):
            score += pts; reasons.append(why)
    if HARD_WORDS.search(low) and score:
        score += 1
    if len(t) > 1500 and score: score += 1                   # развёрнутое ТЗ с техническими требованиями
    is_build = bool(BUILD_VERBS.search(low) and BUILD_OBJECTS.search(low))
    is_video = bool(VIDEO_WORDS.search(low))
    if score >= COMPLEX_THRESHOLD: level = "complex"
    elif is_build or score >= NORMAL_THRESHOLD or (is_edit and BUILD_VERBS.search(low)): level = "normal"
    else: level = "simple"
    return {"level": level, "score": score, "reasons": reasons[:4], "is_build": is_build, "is_video": is_video, "is_site": bool(SITE_WORDS.search(low))}


def decide(mode, text, *, is_edit=False, is_pro=False, opus_used=0, opus_cap=2):
    """Решение об уровне модели. mode: chat|code|video.
    Возвращает {"tier": haiku|sonnet|opus, "role": ..., "reason": str, "assessment": {...}, "redirect": None|"code"|"video"}.
    Opus в chat невозможен ни при каких условиях; в code — только PRO, только complex, и не больше opus_cap раз на задачу."""
    a = assess(text, is_edit=is_edit)
    out = {"assessment": a, "redirect": None}
    if mode == "chat":
        if a["is_video"] and not a["is_build"] and re.search(r"созд|сдела|сними|сгенер|make|create", (text or "").lower()):
            out.update(tier="haiku", role="chat", reason="video_request_in_chat", redirect="video")
        elif a["level"] == "complex" and is_pro:
            out.update(tier="haiku", role="chat", reason="complex_task_belongs_to_code", redirect="code")   # Chat честно отправляет в Code
        elif a["is_build"] or a["level"] != "simple":
            out.update(tier="sonnet", role="code", reason="build_or_technical_in_chat")
        else:
            out.update(tier="haiku", role="chat", reason="simple_question")
        return out
    if mode == "code":
        if not is_pro:
            out.update(tier="haiku", role="chat", reason="no_access")
        elif a["level"] == "complex" and opus_used < opus_cap:
            out.update(tier="opus", role="code_expert", reason="complex: " + ", ".join(a["reasons"] or ["высокая сложность"]))
        elif a["is_build"] or a["level"] != "simple":
            out.update(tier="sonnet", role="code", reason="normal_implementation")
        else:
            out.update(tier="haiku", role="chat", reason="simple_question")
        return out
    out.update(tier="haiku", role="chat", reason="video_mode_not_available")
    return out
