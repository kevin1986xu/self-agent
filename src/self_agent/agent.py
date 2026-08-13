"""框架核心组装（技术方案 4.1）。"""

from langgraph.checkpoint.base import BaseCheckpointSaver

from deepagents import create_deep_agent

from .model import build_model

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
]

LEAD_PROMPT = """\
你是无人机作业智能体的主控（Lead Agent）。

工作方式：
- 复杂任务先用 write_todos 列计划，随执行更新状态；
- 独立子任务用 task 工具并行派给子代理，自己只做规划与汇总；
- 工具返回错误时如实报告，不得编造数据；
- 高危操作（派机、起飞、建删围栏等）会要求人工确认，等待确认结果，不要重复发起。

回答使用中文。
"""

# 专家子代理（M1 起从 DB 配置加载；M0 先内置最小集）
SUBAGENTS = [
    {
        "name": "research",
        "description": "网页研究与资料汇总类任务（不操作无人机平台）",
        "system_prompt": "你是研究助理，负责检索汇总资料，输出结构化结论。使用中文。",
        "model": build_model("cheap"),
    },
]


def build_agent(
    tools: list,
    *,
    down_domains: list[str] | None = None,
    checkpointer: BaseCheckpointSaver | bool | None = None,
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
        subagents=SUBAGENTS,
        interrupt_on=interrupts or None,
        checkpointer=checkpointer,
        skills=skills,
        name="self-agent",
    )
