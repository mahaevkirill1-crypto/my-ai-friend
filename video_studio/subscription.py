"""Проверка подписки для воркера (он не может импортировать main.py).
Предикат ОБЯЗАН совпадать с pro_info() из main.py: expires_at > now AND requests_left > 0.
Совпадение проверяет tests/test_subscription_parity.py."""

PLAN_UNKNOWN = ""


def pro_state_info(conn, uid, now):
    row = conn.execute("SELECT expires_at, requests_left, plan FROM pro_state WHERE user_id = ?", (uid,)).fetchone()
    if not row:
        return {"active": False, "left": 0, "plan_id": ""}
    expires_at, left, plan = row[0] or 0, row[1] or 0, row[2] or ""
    active = expires_at > now and left > 0
    return {"active": bool(active), "left": max(0, left) if active else 0, "plan_id": plan}
