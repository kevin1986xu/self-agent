"""P0 沙箱后端（M2-1，技术方案 4.4）。

WorkspaceShellBackend = LocalShellBackend + 虚拟路径改写：
文件工具用虚拟绝对路径（/skills/...、/work/...），而 execute 的 shell 跑在
真实文件系统（cwd=root_dir）——agent 会自然地把技能列表里的虚拟路径写进命令，
这里把命令中的 /skills、/work 前缀改写为相对路径，两种写法都能执行。

P0 已知边界（接受并记录）：shell 本身无隔离（LocalShellBackend 官方语义），
靠受限 PATH + 不继承环境 + 超时兜底；P1 换 Docker SandboxBackendProtocolV2。
"""

import re

from deepagents.backends import LocalShellBackend

# 仅改写工作区内已知的顶层目录，词边界防止误伤（如 URL 里的 /work/）
_VPATH = re.compile(r"(?<![\w./-])/(skills|work|memories)(?=[/\s'\"]|$)")


def rewrite_virtual_paths(command: str) -> str:
    return _VPATH.sub(r"\1", command)


class WorkspaceShellBackend(LocalShellBackend):
    def execute(self, command: str, **kwargs):
        return super().execute(rewrite_virtual_paths(command), **kwargs)

    async def aexecute(self, command: str, **kwargs):
        return await super().aexecute(rewrite_virtual_paths(command), **kwargs)
