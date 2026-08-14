"""下游身份传递（模式 A：On-Behalf-Of 声明式代表身份，docs/09 身份链落地）。

问题：MCP 连接的 headers 建连时静态，而「当前用户」每次 run 才确定——
下游系统（如无人机平台）需要知道是谁在操作，才能返回该用户自己的数据/权限。

机制（contextvar 传播）：
  ① IdentityPropagationMiddleware 在每次工具调用前，把 run context 里的
     user_id 与该用户在目标系统的外部身份（external_identity 绑定表）写入
     contextvar；
  ② MCP 连接注入 httpx_client_factory：发请求时从 contextvar 读取并附加
       X-Acting-User:    u<user_id>（本框架统一身份，恒发）
       X-On-Behalf-Of:   <外部系统账号>（该用户已绑定目标系统时发）
  ③ 下游验完服务级 X-API-Key（信任本框架）后，按 X-On-Behalf-Of 以该用户
     身份回源查询——信任模型为「服务信任 + 身份声明」（阶段1）。

外部身份绑定：external_identity(user_id, system, external_id)，
mcp_config.json 中 server 加 "identity_system": "<系统名>" 即启用绑定解析。
"""

from __future__ import annotations

import contextvars
import logging
import os
import time
from typing import Any

import httpx

logger = logging.getLogger(__name__)

# 当前调用者：{"user_key": "u23", "bindings": {"drone-platform": "zhang.san"}}
current_caller: contextvars.ContextVar[dict | None] = contextvars.ContextVar(
    "current_caller", default=None)

_DDL = """
CREATE TABLE IF NOT EXISTS external_identity (
    user_id BIGINT NOT NULL,
    system TEXT NOT NULL,
    external_id TEXT NOT NULL,
    created_at TIMESTAMPTZ DEFAULT now(),
    PRIMARY KEY (user_id, system)
)
"""

_bind_cache: dict[int, tuple[float, dict[str, str]]] = {}


def _conn():
    import psycopg

    dsn = os.environ.get("DATABASE_URL", "")
    if not dsn:
        raise RuntimeError("需要 DATABASE_URL")
    conn = psycopg.connect(dsn, autocommit=True)
    conn.execute(_DDL)
    return conn


def bind(user_id: int, system: str, external_id: str) -> None:
    with _conn() as c:
        c.execute(
            """INSERT INTO external_identity (user_id, system, external_id) VALUES (%s,%s,%s)
               ON CONFLICT (user_id, system) DO UPDATE SET external_id=EXCLUDED.external_id""",
            (user_id, system, external_id))
    _bind_cache.pop(user_id, None)


def unbind(user_id: int, system: str) -> None:
    with _conn() as c:
        c.execute("DELETE FROM external_identity WHERE user_id=%s AND system=%s", (user_id, system))
    _bind_cache.pop(user_id, None)


def bindings_of(user_id: int) -> dict[str, str]:
    hit = _bind_cache.get(user_id)
    if hit and hit[0] > time.time():
        return hit[1]
    try:
        with _conn() as c:
            rows = c.execute("SELECT system, external_id FROM external_identity WHERE user_id=%s",
                             (user_id,)).fetchall()
        out = dict(rows)
    except Exception:  # noqa: BLE001 —— 无 DB 时无绑定
        out = {}
    _bind_cache[user_id] = (time.time() + 60, out)
    return out


def list_bindings() -> list[dict]:
    with _conn() as c:
        rows = c.execute(
            "SELECT user_id, system, external_id, created_at::date FROM external_identity"
            " ORDER BY user_id, system").fetchall()
    return [dict(zip(["user_id", "system", "external_id", "since"], r)) for r in rows]


def resolve_caller(request: Any) -> dict:
    """从工具调用请求解析当前调用者与其外部绑定。"""
    from .agent import _user_key

    key = _user_key(getattr(request, "runtime", None))
    if key.startswith("u"):
        try:
            return {"user_key": key, "bindings": bindings_of(int(key[1:]))}
        except ValueError:
            pass
    return {"user_key": key, "bindings": {}}


def make_identity_middleware():
    """身份传播中间件：把 run 的用户身份写入 contextvar（工具调用生命周期内）。
    与 AuditMiddleware 同栈挂载；Lead 与子代理都要挂。延迟导入避免循环依赖。"""
    from langchain.agents.middleware.types import AgentMiddleware

    class IdentityPropagationMiddleware(AgentMiddleware):
        def wrap_tool_call(self, request, handler):
            token = current_caller.set(resolve_caller(request))
            try:
                return handler(request)
            finally:
                current_caller.reset(token)

        async def awrap_tool_call(self, request, handler):
            token = current_caller.set(resolve_caller(request))
            try:
                return await handler(request)
            finally:
                current_caller.reset(token)

    return IdentityPropagationMiddleware()


def make_httpx_factory(identity_system: str | None):
    """MCP 连接的 httpx 客户端工厂：请求前附加身份头。"""

    async def _add_identity(request: httpx.Request) -> None:
        caller = current_caller.get()
        if not caller:
            return
        request.headers["X-Acting-User"] = caller["user_key"]
        if identity_system:
            ext = caller["bindings"].get(identity_system)
            if ext:
                request.headers["X-On-Behalf-Of"] = ext

    def factory(headers: dict[str, str] | None = None,
                timeout: httpx.Timeout | None = None,
                auth: httpx.Auth | None = None) -> httpx.AsyncClient:
        return httpx.AsyncClient(
            headers=headers, timeout=timeout, auth=auth, follow_redirects=True,
            event_hooks={"request": [_add_identity]})

    return factory
