"""M0/通用化 冒烟测试：不依赖网络与真实密钥的部分。"""

import json
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parents[1] / "src"))

import os

os.environ.setdefault("DASHSCOPE_API_KEY", "sk-dummy-for-test")


def test_projects_load():
    from self_agent.project import list_projects, load_project

    names = list_projects()
    assert "uav" in names and "default" in names
    uav = load_project("uav")
    assert "dispatch_drone" in uav.locked_tools
    assert uav.knowledge_scope == "uav"
    default = load_project("default")
    assert default.locked_tools == []


def test_mcp_config_injects_headers_by_project(tmp_path, monkeypatch):
    from self_agent import mcp
    from self_agent.project import Project

    d = tmp_path / "p1"
    d.mkdir()
    (d / "mcp_config.json").write_text(json.dumps({
        "d1": {"transport": "streamable_http", "url": "http://127.0.0.1:9999/mcp"},
        "d2": {"transport": "stdio", "command": "echo"},
    }))
    monkeypatch.setenv("TEST_KEY_ENV", "test-key")
    p = Project(name="p1", display_name="t", role_prompt="",
                mcp_headers_env={"X-API-Key": "TEST_KEY_ENV"}, dir=d)
    servers = mcp.load_mcp_config(p)
    assert servers["d1"]["headers"]["X-API-Key"] == "test-key"
    assert "headers" not in servers["d2"]  # stdio 不注入


def test_build_agent_per_project(tmp_path, monkeypatch):
    from langchain_core.tools import tool

    from self_agent import settings
    from self_agent.agent import build_agent
    from self_agent.project import load_project

    monkeypatch.setattr(settings, "WORKSPACE_ROOT", tmp_path)

    @tool
    def dispatch_drone(drone_id: str) -> str:
        """派机（测试桩）。"""
        return "ok"

    uav = build_agent(load_project("uav"), [dispatch_drone])
    assert uav.get_graph() is not None
    default = build_agent(load_project("default"), [dispatch_drone])
    assert default.get_graph() is not None
    # 项目工作区隔离
    assert (tmp_path / "uav").exists() and (tmp_path / "default").exists()
