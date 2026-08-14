"""框架核心组装（通用化重构）。

核心层只含通用 harness 纪律（规划/派单/记忆/诚实性/守卫），业务人格、
高危工具清单、子代理编制全部来自项目档案（projects/<name>/）。
"""

import json
import logging
import shutil

from langgraph.checkpoint.base import BaseCheckpointSaver

from deepagents import create_deep_agent
from deepagents.backends import CompositeBackend, StoreBackend
from langchain.agents.middleware import ModelCallLimitMiddleware

from . import settings
from .audit import AuditMiddleware
from .guard import LoopGuardMiddleware
from .model import build_model
from .project import Project, load_project

logger = logging.getLogger(__name__)

# 通用 harness 纪律：与具体业务无关，所有项目共享
CORE_PROMPT = """
工作方式（通用纪律）：
- 复杂任务先用 write_todos 列计划，随执行更新状态；
- 独立子任务用 task 工具并行派给子代理，自己做规划与汇总；简单查询直接调工具；
- 工具返回错误时如实报告，不得编造数据；
- 记忆：/memories/ 目录跨会话持久。用户明确的偏好用 write_file 记到
  /memories/preferences.md；涉及个性化输出时先 read_file 查看该文件。

回答使用中文。
"""

SKILLS_SRC = settings.PROJECT_ROOT / "skills"


def _sync_workspace(project: Project) -> None:
    """项目工作区：受限根目录 + 技能装配（内置技能组 + 受管技能库）。"""
    ws = project.workspace
    ws.mkdir(parents=True, exist_ok=True)
    for group in project.skills:
        src = SKILLS_SRC / group
        if src.exists():
            shutil.copytree(src, ws / "skills" / group, dirs_exist_ok=True)
    from .skill_manager import materialize

    managed = materialize(ws)
    if managed:
        logger.info("[%s] 受管技能已装配: %s", project.name, managed)


def build_backend(project: Project):
    """文件系统路由：项目独立工作区；/memories 跨会话持久（按项目隔离命名空间）。"""
    _sync_workspace(project)
    from .docker_sandbox import build_shell_backend

    ns = ("memories", project.name)
    return CompositeBackend(
        default=build_shell_backend(root_dir=project.workspace),
        routes={"/memories": StoreBackend(namespace=lambda rt: ns)},
    )


def load_subagents(project: Project, tools: list) -> list[dict]:
    if not project.subagents_path.exists():
        return []
    by_name = {getattr(t, "name", ""): t for t in tools}
    subagents = []
    for name, spec in json.loads(project.subagents_path.read_text()).items():
        wanted = spec.get("tools", [])
        resolved = [by_name[n] for n in wanted if n in by_name]
        missing = [n for n in wanted if n not in by_name]
        if missing:
            logger.warning("[%s] 子代理 %s 缺少工具（域未挂载？）: %s", project.name, name, missing)
        subagents.append({
            "name": name,
            "description": spec["description"],
            "system_prompt": spec["system_prompt"],
            "tools": resolved,
            "model": build_model(spec.get("model", "strong")),
            # 子代理有独立中间件栈：审计与守卫都要逐个挂
            "middleware": [AuditMiddleware(), LoopGuardMiddleware()],
        })
    return subagents


def _knowledge_tool(project: Project):
    """知识库检索工具（按项目作用域）。无 DATABASE_URL 时不挂载。"""
    import os

    if not os.environ.get("DATABASE_URL"):
        return None
    from langchain_core.tools import tool

    scope = project.knowledge_scope

    @tool
    def search_knowledge(query: str) -> str:
        """检索本项目知识库（规范/手册/历史资料等已入库文档）。
        涉及规程、限值、操作标准的问题，先用本工具查依据再回答，并注明出处文件名。"""
        from . import knowledge

        hits = knowledge.search(query, scope=scope)
        if not hits:
            return "知识库中未找到相关内容。"
        return "\n---\n".join(f"[{h['file']} 第{h['seq']}块] {h['content']}" for h in hits)

    return search_knowledge


def build_agent(
    project: Project | str,
    tools: list,
    *,
    down_domains: list[str] | None = None,
    checkpointer: BaseCheckpointSaver | bool | None = None,
    store=None,
    skills: list[str] | None = None,
):
    """按项目档案组装 Deep Agent。"""
    if isinstance(project, str):
        project = load_project(project)
    kt = _knowledge_tool(project)
    if kt is not None:
        tools = list(tools) + [kt]
    prompt = project.role_prompt + "\n" + CORE_PROMPT
    if down_domains:
        prompt += f"\n注意：以下工具域当前不可用，不要尝试调用：{', '.join(down_domains)}。\n"
    tool_names = {getattr(t, "name", "") for t in tools}
    interrupts = {name: True for name in project.locked_tools if name in tool_names}
    if skills is None:
        skills = [f"/skills/{g}/" for g in project.skills
                  if (project.workspace / "skills" / g).exists()]
        if (project.workspace / "skills" / "managed").exists():
            skills.append("/skills/managed/")
        skills = skills or None
    return create_deep_agent(
        model=build_model("strong"),
        tools=tools,
        system_prompt=prompt,
        subagents=load_subagents(project, tools),
        middleware=[AuditMiddleware(), LoopGuardMiddleware(),
                    ModelCallLimitMiddleware(run_limit=80, exit_behavior="end")],
        backend=build_backend(project),
        interrupt_on=interrupts or None,
        checkpointer=checkpointer,
        store=store,
        skills=skills,
        name=f"self-agent:{project.name}",
    )
