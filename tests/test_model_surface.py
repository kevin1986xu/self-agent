"""模型可见面快照（借鉴 dsh python-minimal-model-visible-snapshot）。

钉住每个项目「模型将看到的可控面」：组装后的系统提示词全文、锁定工具集、
子代理编制（名称/工具清单/档位）、技能组、知识工具开关、deepagents 版本
（其内置 harness 提示随版本变化）。任何漂移给出完整 diff——我们的回归失败
曾多次源于提示词面被顺手改动而无人察觉。

更新快照：UPDATE_SNAPSHOTS=1 .venv/bin/python -m pytest tests/test_model_surface.py
（更新后请 review diff 再提交——快照即合同。）
"""

import difflib
import json
import os
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parents[1] / "src"))

os.environ.setdefault("DASHSCOPE_API_KEY", "sk-dummy-for-test")

SNAPSHOT = Path(__file__).parent / "snapshots" / "model_surface.json"


def _surface() -> dict:
    import importlib.metadata as md

    from self_agent.agent import CORE_PROMPT
    from self_agent.project import list_projects, load_project

    out = {"deepagents": md.version("deepagents"), "projects": {}}
    for name in list_projects():
        p = load_project(name)
        subagents = {}
        if p.subagents_path.exists():
            for sa_name, spec in json.loads(p.subagents_path.read_text()).items():
                subagents[sa_name] = {"tools": spec.get("tools", []),
                                      "model": spec.get("model", "strong")}
        out["projects"][name] = {
            "system_prompt": p.role_prompt + "\n" + CORE_PROMPT,
            "locked_tools": p.locked_tools,
            "skills": p.skills,
            "knowledge_scope": p.knowledge_scope,
            "subagents": subagents,
        }
    return out


def test_model_visible_surface_pinned():
    actual = json.dumps(_surface(), ensure_ascii=False, indent=2, sort_keys=True)
    if os.environ.get("UPDATE_SNAPSHOTS") == "1" or not SNAPSHOT.exists():
        SNAPSHOT.parent.mkdir(parents=True, exist_ok=True)
        SNAPSHOT.write_text(actual + "\n")
        return
    expected = SNAPSHOT.read_text().rstrip("\n")
    if actual != expected:
        diff = "\n".join(difflib.unified_diff(
            expected.splitlines(), actual.splitlines(),
            fromfile="snapshot(合同)", tofile="当前代码", lineterm=""))
        raise AssertionError(
            "模型可见面发生漂移（若为有意变更，UPDATE_SNAPSHOTS=1 重跑并 review 快照 diff）：\n"
            + diff)
