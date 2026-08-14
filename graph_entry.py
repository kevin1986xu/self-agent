"""Aegra / LangGraph Server 图入口适配（文件路径加载器用绝对导入）。"""

from self_agent.graph import make_graph_default, make_graph_uav

__all__ = ["make_graph_uav", "make_graph_default"]
