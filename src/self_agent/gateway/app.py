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
from pathlib import Path

import httpx
from fastapi import BackgroundTasks, FastAPI, HTTPException, Request, UploadFile
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import FileResponse, HTMLResponse

from .. import settings
from . import identity
from .channels import CHANNELS, LocalChannel

logger = logging.getLogger(__name__)

AEGRA_BASE = os.environ.get("AEGRA_BASE", "http://127.0.0.1:2027").rstrip("/")
ASSISTANT_ID = os.environ.get("ASSISTANT_ID", "agent")

app = FastAPI(title="self-agent gateway")
app.add_middleware(
    CORSMiddleware,
    allow_origins=os.environ.get("GATEWAY_CORS", "http://localhost:3000").split(","),
    allow_methods=["*"], allow_headers=["*"],
)

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


# ============ 工作区文件（M2-11 前端文件面板数据源） ============

def _safe_workspace_path(rel: str):
    p = (settings.WORKSPACE_ROOT / rel.lstrip("/")).resolve()
    if not str(p).startswith(str(settings.WORKSPACE_ROOT.resolve())):
        raise HTTPException(400, "非法路径")
    return p


@app.get("/files")
async def list_files():
    root = settings.WORKSPACE_ROOT
    out = []
    if root.exists():
        for f in sorted(root.rglob("*")):
            if f.is_file() and "skills" not in f.parts[:len(root.parts) + 1]:
                rel = str(f.relative_to(root))
                if rel.startswith(("skills/",)):
                    continue
                out.append({"path": rel, "size": f.stat().st_size,
                            "mtime": int(f.stat().st_mtime)})
    return {"files": out}


@app.get("/files/download")
async def download_file(path: str):
    p = _safe_workspace_path(path)
    if not p.is_file():
        raise HTTPException(404, "文件不存在")
    return FileResponse(p, filename=p.name)


# ============ 技能管理 REST（M2-12） ============

@app.get("/skills")
async def api_skills():
    from .. import skill_manager

    return {"skills": skill_manager.list_skills()}


@app.post("/skills/import")
async def api_skill_import(payload: dict):
    from .. import skill_manager

    url = (payload.get("url") or "").strip()
    if not url.startswith("https://github.com/"):
        raise HTTPException(400, "仅支持 GitHub 地址（zip 上传用 /skills/upload）")
    try:
        return {"imported": skill_manager.import_github(url, imported_by=payload.get("by", "web"))}
    except ValueError as e:
        raise HTTPException(400, str(e)) from e


@app.post("/skills/upload")
async def api_skill_upload(file: UploadFile):
    from .. import skill_manager

    data = await file.read()
    try:
        return {"imported": skill_manager._import_zip_bytes(
            data, source_type="zip", source_url=file.filename, pinned_ref=None, imported_by="web")}
    except ValueError as e:
        raise HTTPException(400, str(e)) from e


@app.post("/skills/{name}/status")
async def api_skill_status(name: str, payload: dict):
    from .. import skill_manager

    try:
        skill_manager.set_status(name, payload["status"], reviewed_by=payload.get("by", "web"))
    except (ValueError, AssertionError) as e:
        raise HTTPException(400, str(e)) from e
    return {"ok": True}


@app.get("/skills/{name}/findings")
async def api_skill_findings(name: str):
    from .. import skill_manager

    with skill_manager._conn() as c:
        row = c.execute("SELECT scan_findings FROM skill_registry WHERE name=%s", (name,)).fetchone()
    if not row:
        raise HTTPException(404, "技能不存在")
    return {"findings": row[0]}


# ============ 知识库 REST（M2-12） ============

@app.get("/knowledge")
async def api_knowledge():
    from .. import knowledge

    return {"docs": knowledge.list_docs()}


@app.post("/knowledge/upload")
async def api_knowledge_upload(file: UploadFile):
    import tempfile

    from .. import knowledge

    suffix = os.path.splitext(file.filename or "")[1]
    with tempfile.NamedTemporaryFile(suffix=suffix, delete=False) as tf:
        tf.write(await file.read())
        tmp = tf.name
    try:
        info = knowledge.add_document(tmp, uploaded_by="web")
        info["filename"] = file.filename
        with knowledge._conn() as c:
            c.execute("UPDATE knowledge_doc SET filename=%s WHERE id=%s",
                      (file.filename, info["doc_id"]))
        return info
    except ValueError as e:
        raise HTTPException(400, str(e)) from e
    finally:
        os.unlink(tmp)


@app.delete("/knowledge/{doc_id}")
async def api_knowledge_delete(doc_id: int):
    from .. import knowledge

    knowledge.remove(doc_id)
    return {"ok": True}


@app.get("/knowledge/search")
async def api_knowledge_search(q: str):
    from .. import knowledge

    return {"hits": knowledge.search(q)}


# ============ 审批列表（M2-12） ============

@app.get("/approvals")
async def api_approvals(status: str = "pending"):
    with identity._conn() as c:
        rows = c.execute(
            """SELECT a.id, a.thread_id, a.action_summary, a.status, a.created_at::text,
                      u.display_name, u.channel
               FROM gateway_approval a LEFT JOIN user_identity u ON u.user_id = a.user_id
               WHERE (%s = 'all' OR a.status = %s) ORDER BY a.id DESC LIMIT 100""",
            (status, status)).fetchall()
    keys = ["id", "thread_id", "action", "status", "created_at", "user", "channel"]
    return {"approvals": [dict(zip(keys, r)) for r in rows]}


# ============ 管理台（单页应用） ============

@app.get("/admin", response_class=HTMLResponse)
async def admin_page():
    page = Path(__file__).parent / "admin.html"
    return page.read_text()
