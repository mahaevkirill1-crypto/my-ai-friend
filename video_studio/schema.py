"""Таблицы Video Studio. Все с префиксом video_, существующие таблицы Holo Friend не меняются.
Используется только pro_state (UPDATE requests_left), как и в основном коде."""

DDL = [
    """CREATE TABLE IF NOT EXISTS video_jobs (
        job_id TEXT PRIMARY KEY, user_id INTEGER NOT NULL, status TEXT NOT NULL, stage TEXT DEFAULT '',
        prompt TEXT NOT NULL, idem_key TEXT NOT NULL, weight INTEGER DEFAULT 0,
        attempts INTEGER DEFAULT 0, resume_count INTEGER DEFAULT 0,
        state_json TEXT DEFAULT '{}', error TEXT DEFAULT '', failure_kind TEXT DEFAULT '',
        created_at INTEGER NOT NULL, updated_at INTEGER NOT NULL, deadline INTEGER DEFAULT 0,
        lease_owner TEXT DEFAULT '', lease_until INTEGER DEFAULT 0,
        delivery_status TEXT DEFAULT 'none', delivery_attempts INTEGER DEFAULT 0, delivery_claim_at INTEGER DEFAULT 0,
        tg_message_id INTEGER DEFAULT 0, video_path TEXT DEFAULT '',
        parent_job_id TEXT DEFAULT '', children INTEGER DEFAULT 0)""",
    # один пользователь = одна активная задача (атомарно, на уровне БД)
    "CREATE UNIQUE INDEX IF NOT EXISTS uq_video_active ON video_jobs (user_id) WHERE status IN ('queued','running')",
    # идемпотентность: повтор запроса с тем же ключом не создаёт новую задачу
    "CREATE UNIQUE INDEX IF NOT EXISTS uq_video_idem ON video_jobs (user_id, idem_key)",
    "CREATE INDEX IF NOT EXISTS idx_video_status ON video_jobs (status, created_at)",
    """CREATE TABLE IF NOT EXISTS video_events (
        id INTEGER PRIMARY KEY AUTOINCREMENT, job_id TEXT NOT NULL, ts INTEGER NOT NULL,
        kind TEXT NOT NULL, stage TEXT DEFAULT '', label TEXT DEFAULT '')""",
    "CREATE INDEX IF NOT EXISTS idx_video_events_job ON video_events (job_id, id)",
    """CREATE TABLE IF NOT EXISTS video_costs (
        id INTEGER PRIMARY KEY AUTOINCREMENT, job_id TEXT NOT NULL, ts INTEGER NOT NULL, stage TEXT DEFAULT '',
        role TEXT DEFAULT '', model TEXT DEFAULT '', tier TEXT DEFAULT '',
        tokens_in INTEGER DEFAULT 0, tokens_out INTEGER DEFAULT 0, cost_micro INTEGER DEFAULT 0, ok INTEGER DEFAULT 1)""",
    "CREATE INDEX IF NOT EXISTS idx_video_costs_job ON video_costs (job_id)",
    # журнал списаний/возвратов: PK (job_id, kind) физически не даёт списать или вернуть дважды
    """CREATE TABLE IF NOT EXISTS video_ledger (
        job_id TEXT NOT NULL, kind TEXT NOT NULL, units INTEGER NOT NULL, ts INTEGER NOT NULL,
        PRIMARY KEY (job_id, kind))""",
]


def init_video_schema(conn):
    for sql in DDL:
        conn.execute(sql)
    conn.commit()
