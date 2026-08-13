"""Nacos MCP Registry → 本框架 MCP 配置生成器（M1-2，技术方案 4.2）。

复刻既有 uav_extensions/nacos_bridge 的对账语义，但输出目标是本框架的
config/mcp_config.json（文件本体不含密钥，X-API-Key 由 mcp.load_mcp_config
运行时注入）：

- 只管理名字命中 UAV_BRIDGE_PREFIX（默认 "uav-"）的 server，不碰人工条目；
- DIRECT 端点无 TTL、IP 变更后新旧并存 → 逐端点探活 /healthz 选第一个可达；
- 无变化不写（防抖）；Nacos 拉取失败保持现状不误删；
- 写入后新会话自动生效（graph 工厂每次构建都重读配置），无需重启。

运行：python -m self_agent.nacos_sync            # 常驻轮询
      python -m self_agent.nacos_sync --once     # 单轮对账（调试/CI）
环境：NACOS_SERVER_ADDR / NACOS_USERNAME / NACOS_PASSWORD / NACOS_NAMESPACE
      UAV_BRIDGE_PREFIX（默认 uav-）  BRIDGE_INTERVAL_S（默认 30）
"""

from __future__ import annotations

import asyncio
import json
import logging
import os
import sys
from typing import Any

import httpx

from . import settings

logger = logging.getLogger(__name__)

NACOS_ADDR = os.getenv("NACOS_SERVER_ADDR", "").strip()
NACOS_NS = os.getenv("NACOS_NAMESPACE", "public")
NACOS_USER = os.getenv("NACOS_USERNAME", "nacos")
NACOS_PASS = os.getenv("NACOS_PASSWORD", "")
PREFIX = os.getenv("UAV_BRIDGE_PREFIX", "uav-")
INTERVAL_S = int(os.getenv("BRIDGE_INTERVAL_S", "30"))


async def _nacos_token(client: httpx.AsyncClient) -> str:
    r = await client.post(
        f"http://{NACOS_ADDR}/nacos/v1/auth/users/login",
        data={"username": NACOS_USER, "password": NACOS_PASS},
    )
    r.raise_for_status()
    return r.json()["accessToken"]


async def fetch_nacos_servers(client: httpx.AsyncClient) -> tuple[dict[str, str], set[str]]:
    """Nacos Registry 对账输入。

    返回 (desired, listed)：desired = 探活可达的 {name: url}；listed = registry
    中所有命中前缀的名字。两者分开是关键语义——「registry 已下线」才允许摘除，
    「registry 在但端点探活不通」必须保持现状（否则 IP 漂移瞬间会清空全部配置）。
    """
    token = await _nacos_token(client)
    headers = {"accessToken": token}
    base = f"http://{NACOS_ADDR}/nacos/v3/admin/ai/mcp"
    r = await client.get(
        f"{base}/list", headers=headers,
        params={"namespaceId": NACOS_NS, "pageNo": 1, "pageSize": 100},
    )
    r.raise_for_status()
    page = r.json().get("data") or {}
    items = page.get("pageItems") or page.get("list") or []
    out: dict[str, str] = {}
    listed: set[str] = set()
    for it in items:
        name = it.get("name") or ""
        if not name.startswith(PREFIX):
            continue
        listed.add(name)
        d = await client.get(base, headers=headers,
                             params={"namespaceId": NACOS_NS, "mcpName": name})
        detail = (d.json() or {}).get("data") or {}
        eps = detail.get("backendEndpoints") or []
        if not eps:
            logger.warning("忽略无端点的 server：%s", name)
            continue
        path = ((detail.get("remoteServerConfig") or {}).get("exportPath")) or "/mcp"
        url = None
        for ep in eps:
            candidate = f"http://{ep.get('address')}:{ep.get('port')}"
            try:
                probe = await client.get(f"{candidate}/healthz", timeout=2)
                if probe.status_code == 200:
                    url = f"{candidate}{path}"
                    break
            except Exception:  # noqa: BLE001
                continue
        if url is None:
            logger.warning("server %s 的 %d 个端点均不可达，保持现状", name, len(eps))
            continue
        out[name] = url
    return out, listed


def sync_config_file(desired: dict[str, str], listed: set[str]) -> bool:
    """把期望态合并进 mcp_config.json。返回是否写入。

    摘除规则：仅当名字命中前缀 且 registry 不再列出（真正下线）才摘除；
    「listed 但探活不通」保持现状——run 前的 healthz 快检会临时跳过它。
    """
    path = settings.MCP_CONFIG_PATH
    current: dict[str, Any] = {}
    if path.exists():
        current = json.loads(path.read_text() or "{}")

    merged = dict(current)
    changed = False
    for name, url in desired.items():
        cur = current.get(name)
        if not cur or cur.get("url") != url:
            changed = True
        merged[name] = {"transport": "streamable_http", "url": url}
    for name in list(current):
        if name.startswith(PREFIX) and name not in listed:
            merged.pop(name)  # Nacos registry 已下线 → 摘除（仅限前缀管理范围）
            changed = True

    if not changed:
        return False
    tmp = path.with_suffix(".tmp")
    tmp.write_text(json.dumps(merged, ensure_ascii=False, indent=2))
    tmp.replace(path)  # 原子替换，读方不会看到半截文件
    logger.info("已写入 %d 个 server → %s", len(desired), path)
    return True


async def sync_once(client: httpx.AsyncClient) -> bool:
    desired, listed = await fetch_nacos_servers(client)
    return sync_config_file(desired, listed)


async def main() -> None:
    if not NACOS_ADDR:
        raise SystemExit("需要 NACOS_SERVER_ADDR")
    once = "--once" in sys.argv
    async with httpx.AsyncClient(timeout=15) as client:
        while True:
            try:
                wrote = await sync_once(client)
                if once:
                    print("changed" if wrote else "no-change")
                    return
            except Exception as exc:  # noqa: BLE001 —— 单轮失败保持现状，下轮重试
                if once:
                    raise
                logger.warning("同步失败（保持现状）：%s", exc)
            await asyncio.sleep(INTERVAL_S)


if __name__ == "__main__":
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
    asyncio.run(main())
