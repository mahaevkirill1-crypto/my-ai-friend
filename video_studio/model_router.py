"""ЕДИНАЯ серверная политика выбора моделей (чат, сайты и Video Studio).

Правила:
  * Тариф всегда читается из БД на сервере при КАЖДОМ вызове route(); кэша нет, данные клиента не принимаются.
  * Opus разрешён только: контекст "video" + роль режиссёрского уровня + активная платная подписка.
  * Любая ошибка проверки подписки => Opus запрещён (fail-closed).
  * Free: Haiku (чат) и Sonnet (сайты/техника). Video Studio и Opus недоступны.
  * Модель с "opus" в имени никогда не выдаётся для не-Opus роли (защита от ошибочной конфигурации env).
"""
import time

HAIKU, SONNET, OPUS = "haiku", "sonnet", "opus"

ROLE_TIER = {
    "chat": HAIKU, "simple": HAIKU,
    "code": SONNET, "scene_code": SONNET, "fix": SONNET, "render": SONNET,
    "concept": OPUS, "direction": OPUS, "storyboard": OPUS, "qc_complex": OPUS,
}
OPUS_ROLES = frozenset(r for r, t in ROLE_TIER.items() if t == OPUS)


class ModelDenied(Exception):
    """reason: unknown_role | opus_not_allowed | not_paid | access_unverifiable | plan_not_allowed | misconfigured"""
    def __init__(self, reason, detail=""):
        super().__init__(f"{reason}: {detail}" if detail else reason)
        self.reason = reason


class ModelRouter:
    def __init__(self, models, sub_check, allowed_plans=None, clock=time.time):
        """models: {"haiku": "...", "sonnet": "...", "opus": "..."}
        sub_check(conn, uid, now) -> dict с ключами active (bool) и plan_id; читает БД, исключения допустимы."""
        self.models = dict(models)
        self.sub_check = sub_check
        self.allowed_plans = list(allowed_plans) if allowed_plans else None
        self.clock = clock

    def _sub(self, conn, uid, now):
        try:
            info = self.sub_check(conn, uid, now)
        except Exception as e:  # БД недоступна, битые данные и т.п.
            raise ModelDenied("access_unverifiable", type(e).__name__) from e
        if not isinstance(info, dict): raise ModelDenied("access_unverifiable", "bad_sub_info")
        return info

    def video_access(self, conn, uid, now=None):
        """Доступ к Video Studio: только активная платная подписка (и разрешённый план, если задан)."""
        info = self._sub(conn, uid, int(now if now is not None else self.clock()))
        if not info.get("active"): raise ModelDenied("not_paid")
        if self.allowed_plans is not None and info.get("plan_id") not in self.allowed_plans:
            raise ModelDenied("plan_not_allowed")
        return info

    def route(self, conn, uid, role, context="chat", now=None):
        tier = ROLE_TIER.get(role)
        if tier is None: raise ModelDenied("unknown_role", str(role))
        now = int(now if now is not None else self.clock())
        if context == "video":
            self.video_access(conn, uid, now)            # любая модель внутри видео — только платным
        elif tier == OPUS:
            raise ModelDenied("opus_not_allowed", "opus only inside video studio")
        # (чат/сайты: haiku и sonnet доступны всем тарифам, подписку проверять не нужно)
        model = self.models.get(tier)
        if not model: raise ModelDenied("misconfigured", f"no model for {tier}")
        if tier != OPUS and "opus" in model.lower():
            raise ModelDenied("misconfigured", f"{tier} role resolved to an opus model")
        return model

    def tier_of(self, role):
        return ROLE_TIER.get(role)
