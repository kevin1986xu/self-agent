"""任务模板 / Playbook + 定时任务（M3-3/4，R18/R14，技术方案 4.10）。

模板 = 参数化 prompt（{param} 槽位）+ 投递通道；可挂 cron 定时自动发起。
与定时任务共用一张表：cron 为空 = 纯手动模板；非空 = 定时任务。
"""

from __future__ import annotations

import logging
import os
from datetime import datetime, timedelta, timezone

logger = logging.getLogger(__name__)

_DDL = """
CREATE TABLE IF NOT EXISTS playbook (
    id BIGSERIAL PRIMARY KEY,
    name TEXT UNIQUE NOT NULL,
    prompt_template TEXT NOT NULL,
    default_params JSONB NOT NULL DEFAULT '{}',
    cron TEXT,                                -- 空=手动模板；如 '0 9 * * 1'
    channel TEXT NOT NULL DEFAULT 'local',
    conversation_key TEXT NOT NULL DEFAULT 'playbook',
    enabled BOOLEAN NOT NULL DEFAULT true,
    last_run_at TIMESTAMPTZ,
    created_at TIMESTAMPTZ DEFAULT now()
)
"""


def _conn():
    import psycopg

    dsn = os.environ.get("DATABASE_URL", "")
    if not dsn:
        raise RuntimeError("需要 DATABASE_URL")
    conn = psycopg.connect(dsn, autocommit=True)
    conn.execute(_DDL)
    return conn


_COLS = ["id", "name", "prompt_template", "default_params", "cron",
         "channel", "conversation_key", "enabled", "last_run_at", "created_at"]


def list_playbooks() -> list[dict]:
    with _conn() as c:
        rows = c.execute(
            "SELECT id,name,prompt_template,default_params,cron,channel,"
            "conversation_key,enabled,last_run_at::text,created_at::text"
            " FROM playbook ORDER BY id").fetchall()
    return [dict(zip(_COLS, r)) for r in rows]


def upsert(spec: dict) -> dict:
    import json as _json

    with _conn() as c:
        row = c.execute(
            """INSERT INTO playbook (name, prompt_template, default_params, cron,
                                     channel, conversation_key, enabled)
               VALUES (%(name)s, %(prompt_template)s, %(default_params)s, %(cron)s,
                       %(channel)s, %(conversation_key)s, %(enabled)s)
               ON CONFLICT (name) DO UPDATE SET
                 prompt_template=EXCLUDED.prompt_template,
                 default_params=EXCLUDED.default_params, cron=EXCLUDED.cron,
                 channel=EXCLUDED.channel, conversation_key=EXCLUDED.conversation_key,
                 enabled=EXCLUDED.enabled
               RETURNING id""",
            {"name": spec["name"], "prompt_template": spec["prompt_template"],
             "default_params": _json.dumps(spec.get("default_params") or {}, ensure_ascii=False),
             "cron": spec.get("cron") or None,
             "channel": spec.get("channel", "local"),
             "conversation_key": spec.get("conversation_key", "playbook"),
             "enabled": spec.get("enabled", True)}).fetchone()
    return {"id": row[0]}


def delete(pb_id: int) -> None:
    with _conn() as c:
        c.execute("DELETE FROM playbook WHERE id=%s", (pb_id,))


def render(pb: dict, params: dict | None) -> str:
    merged = {**(pb.get("default_params") or {}), **(params or {})}
    text = pb["prompt_template"]
    for k, v in merged.items():
        text = text.replace("{" + k + "}", str(v))
    return text


def mark_run(pb_id: int) -> None:
    with _conn() as c:
        c.execute("UPDATE playbook SET last_run_at=now() WHERE id=%s", (pb_id,))


def due_playbooks(now: datetime | None = None) -> list[dict]:
    """到期的定时模板：cron 非空、enabled、且上次运行早于最近一个 cron 触发点。"""
    from croniter import croniter

    now = now or datetime.now(timezone.utc)
    due = []
    for pb in list_playbooks():
        if not pb["cron"] or not pb["enabled"]:
            continue
        try:
            prev_fire = croniter(pb["cron"], now).get_prev(datetime)
        except (ValueError, KeyError):
            logger.warning("playbook %s cron 非法: %s", pb["name"], pb["cron"])
            continue
        last = pb["last_run_at"]
        last_dt = datetime.fromisoformat(last) if last else None
        if last_dt is None or last_dt < prev_fire.replace(tzinfo=timezone.utc):
            # 容忍窗口：只补最近 10 分钟内的触发点，避免久停后风暴
            if now - prev_fire.replace(tzinfo=timezone.utc) <= timedelta(minutes=10):
                due.append(pb)
    return due
