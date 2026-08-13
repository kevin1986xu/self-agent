"""LangGraph Server 图入口（开发期 `langgraph dev` / 生产协议端点共用）。

服务端自管持久化与线程，这里不传 checkpointer（langgraph-api 会拒绝自定义
checkpointer）。MCP 工具在图构建时异步加载。
"""

from .agent import build_agent
from .mcp import load_mcp_tools


async def make_graph():
    tools, down = await load_mcp_tools()
    return build_agent(tools, down_domains=down, checkpointer=None)
