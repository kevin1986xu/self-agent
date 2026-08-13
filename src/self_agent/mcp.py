"""MCP 集成层（技术方案 4.2）。

- 配置来自 config/mcp_config.json（后续由 Nacos 同步桥生成）；
- 每 server 注入 X-API-Key headers；
- run 前 /healthz 快检：失败的域本轮不挂载（修正 DeerFlow「发现失败缓存跳过」
  的行为），并返回失败清单供系统提示声明。
"""

import json
import logging
from urllib.parse import urlsplit

import httpx
from langchain_mcp_adapters.client import MultiServerMCPClient

from . import settings

logger = logging.getLogger(__name__)


def load_mcp_config() -> dict[str, dict]:
    """读取 MCP 配置并注入鉴权 headers。"""
    if not settings.MCP_CONFIG_PATH.exists():
        logger.warning("MCP 配置不存在: %s", settings.MCP_CONFIG_PATH)
        return {}
    servers: dict[str, dict] = json.loads(settings.MCP_CONFIG_PATH.read_text())
    for conn in servers.values():
        if conn.get("transport") in ("streamable_http", "http", "sse"):
            headers = conn.setdefault("headers", {})
            if settings.UAV_MCP_API_KEY and "X-API-Key" not in headers:
                headers["X-API-Key"] = settings.UAV_MCP_API_KEY
    return servers


async def _healthy(url: str, timeout: float = 2.0) -> bool:
    """探测 server 的 /healthz（免鉴权端点）。"""
    parts = urlsplit(url)
    healthz = f"{parts.scheme}://{parts.netloc}/healthz"
    try:
        async with httpx.AsyncClient(timeout=timeout) as client:
            resp = await client.get(healthz)
            return resp.status_code == 200
    except httpx.HTTPError:
        return False


async def load_mcp_tools() -> tuple[list, list[str]]:
    """健康检查后加载工具。返回 (tools, 不可用的域名单)。"""
    servers = load_mcp_config()
    healthy: dict[str, dict] = {}
    down: list[str] = []
    for name, conn in servers.items():
        if "url" in conn and not await _healthy(conn["url"]):
            down.append(name)
            logger.warning("MCP 域不可用，本轮不挂载: %s (%s)", name, conn["url"])
        else:
            healthy[name] = conn
    if not healthy:
        return [], down
    client = MultiServerMCPClient(healthy)
    tools = await client.get_tools()
    return tools, down
