"""框架核心组装（技术方案 4.1）。

- 子代理从 config/subagents.json 加载（M1-4 配置化：改配置不改代码），
  每个子代理限定工具集与模型档位；
- 审计中间件记录所有工具调用（M1-5）；
- 🔒 高危工具挂 interrupt_on 人在环（M0-5 已验证）。
"""

import json
import logging
import os
import shutil

from langgraph.checkpoint.base import BaseCheckpointSaver

from deepagents import create_deep_agent
from deepagents.backends import CompositeBackend, StoreBackend

from . import settings
from .audit import AuditMiddleware
from .model import build_model
from .sandbox import WorkspaceShellBackend

logger = logging.getLogger(__name__)

# 🔒 高危·人在环工具（与 mcp-services 的 confirm_token 工具对齐；
# interrupt 是体验层，confirm_token 是服务端最后防线，双层语义见方案 4.5）。
# 注意：emergency_stop / return_home 是紧急白名单动作（docs/05）——紧急时刻
# 必须直通，任何一层都不拦（回归 #69/#70 教训：拦了模型都会变得畏缩）。
LOCKED_TOOLS = [
    "dispatch_drone",
    "create_task_plan",
    "take_off",
    "create_zone",
    "delete_zone",
    "start_3d_modeling",
    "create_scheduled_task",
    "create_recurring_task",
    "cancel_scheduled_task",
    "reschedule_task",
    "retry_failed_task",
    "resume_from_breakpoint",
    "takeoff_to_point",
    "set_height_limit",
    "debug_mode",
    "charge_control",
]

LEAD_PROMPT = """\
你是无人机作业智能体的主控（Lead Agent）。

工作方式：
- 复杂任务先用 write_todos 列计划，随执行更新状态；
- 独立子任务用 task 工具并行派给子代理（飞控执行找 uav-ops、告警监控找
  monitor、数据分析找 data-analyst），自己只做规划与汇总；简单查询可直接调工具；
- 工具返回错误时如实报告，不得编造数据；
- **紧急指令（急停、返航）立即调用对应工具执行，不要先查询状态、不要犹豫**；
- 用户明确点名的操作（如调画质、开舱盖、排期），直接调用最贴合的专用工具完成，
  不要用查询类工具替代或半途停下；
- 高危操作（派机、起飞、建删围栏等）系统会自动弹出人工确认，弹出后等待结果、
  不要重复发起；没弹确认的操作正常执行即可；
- 工具返回"已登记待确认单"即视为该步已受理：**不要**再次调用同一工具，
  如实告知用户等待审批并继续其余任务；只有收到 [SYSTEM_CONFIRMATION] 带
  confirm_token 的指令时，才携带该 token 重新执行对应操作。

记忆：/memories/ 目录跨会话持久。用户明确的偏好（报告格式、常用片区、
称呼习惯等）用 write_file 记到 /memories/preferences.md；会话开始处理任务前，
如涉及个性化输出，先 read_file 查看该文件是否有相关偏好。

回答使用中文。
"""


SKILLS_SRC = settings.PROJECT_ROOT / "skills"


def _sync_workspace() -> None:
    """P0 沙箱工作区：受限根目录 + 技能装配（repo 内置技能 + 受管技能库）。"""
    settings.WORKSPACE_ROOT.mkdir(parents=True, exist_ok=True)
    if SKILLS_SRC.exists():
        shutil.copytree(SKILLS_SRC, settings.WORKSPACE_ROOT / "skills", dirs_exist_ok=True)
    from .skill_manager import materialize  # 延迟导入避免无 DB 环境的启动开销

    managed = materialize(settings.WORKSPACE_ROOT)
    if managed:
        logger.info("受管技能已装配: %s", managed)


def build_backend():
    """文件系统路由（技术方案 4.4，M2-1 P0 沙箱）。

    - 默认：LocalShellBackend 受限在 WORKSPACE_ROOT（虚拟路径模式），自带
      execute 工具跑技能脚本；PATH 注入本项目 venv（python-pptx 等文档库在内），
      不继承宿主环境（密钥不外泄给脚本）；
    - /memories：跨会话持久（宿主 store）。
    - 已知限制（P0 接受）：工作区暂为全会话共享；P1 Docker 沙箱按会话隔离。
    """
    _sync_workspace()
    from .docker_sandbox import build_shell_backend

    return CompositeBackend(
        default=build_shell_backend(),  # SANDBOX_MODE=docker 时 execute 进容器
        routes={"/memories": StoreBackend(namespace=lambda rt: ("memories",))},
    )

SUBAGENTS_PATH = settings.PROJECT_ROOT / "config" / "subagents.json"


def load_subagents(tools: list) -> list[dict]:
    """从配置文件加载子代理定义，把工具名解析为实际工具对象。"""
    if not SUBAGENTS_PATH.exists():
        return []
    by_name = {getattr(t, "name", ""): t for t in tools}
    subagents = []
    for name, spec in json.loads(SUBAGENTS_PATH.read_text()).items():
        wanted = spec.get("tools", [])
        resolved = [by_name[n] for n in wanted if n in by_name]
        missing = [n for n in wanted if n not in by_name]
        if missing:
            logger.warning("子代理 %s 缺少工具（域未挂载？）: %s", name, missing)
        subagents.append({
            "name": name,
            "description": spec["description"],
            "system_prompt": spec["system_prompt"],
            "tools": resolved,
            "model": build_model(spec.get("model", "strong")),
            # 子代理有独立中间件栈：审计必须逐个挂，否则只记到 Lead 的 task 调用
            "middleware": [AuditMiddleware()],
        })
    return subagents


def _knowledge_tool():
    """知识库检索工具（R16）。无 DATABASE_URL 时不挂载。"""
    if not os.environ.get("DATABASE_URL"):
        return None
    from langchain_core.tools import tool

    @tool
    def search_knowledge(query: str) -> str:
        """检索知识库（巡检规范/飞行手册/历史报告等已入库文档）。
        涉及规程、限值、操作标准的问题，先用本工具查依据再回答，并注明出处文件名。"""
        from . import knowledge

        hits = knowledge.search(query)
        if not hits:
            return "知识库中未找到相关内容。"
        return "\n---\n".join(f"[{h['file']} 第{h['seq']}块] {h['content']}" for h in hits)

    return search_knowledge


def build_agent(
    tools: list,
    *,
    down_domains: list[str] | None = None,
    checkpointer: BaseCheckpointSaver | bool | None = None,
    store=None,
    skills: list[str] | None = None,
):
    """组装 Deep Agent。down_domains 会写入系统提示，避免模型摸不可用工具。"""
    kt = _knowledge_tool()
    if kt is not None:
        tools = list(tools) + [kt]
    prompt = LEAD_PROMPT
    if down_domains:
        prompt += f"\n注意：以下工具域当前不可用，不要尝试调用：{', '.join(down_domains)}。\n"
    tool_names = {getattr(t, "name", "") for t in tools}
    interrupts = {name: True for name in LOCKED_TOOLS if name in tool_names}
    if skills is None:
        skills = []
        if SKILLS_SRC.exists():
            skills.append("/skills/doc-skills/")  # 内置技能（backend 虚拟路径）
        if (settings.WORKSPACE_ROOT / "skills" / "managed").exists():
            skills.append("/skills/managed/")  # 受管技能（导入+审核通过的）
        skills = skills or None
    return create_deep_agent(
        model=build_model("strong"),
        tools=tools,
        system_prompt=prompt,
        subagents=load_subagents(tools),
        middleware=[AuditMiddleware()],
        backend=build_backend(),
        interrupt_on=interrupts or None,
        checkpointer=checkpointer,
        store=store,
        skills=skills,
        name="self-agent",
    )
