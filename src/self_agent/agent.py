"""框架核心组装（技术方案 4.1）。

- 子代理从 config/subagents.json 加载（M1-4 配置化：改配置不改代码），
  每个子代理限定工具集与模型档位；
- 审计中间件记录所有工具调用（M1-5）；
- 🔒 高危工具挂 interrupt_on 人在环（M0-5 已验证）。
"""

import json
import logging

from langgraph.checkpoint.base import BaseCheckpointSaver

from deepagents import create_deep_agent
from deepagents.backends import CompositeBackend, StateBackend, StoreBackend

from . import settings
from .audit import AuditMiddleware
from .model import build_model

logger = logging.getLogger(__name__)

# 🔒 高危·人在环工具（与 mcp-services 的 confirm_token 工具一致；
# interrupt 是体验层，confirm_token 是服务端最后防线，双层语义见方案 4.5）
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
    "handle_alert",
    "takeoff_to_point",
    "set_height_limit",
    "emergency_stop",
    "return_home",
    "debug_mode",
    "charge_control",
    "air_conditioner",
    "dock_cover",
]

LEAD_PROMPT = """\
你是无人机作业智能体的主控（Lead Agent）。

工作方式：
- 复杂任务先用 write_todos 列计划，随执行更新状态；
- 独立子任务用 task 工具并行派给子代理（飞控执行找 uav-ops、告警监控找
  monitor、数据分析找 data-analyst），自己只做规划与汇总；简单查询可直接调工具；
- 工具返回错误时如实报告，不得编造数据；
- 高危操作（派机、起飞、建删围栏等）会要求人工确认，等待确认结果，不要重复发起。

记忆：/memories/ 目录跨会话持久。用户明确的偏好（报告格式、常用片区、
称呼习惯等）用 write_file 记到 /memories/preferences.md；会话开始处理任务前，
如涉及个性化输出，先 read_file 查看该文件是否有相关偏好。

回答使用中文。
"""


def build_backend():
    """文件系统路由（技术方案 4.4）：/memories 跨会话持久（宿主 store），
    其余路径为会话内状态。namespace 暂为全局，R19 身份上线后按用户隔离。"""
    return CompositeBackend(
        default=StateBackend(),
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


def build_agent(
    tools: list,
    *,
    down_domains: list[str] | None = None,
    checkpointer: BaseCheckpointSaver | bool | None = None,
    store=None,
    skills: list[str] | None = None,
):
    """组装 Deep Agent。down_domains 会写入系统提示，避免模型摸不可用工具。"""
    prompt = LEAD_PROMPT
    if down_domains:
        prompt += f"\n注意：以下工具域当前不可用，不要尝试调用：{', '.join(down_domains)}。\n"
    tool_names = {getattr(t, "name", "") for t in tools}
    interrupts = {name: True for name in LOCKED_TOOLS if name in tool_names}
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
