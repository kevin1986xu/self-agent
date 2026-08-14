"""MCP 集成层（通用化）：按项目加载 MCP 配置。

- 配置来自 projects/<name>/mcp_config.json——外部自建、公共开源 MCP 均可，
  streamable_http/sse/stdio 任意传输；
- 鉴权 headers 按 project.json 的 mcp_headers_env 从环境变量注入（密钥不落盘）；
- run 前 /healthz 快检（约定俗成端点，探测失败的域本轮不挂载并声明）。
"""

import json
import logging
from urllib.parse import urlsplit

import httpx

from .project import Project

logger = logging.getLogger(__name__)


def load_mcp_config(project: Project) -> dict[str, dict]:
    if not project.mcp_config_path.exists():
        return {}
    from .identity_propagation import make_httpx_factory

    servers: dict[str, dict] = json.loads(project.mcp_config_path.read_text())
    headers = project.mcp_headers()
    for conn in servers.values():
        if conn.get("transport") in ("streamable_http", "http", "sse"):
            if headers:
                conn.setdefault("headers", {}).update(
                    {k: v for k, v in headers.items() if k not in conn.get("headers", {})})
            # 下游身份传递：X-Acting-User 恒发；identity_system 配置后按绑定发 X-On-Behalf-Of
            conn["httpx_client_factory"] = make_httpx_factory(conn.pop("identity_system", None))
    return servers


async def _healthy(url: str, timeout: float = 2.0) -> bool:
    parts = urlsplit(url)
    healthz = f"{parts.scheme}://{parts.netloc}/healthz"
    try:
        async with httpx.AsyncClient(timeout=timeout) as client:
            return (await client.get(healthz)).status_code == 200
    except httpx.HTTPError:
        return False


async def load_mcp_tools(project: Project) -> tuple[list, list[str]]:
    """健康检查后加载工具。返回 (tools, 不可用域名单)。"""
    from langchain_mcp_adapters.client import MultiServerMCPClient

    servers = load_mcp_config(project)
    healthy: dict[str, dict] = {}
    down: list[str] = []
    for name, conn in servers.items():
        if "url" in conn and not await _healthy(conn["url"]):
            down.append(name)
            logger.warning("[%s] MCP 域不可用，本轮不挂载: %s (%s)", project.name, name, conn["url"])
        else:
            healthy[name] = conn
    if not healthy:
        return [], down
    client = MultiServerMCPClient(healthy)
    return await client.get_tools(), down
