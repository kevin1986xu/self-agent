"""统一身份（M2-10，docs/09 阶段1 声明式口径）。

渠道用户 → 统一 user_id 映射；首次出现自动登记。审批与审计经 user_id 追责到人。
"""

from __future__ import annotations

import os

_DDL = """
CREATE TABLE IF NOT EXISTS user_identity (
    user_id BIGSERIAL PRIMARY KEY,
    channel TEXT NOT NULL,
    channel_user_id TEXT NOT NULL,
    display_name TEXT,
    created_at TIMESTAMPTZ DEFAULT now(),
    UNIQUE (channel, channel_user_id)
);
CREATE TABLE IF NOT EXISTS gateway_approval (
    id BIGSERIAL PRIMARY KEY,
    thread_id TEXT NOT NULL,
    user_id BIGINT REFERENCES user_identity(user_id),
    action_summary TEXT NOT NULL,
    status TEXT NOT NULL DEFAULT 'pending',   -- pending/approved/rejected/expired
    decided_by BIGINT,
    created_at TIMESTAMPTZ DEFAULT now(),
    decided_at TIMESTAMPTZ
);
"""


def _conn():
    import psycopg

    dsn = os.environ.get("DATABASE_URL", "")
    if not dsn:
        raise RuntimeError("需要 DATABASE_URL")
    conn = psycopg.connect(dsn, autocommit=True)
    conn.execute(_DDL)
    return conn


def get_or_create_user(channel: str, channel_user_id: str, display_name: str | None = None) -> int:
    with _conn() as c:
        row = c.execute(
            """INSERT INTO user_identity (channel, channel_user_id, display_name)
               VALUES (%s,%s,%s)
               ON CONFLICT (channel, channel_user_id)
               DO UPDATE SET display_name = COALESCE(EXCLUDED.display_name, user_identity.display_name)
               RETURNING user_id""",
            (channel, channel_user_id, display_name)).fetchone()
    return row[0]


def create_approval(thread_id: str, user_id: int, action_summary: str) -> int:
    with _conn() as c:
        return c.execute(
            "INSERT INTO gateway_approval (thread_id, user_id, action_summary) VALUES (%s,%s,%s) RETURNING id",
            (thread_id, user_id, action_summary)).fetchone()[0]


def decide_approval(approval_id: int, *, approve: bool, decided_by: int) -> dict | None:
    """置决策。仅 pending 可决策（防重放）。返回审批记录或 None。"""
    with _conn() as c:
        row = c.execute(
            """UPDATE gateway_approval
               SET status=%s, decided_by=%s, decided_at=now()
               WHERE id=%s AND status='pending'
               RETURNING thread_id, action_summary""",
            ("approved" if approve else "rejected", decided_by, approval_id)).fetchone()
    if not row:
        return None
    return {"thread_id": row[0], "action_summary": row[1]}
