"""项目（Project）抽象——多项目管理的核心（通用化重构）。

每个项目一个目录 `projects/<name>/`：
  project.json    身份与策略：display_name / role_prompt（业务人格与纪律）/
                  locked_tools（人在环工具）/ mcp_headers_env（每个 header 取哪个
                  环境变量）/ knowledge_scope / skills（可见技能组）
  mcp_config.json 本项目接入的 MCP（外部/公共均可，键名任意）
  subagents.json  本项目的专家子代理（可选）

核心代码不含任何业务词——业务全部生活在项目档案里。
新开项目 = 新建一个目录，零代码改动。
"""

from __future__ import annotations

import json
import os
from dataclasses import dataclass, field
from pathlib import Path

from . import settings

PROJECTS_ROOT = Path(os.environ.get("PROJECTS_ROOT", settings.PROJECT_ROOT / "projects"))


@dataclass
class Project:
    name: str
    display_name: str
    role_prompt: str
    locked_tools: list[str] = field(default_factory=list)
    mcp_headers_env: dict[str, str] = field(default_factory=dict)
    knowledge_scope: str = "default"
    skills: list[str] = field(default_factory=list)
    dir: Path = None  # type: ignore[assignment]

    @property
    def workspace(self) -> Path:
        return settings.WORKSPACE_ROOT / self.name

    @property
    def mcp_config_path(self) -> Path:
        return self.dir / "mcp_config.json"

    @property
    def subagents_path(self) -> Path:
        return self.dir / "subagents.json"

    def mcp_headers(self) -> dict[str, str]:
        """按 project.json 的映射从环境变量解析 headers（密钥不落档案）。"""
        out = {}
        for header, env_name in self.mcp_headers_env.items():
            v = os.environ.get(env_name, "")
            if v:
                out[header] = v
        return out


def list_projects() -> list[str]:
    if not PROJECTS_ROOT.exists():
        return []
    return sorted(p.name for p in PROJECTS_ROOT.iterdir()
                  if (p / "project.json").exists())


def load_project(name: str) -> Project:
    d = PROJECTS_ROOT / name
    spec_path = d / "project.json"
    if not spec_path.exists():
        raise ValueError(f"项目不存在: {name}（可用: {list_projects()}）")
    spec = json.loads(spec_path.read_text())
    return Project(
        name=name,
        display_name=spec.get("display_name", name),
        role_prompt=spec.get("role_prompt", ""),
        locked_tools=spec.get("locked_tools", []),
        mcp_headers_env=spec.get("mcp_headers_env", {}),
        knowledge_scope=spec.get("knowledge_scope", name),
        skills=spec.get("skills", []),
        dir=d,
    )
