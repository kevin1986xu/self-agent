"""self-agent CLI（借鉴 deepseek-harness 的 CLI 接入形态）。

三个子命令：
  self-agent login                       # 登录（token 存 ~/.self-agent/credentials.json）
  self-agent run "任务" [-p 项目]         # headless：跑完打印最终回答退出（脚本/CI 友好）
  self-agent chat [-p 项目]               # 交互 REPL（审批口令「同意 N」直接输入即可）

走网关渠道（channel=local）：消息留痕/审批卡片/身份归属/项目路由全套复用，
CLI 本质上是网关的一个终端形态客户端。
"""

from __future__ import annotations

import argparse
import getpass
import json
import os
import sys
import time
import uuid
from pathlib import Path

import httpx

GATEWAY = os.environ.get("SELF_AGENT_GATEWAY", "http://127.0.0.1:8400").rstrip("/")
CRED_PATH = Path.home() / ".self-agent" / "credentials.json"


def _creds() -> dict:
    if CRED_PATH.exists():
        return json.loads(CRED_PATH.read_text())
    return {}


def cmd_login(_args) -> int:
    username = input("用户名: ").strip()
    password = getpass.getpass("密码: ")
    r = httpx.post(f"{GATEWAY}/auth/login",
                   json={"username": username, "password": password}, timeout=15)
    if r.status_code != 200:
        print(f"登录失败: {r.json().get('detail', r.status_code)}", file=sys.stderr)
        return 1
    d = r.json()
    CRED_PATH.parent.mkdir(parents=True, exist_ok=True)
    CRED_PATH.write_text(json.dumps({"username": d["username"], "token": d["token"],
                                     "role": d["role"]}, ensure_ascii=False))
    CRED_PATH.chmod(0o600)
    print(f"已登录：{d['username']}（{d['role']}），令牌 7 天有效")
    return 0


def _send_and_wait(conv: str, user: str, text: str, project: str | None,
                   *, timeout_s: int = 600, seen: int = 0) -> tuple[list[str], int]:
    """经网关发消息，轮询 outbox 直到有新回推。返回 (新消息, 新水位)。"""
    payload = {"user": user, "name": user, "conv": conv, "text": text}
    if project:
        payload["project"] = project
    r = httpx.post(f"{GATEWAY}/channels/local/webhook", json=payload, timeout=15)
    r.raise_for_status()
    deadline = time.time() + timeout_s
    while time.time() < deadline:
        time.sleep(2)
        msgs = httpx.get(f"{GATEWAY}/channels/local/outbox/{conv}", timeout=10).json()["messages"]
        if len(msgs) > seen:
            return msgs[seen:], len(msgs)
    return [], seen


def cmd_run(args) -> int:
    creds = _creds()
    user = creds.get("username") or f"cli-{getpass.getuser()}"
    conv = f"cli-{uuid.uuid4().hex[:8]}"
    try:
        new, _ = _send_and_wait(conv, user, args.task, args.project, timeout_s=args.timeout)
    except httpx.HTTPError as e:
        print(f"网关不可达: {e}", file=sys.stderr)
        return 2
    if not new:
        print("超时未收到回复", file=sys.stderr)
        return 3
    print("\n\n".join(new))
    # headless 场景遇到待审批：如实输出卡片并以非零码退出，交由调用方决策
    return 4 if "审批单号" in new[-1] else 0


def cmd_chat(args) -> int:
    creds = _creds()
    user = creds.get("username") or f"cli-{getpass.getuser()}"
    conv = f"cli-{uuid.uuid4().hex[:8]}"
    seen = 0
    print(f"self-agent chat · 项目={args.project or '网关默认'} · 用户={user} · exit 退出")
    while True:
        try:
            line = input("你> ").strip()
        except (EOFError, KeyboardInterrupt):
            break
        if not line or line in ("exit", "quit"):
            break
        try:
            new, seen = _send_and_wait(conv, user, line, args.project,
                                       timeout_s=args.timeout, seen=seen)
        except httpx.HTTPError as e:
            print(f"[网关错误] {e}", file=sys.stderr)
            continue
        for m in new:
            print(f"\n🤖 {m}\n")
        if not new:
            print("[超时未收到回复]", file=sys.stderr)
    return 0


def main() -> None:
    ap = argparse.ArgumentParser(prog="self-agent", description="self-agent 终端客户端")
    sub = ap.add_subparsers(dest="cmd", required=True)
    sub.add_parser("login", help="登录并保存令牌")
    p_run = sub.add_parser("run", help="headless：执行一个任务，打印回答后退出")
    p_run.add_argument("task")
    p_run.add_argument("-p", "--project", default=None)
    p_run.add_argument("--timeout", type=int, default=600)
    p_chat = sub.add_parser("chat", help="交互对话")
    p_chat.add_argument("-p", "--project", default=None)
    p_chat.add_argument("--timeout", type=int, default=600)
    args = ap.parse_args()
    rc = {"login": cmd_login, "run": cmd_run, "chat": cmd_chat}[args.cmd](args)
    sys.exit(rc)


if __name__ == "__main__":
    main()
