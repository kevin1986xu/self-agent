"""POC 对话入口（M0-4/M0-5）。

用法：
    .venv/bin/python poc/run_chat.py "查询A片区的图斑"
    .venv/bin/python poc/run_chat.py            # 交互模式

interrupt（高危工具）会打印确认请求，输入 y 批准 / n 拒绝。
"""

import asyncio
import sys
import uuid

sys.path.insert(0, "src")

from langgraph.checkpoint.memory import InMemorySaver
from langgraph.types import Command

from self_agent.agent import build_agent
from self_agent.mcp import load_mcp_tools


def print_new_messages(state_update, seen: set):
    for msg in state_update.get("messages", []):
        if id(msg) in seen:
            continue
        seen.add(id(msg))
        kind = msg.__class__.__name__
        if kind == "AIMessage":
            for tc in getattr(msg, "tool_calls", None) or []:
                print(f"  ⚙ 调用工具 {tc['name']}({tc['args']})")
            if msg.content:
                print(f"\n🤖 {msg.content}\n")
        elif kind == "ToolMessage":
            text = str(msg.content)
            print(f"  ↩ {text[:200]}{'…' if len(text) > 200 else ''}")


async def run_turn(agent, config, payload, seen):
    """执行一轮，处理 interrupt 循环。"""
    while True:
        result = await agent.ainvoke(payload, config=config)
        print_new_messages(result, seen)
        interrupts = result.get("__interrupt__")
        if not interrupts:
            return result
        for intr in interrupts:
            print(f"\n⚠ 高危操作待确认: {intr.value}")
        ans = input("批准执行? [y/N] ").strip().lower()
        decision = {"type": "approve"} if ans == "y" else {"type": "reject", "message": "用户拒绝"}
        payload = Command(resume={"decisions": [decision]})


async def main():
    tools, down = await load_mcp_tools()
    print(f"已加载 MCP 工具 {len(tools)} 个" + (f"；不可用域: {down}" if down else ""))
    agent = build_agent(tools, down_domains=down, checkpointer=InMemorySaver())
    config = {"configurable": {"thread_id": str(uuid.uuid4())}}
    seen: set = set()

    if len(sys.argv) > 1:
        await run_turn(agent, config, {"messages": [("user", sys.argv[1])]}, seen)
        return
    print("交互模式，exit 退出")
    while True:
        try:
            line = input("你> ").strip()
        except EOFError:
            break
        if not line or line in ("exit", "quit"):
            break
        await run_turn(agent, config, {"messages": [("user", line)]}, seen)


if __name__ == "__main__":
    asyncio.run(main())
