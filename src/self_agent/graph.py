"""LangGraph Server 图入口（多项目：每个项目一个图 / assistant_id）。

aegra.json 的 graphs 段把每个项目映射到 make_graph_for(<项目名>) 生成的工厂；
前端/网关切项目 = 切 assistant_id。新增项目后在 aegra.json 加一行即可。

可观测走宿主 Aegra 的原生 OTel 集成（OTEL_TARGETS=LANGFUSE），图保持纯净——
教训：graph.with_config(callbacks=...) 会破坏 checkpointer 注入。
"""

from .agent import build_agent
from .mcp import load_mcp_tools
from .project import load_project


def make_graph_for(project_name: str):
    async def make_graph():
        project = load_project(project_name)
        tools, down = await load_mcp_tools(project)
        return build_agent(project, tools, down_domains=down, checkpointer=None)

    make_graph.__name__ = f"make_graph_{project_name}"
    return make_graph


# aegra.json / langgraph.json 引用的具名工厂
make_graph_uav = make_graph_for("uav")
make_graph_default = make_graph_for("default")
