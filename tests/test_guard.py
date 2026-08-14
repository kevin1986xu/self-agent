"""循环卫生守卫单测（借鉴 deepseek-harness guard，状态驱动版）。"""

import sys
from pathlib import Path
from types import SimpleNamespace

sys.path.insert(0, str(Path(__file__).parents[1] / "src"))

import os

os.environ.setdefault("DASHSCOPE_API_KEY", "sk-dummy-for-test")

from langchain_core.messages import AIMessage, ToolMessage

from self_agent.guard import LoopGuardMiddleware  # noqa: E402


def _ai(name, args):
    return AIMessage(content="", tool_calls=[{"name": name, "args": args, "id": "x"}])


def _req(history: list, name="t", args=None):
    return SimpleNamespace(
        tool_call={"name": name, "args": args or {"x": 1}, "id": "c1"},
        state={"messages": history},
    )


def _ok(request):
    return ToolMessage(content="ok", tool_call_id="c1")


def test_blocks_after_threshold_from_history():
    g = LoopGuardMiddleware(max_identical=3)
    history = [_ai("t", {"x": 1}) for _ in range(3)]
    assert g.wrap_tool_call(_req(history), _ok).content == "ok"  # 第3次(prior=3)放行
    history.append(_ai("t", {"x": 1}))
    blocked = g.wrap_tool_call(_req(history), _ok)               # 第4次拦截
    assert blocked.status == "error" and "完全相同的参数" in blocked.content


def test_different_args_or_tools_pass():
    g = LoopGuardMiddleware(max_identical=2)
    history = [_ai("t", {"x": i}) for i in range(5)] + [_ai("other", {"x": 1})] * 5
    assert g.wrap_tool_call(_req(history, args={"x": 99}), _ok).content == "ok"


def test_stateless_across_instances():
    """守卫无实例状态：同一历史在新实例上判定一致（共享/重建实例都正确）。"""
    history = [_ai("t", {"x": 1}) for _ in range(4)]
    assert LoopGuardMiddleware().wrap_tool_call(_req(history), _ok).status == "error"
    assert LoopGuardMiddleware().wrap_tool_call(_req(history), _ok).status == "error"
