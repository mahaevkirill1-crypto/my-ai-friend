"""Контроль расходов: бюджет задачи, лимиты вызовов по моделям, журнал стоимости."""
import time


class BudgetExceeded(Exception):
    pass


def est_tokens(text):
    return max(1, len(text or "") // 3)   # консервативно для русского текста


class CostTracker:
    def __init__(self, cfg, clock=time.time):
        self.cfg, self.clock = cfg, clock

    def _price(self, tier):
        p = (self.cfg.pricing or {}).get(tier)
        if not p: raise BudgetExceeded(f"no pricing for {tier}")
        return float(p["in"]), float(p["out"])     # USD за 1 млн токенов == микро-USD за токен

    def cost_micro(self, tier, tin, tout):
        pin, pout = self._price(tier)
        return int(round(tin * pin + tout * pout))

    def spent(self, conn, job_id):
        return int(conn.execute("SELECT COALESCE(SUM(cost_micro),0) FROM video_costs WHERE job_id=?", (job_id,)).fetchone()[0] or 0)

    def calls(self, conn, job_id, tier):
        return int(conn.execute("SELECT COUNT(*) FROM video_costs WHERE job_id=? AND tier=?", (job_id, tier)).fetchone()[0] or 0)

    def reserve(self, conn, job_id, tier, prompt_text, max_out):
        """Проверка ДО вызова: лимит числа вызовов и худший случай стоимости не должен выходить за бюджет."""
        cap = int((self.cfg.max_calls or {}).get(tier, 0))
        if self.calls(conn, job_id, tier) >= cap:
            raise BudgetExceeded(f"call cap for {tier} reached ({cap})")
        worst = self.cost_micro(tier, est_tokens(prompt_text), max_out)
        budget = int(self.cfg.budget_usd * 1_000_000)
        if self.spent(conn, job_id) + worst > budget:
            raise BudgetExceeded("job budget would be exceeded")

    def record(self, conn, job_id, stage, role, model, tier, tin, tout, ok=True):
        c = self.cost_micro(tier, tin, tout)
        conn.execute("INSERT INTO video_costs (job_id,ts,stage,role,model,tier,tokens_in,tokens_out,cost_micro,ok) VALUES (?,?,?,?,?,?,?,?,?,?)",
                     (job_id, int(self.clock()), stage, role, model, tier, tin, tout, c, 1 if ok else 0))
        conn.commit()
        return c
