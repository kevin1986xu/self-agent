"""LangGraph Server 图入口（开发期 `langgraph dev` / 生产协议端点共用）。

服务端自管持久化与线程，这里不传 checkpointer（langgraph-api 会拒绝自定义
checkpointer）。MCP 工具在图构建时异步加载。

可观测（M1-10）：走宿主 Aegra 的原生 OTel 集成（OTEL_TARGETS=LANGFUSE +
LANGFUSE_BASE_URL/PUBLIC_KEY/SECRET_KEY 环境变量），图本体保持纯净——
教训：graph.with_config(callbacks=[LangfuseHandler]) 会让 Aegra 的
checkpointer 注入失败（handler 不可深拷贝），run 会静默失去持久化。
"""

from .agent import build_agent
from .mcp import load_mcp_tools


async def make_graph():
    tools, down = await load_mcp_tools()
    return build_agent(tools, down_domains=down, checkpointer=None)
