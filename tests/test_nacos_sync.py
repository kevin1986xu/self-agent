"""M1-2 对账语义单测：IP 漂移不清空、下线才摘除、无变化不写。"""

import json
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parents[1] / "src"))

import os

os.environ.setdefault("DASHSCOPE_API_KEY", "sk-dummy-for-test")

from self_agent import nacos_sync, settings  # noqa: E402


def _setup(tmp_path, monkeypatch, current: dict):
    cfg = tmp_path / "mcp.json"
    cfg.write_text(json.dumps(current))
    monkeypatch.setattr(settings, "MCP_CONFIG_PATH", cfg)
    return cfg


BASE = {
    "uav-a-mcp": {"transport": "streamable_http", "url": "http://10.0.0.1:8201/mcp"},
    "uav-b-mcp": {"transport": "streamable_http", "url": "http://10.0.0.1:8202/mcp"},
    "manual-x": {"transport": "streamable_http", "url": "http://other:9000/mcp"},
}


def test_unreachable_but_listed_keeps_current(tmp_path, monkeypatch):
    """IP 漂移瞬间（listed 但全部探活失败）绝不能清空配置。"""
    cfg = _setup(tmp_path, monkeypatch, BASE)
    wrote = nacos_sync.sync_config_file({}, listed={"uav-a-mcp", "uav-b-mcp"})
    assert wrote is False
    assert json.loads(cfg.read_text()) == BASE


def test_deregistered_is_removed_manual_untouched(tmp_path, monkeypatch):
    """registry 真正下线才摘除；人工条目（无前缀）永不触碰。"""
    cfg = _setup(tmp_path, monkeypatch, BASE)
    wrote = nacos_sync.sync_config_file(
        {"uav-a-mcp": "http://10.0.0.2:8201/mcp"}, listed={"uav-a-mcp"}
    )
    assert wrote is True
    result = json.loads(cfg.read_text())
    assert result["uav-a-mcp"]["url"] == "http://10.0.0.2:8201/mcp"  # 更新
    assert "uav-b-mcp" not in result  # 下线摘除
    assert result["manual-x"] == BASE["manual-x"]  # 人工条目不动


def test_no_change_no_write(tmp_path, monkeypatch):
    cfg = _setup(tmp_path, monkeypatch, BASE)
    before = cfg.stat().st_mtime_ns
    wrote = nacos_sync.sync_config_file(
        {"uav-a-mcp": "http://10.0.0.1:8201/mcp", "uav-b-mcp": "http://10.0.0.1:8202/mcp"},
        listed={"uav-a-mcp", "uav-b-mcp"},
    )
    assert wrote is False
    assert cfg.stat().st_mtime_ns == before
