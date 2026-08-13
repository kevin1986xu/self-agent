"""M0 冒烟测试：不依赖网络与真实密钥的部分。"""

import json
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parents[1] / "src"))

import os

os.environ.setdefault("DASHSCOPE_API_KEY", "sk-dummy-for-test")


def test_mcp_config_injects_api_key(tmp_path, monkeypatch):
    cfg = tmp_path / "mcp.json"
    cfg.write_text(json.dumps({
        "d1": {"transport": "streamable_http", "url": "http://127.0.0.1:9999/mcp"},
        "d2": {"transport": "stdio", "command": "echo"},
    }))
    from self_agent import mcp, settings
    monkeypatch.setattr(settings, "MCP_CONFIG_PATH", cfg)
    monkeypatch.setattr(settings, "UAV_MCP_API_KEY", "test-key")
    servers = mcp.load_mcp_config()
    assert servers["d1"]["headers"]["X-API-Key"] == "test-key"
    assert "headers" not in servers["d2"]  # stdio 不注入


def test_build_agent_with_interrupts():
    from langchain_core.tools import tool

    from self_agent.agent import LOCKED_TOOLS, build_agent

    @tool
    def dispatch_drone(drone_id: str) -> str:
        """派机（测试桩）。"""
        return "ok"

    @tool
    def query_plots(region: str) -> str:
        """查图斑（测试桩）。"""
        return "[]"

    assert "dispatch_drone" in LOCKED_TOOLS
    agent = build_agent([dispatch_drone, query_plots], down_domains=["uav-preflight"])
    # 编译产物存在且包含 task/write_todos 等 harness 工具由 deepagents 保证；
    # 这里验证组装不抛错、图对象可用
    assert agent is not None
    assert agent.get_graph() is not None
