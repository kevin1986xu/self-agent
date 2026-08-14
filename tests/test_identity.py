"""用户体系单测：token 注册表解析、用户键提取（无 DB/网络部分）。"""

import sys
from pathlib import Path
from types import SimpleNamespace

ROOT = Path(__file__).parents[1]
sys.path.insert(0, str(ROOT / "src"))
sys.path.insert(0, str(ROOT))

import os

os.environ.setdefault("DASHSCOPE_API_KEY", "sk-dummy-for-test")


def test_auth_registry_parse(monkeypatch):
    import auth as aegra_auth

    monkeypatch.setenv("AUTH_TOKENS", "tk-a:alice:admin, tk-b:bob , bad")
    reg = aegra_auth._registry()
    assert reg["tk-a"] == {"identity": "alice", "role": "admin"}
    assert reg["tk-b"]["role"] == "member"
    assert "bad" not in reg


def test_user_key_extraction():
    from self_agent.agent import _user_key

    assert _user_key(SimpleNamespace(context={"user_id": 7})) == "u7"
    assert _user_key(SimpleNamespace(context={"langgraph_auth_user": {"identity": "alice"}})) == "id:alice"
    assert _user_key(SimpleNamespace(context={})) == "shared"
    assert _user_key(None) == "shared"
