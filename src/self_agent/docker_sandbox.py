"""P1 Docker 沙箱后端（M3-1，技术方案 4.4）。

设计：文件工具沿用宿主侧（工作区目录，快且语义不变），仅 execute 进容器
（同一工作区 bind mount 到 /workspace）。相比全容器方案，改动面最小且
拿到了关键收益——技能脚本/shell 与宿主进程、密钥、文件系统隔离。

- 镜像：deploy/sandbox/Dockerfile（python + 文档库 + 中文字体，非 root 运行）；
- 容器生命周期：懒启动、常驻复用（P1 全会话共享，与工作区一致）；
- 切换：SANDBOX_MODE=docker 环境变量（默认 local 即 WorkspaceShellBackend）。
"""

from __future__ import annotations

import logging
import shlex
import subprocess

from deepagents.backends.local_shell import ExecuteResponse

from . import settings
from .sandbox import WorkspaceShellBackend, rewrite_virtual_paths

logger = logging.getLogger(__name__)

IMAGE = "self-agent-sandbox:latest"
CONTAINER = "self-agent-sandbox"
DEFAULT_TIMEOUT = 180


def _sh(args: list[str], timeout: int = 60) -> subprocess.CompletedProcess:
    return subprocess.run(args, capture_output=True, text=True, timeout=timeout)


def ensure_container() -> bool:
    """容器在则复用，不在则启动（镜像缺失返回 False 由调用方降级）。"""
    r = _sh(["docker", "inspect", "-f", "{{.State.Running}}", CONTAINER])
    if r.returncode == 0 and r.stdout.strip() == "true":
        return True
    _sh(["docker", "rm", "-f", CONTAINER])
    settings.WORKSPACE_ROOT.mkdir(parents=True, exist_ok=True)
    r = _sh(["docker", "run", "-d", "--name", CONTAINER,
             "--network", "none",                      # 无网络：脚本外联在 P1 直接物理隔绝
             "--memory", "1g", "--cpus", "1",
             "-v", f"{settings.WORKSPACE_ROOT.resolve()}:/workspace",
             IMAGE])
    if r.returncode != 0:
        logger.warning("沙箱容器启动失败（将降级本地 shell）：%s", r.stderr.strip()[:200])
        return False
    return True


class DockerShellBackend(WorkspaceShellBackend):
    """execute 走容器，文件工具继承宿主实现。容器挂载工作区总根，
    各项目工作区对应容器内 /workspace/<相对路径>。"""

    def _workdir(self) -> str:
        try:
            rel = self.root_dir.resolve().relative_to(settings.WORKSPACE_ROOT.resolve())
            return f"/workspace/{rel}" if str(rel) != "." else "/workspace"
        except (ValueError, AttributeError):
            return "/workspace"

    def execute(self, command: str, *, timeout: int | None = None) -> ExecuteResponse:
        if not ensure_container():
            return super().execute(command, timeout=timeout)
        cmd = rewrite_virtual_paths(command)
        t = timeout or DEFAULT_TIMEOUT
        try:
            r = _sh(["docker", "exec", "-w", self._workdir(), CONTAINER,
                     "sh", "-c", cmd], timeout=t)
        except subprocess.TimeoutExpired:
            return ExecuteResponse(output=f"[timeout] 超过 {t}s", exit_code=124, truncated=False)
        out = r.stdout + ("".join(f"[stderr] {line}\n" for line in r.stderr.splitlines())
                          if r.stderr else "")
        if len(out) > 100_000:
            return ExecuteResponse(output=out[:100_000], exit_code=r.returncode, truncated=True)
        return ExecuteResponse(output=out, exit_code=r.returncode, truncated=False)

    async def aexecute(self, command: str, **kwargs):
        import asyncio

        return await asyncio.to_thread(self.execute, command, **kwargs)


def build_shell_backend(root_dir=None):
    """按 SANDBOX_MODE 返回 shell 后端（build_backend 的注入点）。
    root_dir：项目工作区（默认工作区总根，兼容单项目场景）。"""
    import os

    mode = os.environ.get("SANDBOX_MODE", "local")
    root = root_dir or settings.WORKSPACE_ROOT
    venv_bin = settings.PROJECT_ROOT / ".venv" / "bin"
    kwargs = dict(root_dir=root, virtual_mode=True, timeout=DEFAULT_TIMEOUT,
                  env={"PATH": f"{venv_bin}:/usr/bin:/bin"}, inherit_env=False)
    if mode == "docker":
        logger.info("沙箱模式：docker（execute 进容器，--network none）")
        return DockerShellBackend(**kwargs)
    return WorkspaceShellBackend(**kwargs)
