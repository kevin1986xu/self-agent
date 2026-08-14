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
    role TEXT NOT NULL DEFAULT 'member',      -- member / admin
    created_at TIMESTAMPTZ DEFAULT now(),
    UNIQUE (channel, channel_user_id)
);
ALTER TABLE user_identity ADD COLUMN IF NOT EXISTS role TEXT NOT NULL DEFAULT 'member';
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
CREATE TABLE IF NOT EXISTS gateway_message (
    id BIGSERIAL PRIMARY KEY,
    ts TIMESTAMPTZ DEFAULT now(),
    project TEXT NOT NULL,
    channel TEXT NOT NULL,
    conversation_key TEXT NOT NULL,
    direction TEXT NOT NULL,                  -- in / out
    user_id BIGINT,
    content TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS gateway_message_conv
    ON gateway_message (project, channel, conversation_key, id);
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


def log_message(project: str, channel: str, conversation_key: str, direction: str,
                content: str, user_id: int | None = None) -> None:
    """渠道消息留痕（R 消息记录）：进出双向、按项目/会话可查。"""
    with _conn() as c:
        c.execute(
            "INSERT INTO gateway_message (project, channel, conversation_key, direction, user_id, content)"
            " VALUES (%s,%s,%s,%s,%s,%s)",
            (project, channel, conversation_key, direction, user_id, content[:20000]))


def list_messages(project: str | None = None, channel: str | None = None,
                  conversation_key: str | None = None, limit: int = 100) -> list[dict]:
    with _conn() as c:
        rows = c.execute(
            """SELECT ts::text, project, channel, conversation_key, direction, user_id, content
               FROM gateway_message
               WHERE (%s::text IS NULL OR project=%s) AND (%s::text IS NULL OR channel=%s)
                 AND (%s::text IS NULL OR conversation_key=%s)
               ORDER BY id DESC LIMIT %s""",
            (project, project, channel, channel, conversation_key, conversation_key,
             min(limit, 500))).fetchall()
    keys = ["ts", "project", "channel", "conversation_key", "direction", "user_id", "content"]
    return [dict(zip(keys, r)) for r in rows]


def get_role(user_id: int) -> str:
    with _conn() as c:
        row = c.execute("SELECT role FROM user_identity WHERE user_id=%s", (user_id,)).fetchone()
    return row[0] if row else "member"


def set_role(user_id: int, role: str) -> None:
    assert role in ("member", "admin")
    with _conn() as c:
        n = c.execute("UPDATE user_identity SET role=%s WHERE user_id=%s", (role, user_id)).rowcount
    if not n:
        raise ValueError(f"用户不存在: {user_id}")


def list_users() -> list[dict]:
    with _conn() as c:
        rows = c.execute(
            "SELECT user_id, channel, channel_user_id, display_name, role, created_at::date"
            " FROM user_identity ORDER BY user_id").fetchall()
    keys = ["user_id", "channel", "channel_user_id", "display_name", "role", "since"]
    return [dict(zip(keys, r)) for r in rows]


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
