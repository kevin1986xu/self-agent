"""消息网关服务（M2-7~10，端口 8400）。

链路：渠道 webhook → 验签/身份登记 → 会话映射 Aegra thread → 后台执行 run
→ 完成回推；interrupt → 登记审批单 + 回推确认卡片 → /approvals/{id}/decision
→ resume → 回推最终结果。

运行：DATABASE_URL=... .venv/bin/python -m uvicorn self_agent.gateway.app:app --port 8400
环境：AEGRA_BASE（默认 http://127.0.0.1:2027）、ASSISTANT_ID（默认 agent）
"""

from __future__ import annotations

import asyncio
import json
import logging
import os

import httpx
from fastapi import BackgroundTasks, FastAPI, HTTPException, Request

from . import identity
from .channels import CHANNELS, LocalChannel

logger = logging.getLogger(__name__)

AEGRA_BASE = os.environ.get("AEGRA_BASE", "http://127.0.0.1:2027").rstrip("/")
ASSISTANT_ID = os.environ.get("ASSISTANT_ID", "agent")

app = FastAPI(title="self-agent gateway")

# conversation → aegra thread 映射（进程内缓存 + Aegra metadata 持久检索）
_thread_cache: dict[str, str] = {}


async def _get_thread(client: httpx.AsyncClient, channel: str, conv: str) -> str:
    key = f"{channel}:{conv}"
    if key in _thread_cache:
        return _thread_cache[key]
    r = await client.post(f"{AEGRA_BASE}/threads/search",
                          json={"metadata": {"gateway_key": key}, "limit": 1})
    hits = r.json() if r.status_code == 200 else []
    if hits:
        tid = hits[0]["thread_id"]
    else:
        r = await client.post(f"{AEGRA_BASE}/threads",
                              json={"metadata": {"gateway_key": key, "channel": channel}})
        tid = r.json()["thread_id"]
    _thread_cache[key] = tid
    return tid


async def _stream_run(client: httpx.AsyncClient, thread_id: str, body: dict) -> dict:
    """跑一轮 run，返回 {'answer': str|None, 'interrupt': dict|None}。"""
    last = None
    async with client.stream(
        "POST", f"{AEGRA_BASE}/threads/{thread_id}/runs/stream",
        json={"assistant_id": ASSISTANT_ID, "stream_mode": ["values"], **body},
        timeout=600,
    ) as resp:
        async for line in resp.aiter_lines():
            if line.startswith("data:"):
                try:
                    d = json.loads(line[5:])
                except ValueError:
                    continue
                if isinstance(d, dict) and d.get("messages"):
                    last = d
    # interrupt 探测：读线程状态
    st = (await client.get(f"{AEGRA_BASE}/threads/{thread_id}/state")).json()
    for task in st.get("tasks") or []:
        for intr in task.get("interrupts") or []:
            return {"answer": None, "interrupt": intr.get("value")}
    answer = None
    if last:
        answer = str(last["messages"][-1].get("content") or "")
    return {"answer": answer, "interrupt": None}


def _format_card(approval_id: int, interrupt_value: dict) -> str:
    reqs = interrupt_value.get("action_requests") or []
    lines = ["## ⚠️ 高危操作待确认\n"]
    for r in reqs:
        lines.append(f"- **{r.get('name')}** 参数：`{json.dumps(r.get('args'), ensure_ascii=False)[:200]}`")
    lines.append(f"\n审批单号：**{approval_id}**")
    lines.append(f"回复「同意 {approval_id}」或「拒绝 {approval_id}」，或调用 "
                 f"POST /approvals/{approval_id}/decision")
    return "\n".join(lines)


async def _handle_message(channel_name: str, msg: dict) -> None:
    ch = CHANNELS[channel_name]
    user_id = identity.get_or_create_user(channel_name, msg["channel_user_id"], msg.get("display_name"))
    conv = msg["conversation_key"]
    text = msg["text"]
    async with httpx.AsyncClient() as client:
        thread_id = await _get_thread(client, channel_name, conv)

        # 快捷审批口令：同意/拒绝 <单号>
        import re

        m = re.match(r"^(同意|批准|拒绝)\s*(\d+)$", text)
        if m:
            approve = m.group(1) != "拒绝"
            rec = identity.decide_approval(int(m.group(2)), approve=approve, decided_by=user_id)
            if not rec:
                await ch.send(conv, f"审批单 {m.group(2)} 不存在或已处理。")
                return
            decision = ({"type": "approve"} if approve
                        else {"type": "reject", "message": f"用户#{user_id} 拒绝"})
            result = await _stream_run(client, rec["thread_id"],
                                       {"command": {"resume": {"decisions": [decision]}}})
        else:
            result = await _stream_run(
                client, thread_id,
                {"input": {"messages": [{"type": "human", "content": text}]}})

        if result["interrupt"]:
            summary = json.dumps(result["interrupt"].get("action_requests"), ensure_ascii=False)[:500]
            approval_id = identity.create_approval(thread_id, user_id, summary)
            await ch.send(conv, _format_card(approval_id, result["interrupt"]))
        elif result["answer"]:
            await ch.send(conv, result["answer"])


@app.post("/channels/{channel}/webhook")
async def webhook(channel: str, request: Request, background: BackgroundTasks):
    ch = CHANNELS.get(channel)
    if ch is None:
        raise HTTPException(404, f"未知渠道: {channel}")
    body = await request.body()
    if not ch.verify(dict(request.headers), body):
        raise HTTPException(401, "验签失败")
    msg = ch.parse(json.loads(body))
    if not msg["text"]:
        return {"ok": True, "skip": "empty"}
    background.add_task(_handle_message, channel, msg)  # 异步执行，webhook 立即返回
    return {"ok": True}


@app.post("/approvals/{approval_id}/decision")
async def decide(approval_id: int, payload: dict):
    """UI/API 审批入口：{"approve": true|false, "channel": "...", "channel_user_id": "..."}"""
    user_id = identity.get_or_create_user(payload.get("channel", "api"),
                                          str(payload.get("channel_user_id", "api")))
    rec = identity.decide_approval(approval_id, approve=bool(payload.get("approve")), decided_by=user_id)
    if not rec:
        raise HTTPException(409, "审批单不存在或已处理")
    decision = ({"type": "approve"} if payload.get("approve")
                else {"type": "reject", "message": f"用户#{user_id} 拒绝"})
    async with httpx.AsyncClient() as client:
        result = await _stream_run(client, rec["thread_id"],
                                   {"command": {"resume": {"decisions": [decision]}}})
    return {"ok": True, "answer": result["answer"], "interrupt": bool(result["interrupt"])}


@app.get("/channels/local/outbox/{conv}")
async def local_outbox(conv: str):
    """测试通道回推查询。"""
    return {"messages": LocalChannel.outbox.get(conv, [])}


@app.get("/healthz")
async def healthz():
    return {"status": "ok"}
