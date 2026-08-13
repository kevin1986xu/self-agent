"""审计中间件（M1-5，技术方案 4.1.2 / docs/07 操作关）。

wrap_tool_call 钩子记录每次工具调用：工具名、参数摘要、结果状态、耗时。
落 Postgres 表 self_agent_audit（复用 Aegra 的库）；库不可用时降级为本地
JSONL 并告警——审计通道故障绝不阻断业务 run。
"""

from __future__ import annotations

import json
import logging
import os
import time
from pathlib import Path
from typing import Any

from langchain.agents.middleware.types import AgentMiddleware

from . import settings

logger = logging.getLogger(__name__)

_DDL = """
CREATE TABLE IF NOT EXISTS self_agent_audit (
    id BIGSERIAL PRIMARY KEY,
    ts TIMESTAMPTZ NOT NULL DEFAULT now(),
    tool TEXT NOT NULL,
    args_digest TEXT,
    status TEXT NOT NULL,
    elapsed_ms INTEGER,
    error TEXT
)
"""

_FALLBACK = Path(os.environ.get("AUDIT_FALLBACK_PATH", settings.PROJECT_ROOT / ".audit.jsonl"))


import re

_SECRET_KEYS = re.compile(
    r'("(?:confirm_token|api_key|apikey|token|secret|password|authorization)[^"]*"\s*:\s*")[^"]+(")',
    re.I,
)


def _digest(args: Any, limit: int = 500) -> str:
    try:
        blob = json.dumps(args, ensure_ascii=False, default=str)
    except (TypeError, ValueError):
        blob = str(args)
    blob = _SECRET_KEYS.sub(r"\1***\2", blob)  # 凭据/令牌类字段一律打码（M3-9）
    return blob[:limit]


class AuditMiddleware(AgentMiddleware):
    """记录 who/tool/args摘要/status/耗时。同步与异步路径都覆盖。"""

    def __init__(self, dsn: str | None = None) -> None:
        super().__init__()
        self._dsn = dsn or os.environ.get("DATABASE_URL", "")
        self._pool = None
        self._db_ok = bool(self._dsn)

    async def _get_pool(self):
        if self._pool is None and self._db_ok:
            try:
                from psycopg_pool import AsyncConnectionPool

                self._pool = AsyncConnectionPool(
                    self._dsn, min_size=1, max_size=3, open=False, timeout=5
                )
                await self._pool.open()
                async with self._pool.connection() as conn:
                    await conn.execute(_DDL)
            except Exception as exc:  # noqa: BLE001
                logger.warning("审计库不可用，降级 JSONL：%s", exc)
                self._db_ok = False
                self._pool = None
        return self._pool

    async def _write(self, row: dict) -> None:
        pool = await self._get_pool()
        if pool is not None:
            try:
                async with pool.connection() as conn:
                    await conn.execute(
                        "INSERT INTO self_agent_audit (tool, args_digest, status, elapsed_ms, error)"
                        " VALUES (%s, %s, %s, %s, %s)",
                        (row["tool"], row["args_digest"], row["status"],
                         row["elapsed_ms"], row.get("error")),
                    )
                return
            except Exception as exc:  # noqa: BLE001
                logger.warning("审计写库失败，降级 JSONL：%s", exc)
        with _FALLBACK.open("a") as f:
            f.write(json.dumps(row, ensure_ascii=False, default=str) + "\n")

    def _row(self, request, status: str, t0: float, error: str | None = None) -> dict:
        tc = request.tool_call or {}
        return {
            "tool": tc.get("name", "?"),
            "args_digest": _digest(tc.get("args")),
            "status": status,
            "elapsed_ms": int((time.monotonic() - t0) * 1000),
            "error": (error or "")[:500] or None,
        }

    async def awrap_tool_call(self, request, handler):
        t0 = time.monotonic()
        try:
            result = await handler(request)
        except Exception as exc:
            await self._write(self._row(request, "error", t0, str(exc)))
            raise
        status = "error" if getattr(result, "status", "") == "error" else "ok"
        await self._write(self._row(request, status, t0))
        return result

    def wrap_tool_call(self, request, handler):
        t0 = time.monotonic()
        try:
            result = handler(request)
        except Exception as exc:
            row = self._row(request, "error", t0, str(exc))
            with _FALLBACK.open("a") as f:
                f.write(json.dumps(row, ensure_ascii=False, default=str) + "\n")
            raise
        status = "error" if getattr(result, "status", "") == "error" else "ok"
        row = self._row(request, status, t0)
        # 同步路径无事件循环可用，直接走 JSONL（Aegra 下实际全为异步路径）
        with _FALLBACK.open("a") as f:
            f.write(json.dumps(row, ensure_ascii=False, default=str) + "\n")
        return result
