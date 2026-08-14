"""循环卫生守卫（借鉴 deepseek-harness `guard` 包的设计，M3 增强）。

解决我们在评测与验收中三次实际观察到的失败模式——模型重复发起同参数的
同一工具调用（execute×8 重试、dispatch 重试环、set_live_quality×7）：

**重复同参检测**：从 request.state 的消息历史统计相同 (tool, args) 的既往
调用次数，超过阈值即不再执行，返回纠偏 ToolMessage 引导模型换路或如实报告。
基于状态而非实例变量——天然按线程作用域，跨 run/进程/共享实例都正确。

预算与截止时间由既有的正确层承担，不在此重复：
- run 级模型调用硬顶：ModelCallLimitMiddleware(run_limit=80)（agent.py 组装）；
- wall-clock 截止：网关 _stream_run timeout=600s。
"""

from __future__ import annotations

import hashlib
import json
from typing import Any

from langchain.agents.middleware.types import AgentMiddleware
from langchain_core.messages import AIMessage, ToolMessage

MAX_IDENTICAL_CALLS = 3


def _key(name: str | None, args: Any) -> str:
    blob = json.dumps(args, ensure_ascii=False, sort_keys=True, default=str)
    return f"{name}:{hashlib.sha1(blob.encode()).hexdigest()[:12]}"


class LoopGuardMiddleware(AgentMiddleware):
    def __init__(self, *, max_identical: int = MAX_IDENTICAL_CALLS) -> None:
        super().__init__()
        self.max_identical = max_identical

    def _check(self, request: Any) -> ToolMessage | None:
        tc = request.tool_call or {}
        current = _key(tc.get("name"), tc.get("args"))
        prior = 0
        state = getattr(request, "state", None) or {}
        for m in state.get("messages") or []:
            if isinstance(m, AIMessage):
                for past in m.tool_calls or []:
                    if _key(past.get("name"), past.get("args")) == current:
                        prior += 1
        # prior 含本次（本轮 AIMessage 已入 state）；> 阈值才拦，容忍合法重复
        if prior > self.max_identical:
            return ToolMessage(
                content=f"[守卫] 工具 {tc.get('name')} 已用完全相同的参数调用 "
                        f"{prior - 1} 次。重复调用不会改变结果——请换一种方法或参数，"
                        "或如实向用户报告该操作无法完成及原因。",
                tool_call_id=tc.get("id", ""), status="error")
        return None

    def wrap_tool_call(self, request, handler):
        blocked = self._check(request)
        return blocked if blocked is not None else handler(request)

    async def awrap_tool_call(self, request, handler):
        blocked = self._check(request)
        return blocked if blocked is not None else await handler(request)
