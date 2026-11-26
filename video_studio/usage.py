"""Внутренняя статистика расходов на модели. Видна только владельцу, пользователю стоимость не показывается.
Стоимость считается по ФАКТИЧЕСКИМ токенам и ценам из конфигурации (VIDEO_PRICING_JSON); нет цены — пишутся только токены."""
import json, os, time

DDL = [
    """CREATE TABLE IF NOT EXISTS llm_usage (
        id INTEGER PRIMARY KEY AUTOINCREMENT, ts INTEGER NOT NULL, user_id INTEGER, mode TEXT DEFAULT '', task_id TEXT DEFAULT '',
        level TEXT DEFAULT '', model TEXT DEFAULT '', tier TEXT DEFAULT '', tokens_in INTEGER DEFAULT 0, tokens_out INTEGER DEFAULT 0,
        cost_micro INTEGER, reason TEXT DEFAULT '')""",
    "CREATE INDEX IF NOT EXISTS idx_llm_usage_ts ON llm_usage (ts)",
    "CREATE INDEX IF NOT EXISTS idx_llm_usage_task ON llm_usage (task_id)",
]


def init_usage_schema(conn):
    for q in DDL: conn.execute(q)
    conn.commit()


def tier_of_model(model):
    m = (model or "").lower()
    return "opus" if "opus" in m else "sonnet" if "sonnet" in m else "haiku" if "haiku" in m else "other"


def load_pricing(env=None):
    try:
        d = json.loads((env or os.environ).get("VIDEO_PRICING_JSON", "") or "{}")
        return d if isinstance(d, dict) else {}
    except ValueError:
        return {}


def cost_micro(tier, tin, tout, pricing):
    p = (pricing or {}).get(tier)
    if not isinstance(p, dict) or "in" not in p or "out" not in p: return None     # цены не заданы — стоимость неизвестна, не выдумываем
    return int(round(tin * float(p["in"]) + tout * float(p["out"])))


def record_usage(conn, *, user_id, mode, task_id, level, model, tokens_in, tokens_out, reason, pricing=None, clock=time.time):
    tier = tier_of_model(model)
    conn.execute("INSERT INTO llm_usage (ts,user_id,mode,task_id,level,model,tier,tokens_in,tokens_out,cost_micro,reason) VALUES (?,?,?,?,?,?,?,?,?,?,?)",
                 (int(clock()), user_id, mode, task_id, level, model, tier, int(tokens_in or 0), int(tokens_out or 0),
                  cost_micro(tier, int(tokens_in or 0), int(tokens_out or 0), pricing), (reason or "")[:200]))
    conn.commit()


def report(conn, since=0):
    """Сводка для владельца: средняя стоимость задач по режимам/уровням, доля Opus, самые дорогие задачи."""
    def one(q, p=()): return conn.execute(q, p).fetchall()
    per_task = one("SELECT task_id, mode, level, SUM(cost_micro), SUM(tokens_in), SUM(tokens_out), COUNT(*), "
                   "SUM(CASE WHEN tier='opus' THEN 1 ELSE 0 END) FROM llm_usage WHERE ts>=? AND task_id!='' GROUP BY task_id", (since,))
    groups = {}
    for tid, mode, level, cost, tin, tout, calls, opus in per_task:
        g = groups.setdefault((mode, level), {"tasks": 0, "cost": 0, "unknown": 0, "calls": 0, "opus_calls": 0})
        g["tasks"] += 1; g["calls"] += calls; g["opus_calls"] += opus
        if cost is None: g["unknown"] += 1
        else: g["cost"] += cost
    avg = {f"{m}/{l}": {"tasks": g["tasks"], "avg_cost_micro": (g["cost"] // max(1, g["tasks"] - g["unknown"])) if g["tasks"] > g["unknown"] else None,
                        "calls": g["calls"], "opus_calls": g["opus_calls"]} for (m, l), g in groups.items()}
    tot = one("SELECT COALESCE(SUM(cost_micro),0), COALESCE(SUM(CASE WHEN tier='opus' THEN cost_micro ELSE 0 END),0) FROM llm_usage WHERE ts>=?", (since,))[0]
    top = one("SELECT task_id, mode, level, SUM(cost_micro) c FROM llm_usage WHERE ts>=? AND task_id!='' GROUP BY task_id HAVING c IS NOT NULL ORDER BY c DESC LIMIT 10", (since,))
    reasons = one("SELECT reason, COUNT(*) FROM llm_usage WHERE ts>=? AND tier='opus' GROUP BY reason ORDER BY 2 DESC LIMIT 10", (since,))
    return {"average_by_mode_level": avg, "total_cost_micro": tot[0], "opus_cost_micro": tot[1],
            "opus_share": (tot[1] / tot[0]) if tot[0] else None, "most_expensive": [dict(zip(("task_id", "mode", "level", "cost_micro"), r)) for r in top],
            "opus_reasons": [dict(zip(("reason", "calls"), r)) for r in reasons]}
