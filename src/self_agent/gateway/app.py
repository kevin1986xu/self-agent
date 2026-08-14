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

logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(name)s %(message)s")
logger = logging.getLogger(__name__)

AEGRA_BASE = os.environ.get("AEGRA_BASE", "http://127.0.0.1:2027").rstrip("/")
AEGRA_TOKEN = os.environ.get("AEGRA_TOKEN", "")  # Aegra 开鉴权后网关的服务令牌


def _aegra_client() -> httpx.AsyncClient:
    headers = {"Authorization": f"Bearer {AEGRA_TOKEN}"} if AEGRA_TOKEN else {}
    return httpx.AsyncClient(headers=headers)
DEFAULT_PROJECT = os.environ.get("DEFAULT_PROJECT", "default")  # assistant_id = 项目名
APPROVAL_BASE = os.environ.get("APPROVAL_BASE", "http://127.0.0.1:8205").rstrip("/")
APPROVAL_ADMIN_KEY = os.environ.get("APPROVAL_ADMIN_KEY", "")

app = FastAPI(title="self-agent gateway")

GATEWAY_ADMIN_TOKEN = os.environ.get("GATEWAY_ADMIN_TOKEN", "")
# 豁免：渠道回调(自带验签)/健康/审批(渠道卡片按钮)/管理台页面外壳(数据仍走受保护 API)
_PUBLIC_PREFIXES = ("/channels/", "/healthz", "/approvals", "/admin", "/auth/", "/login")


@app.middleware("http")
async def _admin_guard(request: Request, call_next):
    if GATEWAY_ADMIN_TOKEN and not any(request.url.path.startswith(p) for p in _PUBLIC_PREFIXES):
        tk = request.headers.get("X-Admin-Token", "")
        if tk != GATEWAY_ADMIN_TOKEN:
            from . import accounts

            session = accounts.verify_session(tk) if tk else None
            if not (session and session["role"] == "admin"):
                from fastapi.responses import JSONResponse

                return JSONResponse(
                    {"detail": "需要 X-Admin-Token（管理令牌或 admin 账号的登录令牌）"},
                    status_code=401)
    return await call_next(request)
app.add_middleware(
    CORSMiddleware,
    allow_origins=os.environ.get("GATEWAY_CORS", "http://localhost:3000").split(","),
    allow_methods=["*"], allow_headers=["*"],
)

# conversation → aegra thread 映射（进程内缓存 + Aegra metadata 持久检索）
_thread_cache: dict[str, str] = {}


async def _get_thread(client: httpx.AsyncClient, project: str, channel: str, conv: str) -> str:
    key = f"{project}:{channel}:{conv}"
    if key in _thread_cache:
        return _thread_cache[key]
    r = await client.post(f"{AEGRA_BASE}/threads/search",
                          json={"metadata": {"gateway_key": key}, "limit": 1})
    hits = r.json() if r.status_code == 200 else []
    if hits:
        tid = hits[0]["thread_id"]
    else:
        r = await client.post(f"{AEGRA_BASE}/threads",
                              json={"metadata": {"gateway_key": key, "project": project,
                                                 "channel": channel}})
        tid = r.json()["thread_id"]
    _thread_cache[key] = tid
    return tid


async def _stream_run(client: httpx.AsyncClient, thread_id: str, body: dict,
                      *, project: str = None, user_id: int | None = None,
                      auto_approve: bool = False) -> dict:
    """跑一轮 run，返回 {'answer': str|None, 'interrupt': dict|None}。

    auto_approve：[SYSTEM_CONFIRMATION] 续跑场景——带 token 的重调会再次
    触发框架层 interrupt，同意链来自同一次用户确认，自动放行（最多 2 次）。
    """
    async def _once(payload: dict):
        last = None
        async with client.stream(
            "POST", f"{AEGRA_BASE}/threads/{thread_id}/runs/stream",
            json={"assistant_id": project or DEFAULT_PROJECT,
                  "stream_mode": ["values"],
                  "context": {"user_id": user_id, "project": project or DEFAULT_PROJECT},
                  **payload},
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
        st = (await client.get(f"{AEGRA_BASE}/threads/{thread_id}/state")).json()
        for task in st.get("tasks") or []:
            for intr in task.get("interrupts") or []:
                return last, intr.get("value")
        return last, None

    last, interrupt = await _once(body)
    hops = 0
    while interrupt and auto_approve and hops < 2:
        hops += 1
        last, interrupt = await _once(
            {"command": {"resume": {"decisions": [{"type": "approve"}]}}})
    if interrupt:
        return {"answer": None, "interrupt": interrupt}
    answer = str(last["messages"][-1].get("content") or "") if last else None
    return {"answer": answer, "interrupt": None}


async def _bridge_server_approval(client: httpx.AsyncClient, thread_id: str,
                                  user_id: int, pending_interrupt: dict | None,
                                  *, project: str = None) -> dict | None:
    """双层人在环桥接（方案 4.5）：框架层批准后，工具无 token 执行只会在
    审批服务登记待确认单——本函数代表已同意的用户完成第二层：

    ① 查最新待确认单并管理端批准换 confirm_token（审批人=X-User-Id，留痕）；
    ② 若本轮 run 停在同一工具的新 interrupt 上（模型重试），用 **edit 决策**
       把 confirm_token 注入工具参数直接放行执行；
    ③ 若 run 已干净结束，注入 [SYSTEM_CONFIRMATION] 指令续跑（带 token 重调
       再触发的 interrupt 自动放行）。同意链均来自同一次用户确认。
    """
    if not APPROVAL_ADMIN_KEY:
        return None
    admin = {"X-Admin-Key": APPROVAL_ADMIN_KEY}
    try:
        r = await client.get(f"{APPROVAL_BASE}/api/approval/pending",
                             params={"status": "pending"}, headers=admin, timeout=10)
        orders = r.json() if r.status_code == 200 else []
    except httpx.HTTPError:
        return None
    if not orders:
        return None
    order = orders[-1]  # 最新登记（本轮 run 刚产生的）
    a = await client.post(
        f"{APPROVAL_BASE}/api/approval/{order['action_id']}/approve",
        headers={**admin, "X-User-Id": f"gateway-user-{user_id}"}, timeout=10)
    if a.status_code != 200:
        logger.warning("审批服务批准失败 %s: %s", a.status_code, a.text[:200])
        return None
    tok = a.json()

    if pending_interrupt:
        reqs = pending_interrupt.get("action_requests") or []
        if reqs and reqs[0].get("name") == tok["action"]:
            edited = {"name": reqs[0]["name"],
                      "args": {**(reqs[0].get("args") or {}), "confirm_token": tok["confirm_token"]}}
            return await _stream_run(
                client, thread_id,
                {"command": {"resume": {"decisions": [{"type": "edit", "edited_action": edited}]}}},
                project=project, auto_approve=True)
    msg = (f"[SYSTEM_CONFIRMATION] 动作 {tok['action']}（单号 {tok['action_id']}）已获人工批准，"
           f"confirm_token={tok['confirm_token']}。请携带该 confirm_token 参数重新执行原操作，"
           f"完成后继续既定任务。")
    return await _stream_run(client, thread_id,
                             {"input": {"messages": [{"type": "human", "content": msg}]}},
                             project=project, auto_approve=True)


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
    project = msg.get("project") or DEFAULT_PROJECT
    user_id = identity.get_or_create_user(channel_name, msg["channel_user_id"], msg.get("display_name"))
    conv = msg["conversation_key"]
    text = msg["text"]
    identity.log_message(project, channel_name, conv, "in", text, user_id)

    async def send(markdown: str) -> None:
        identity.log_message(project, channel_name, conv, "out", markdown, None)
        await ch.send(conv, markdown)

    async with _aegra_client() as client:
        thread_id = await _get_thread(client, project, channel_name, conv)
        logger.info("handle[%s/%s:%s] thread=%s 开跑", project, channel_name, conv, thread_id)

        # 快捷审批口令：同意/拒绝 <单号>
        import re

        m = re.match(r"^(同意|批准|拒绝)\s*(\d+)$", text)
        if m:
            approve = m.group(1) != "拒绝"
            if (os.environ.get("APPROVAL_REQUIRE_ADMIN") == "1"
                    and identity.get_role(user_id) != "admin"):
                await send(f"审批单 {m.group(2)}：你没有审批权限（需要 admin 角色）。")
                return
            rec = identity.decide_approval(int(m.group(2)), approve=approve, decided_by=user_id)
            if not rec:
                await send(f"审批单 {m.group(2)} 不存在或已处理。")
                return
            decision = ({"type": "approve"} if approve
                        else {"type": "reject", "message": f"用户#{user_id} 拒绝"})
            result = await _stream_run(client, rec["thread_id"],
                                       {"command": {"resume": {"decisions": [decision]}}},
                                       project=project, user_id=user_id)
            if approve:
                bridged = await _bridge_server_approval(client, rec["thread_id"], user_id,
                                                        result["interrupt"], project=project)
                if bridged:
                    result = bridged
        else:
            result = await _stream_run(
                client, thread_id,
                {"input": {"messages": [{"type": "human", "content": text}]}},
                project=project, user_id=user_id)

        if result["interrupt"]:
            summary = json.dumps(result["interrupt"].get("action_requests"), ensure_ascii=False)[:500]
            approval_id = identity.create_approval(thread_id, user_id, summary)
            await send(_format_card(approval_id, result["interrupt"]))
        elif result["answer"]:
            await send(result["answer"])


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
        project = payload.get("project") or DEFAULT_PROJECT
        result = await _stream_run(client, rec["thread_id"],
                                   {"command": {"resume": {"decisions": [decision]}}},
                                   project=project)
        if payload.get("approve"):
            bridged = await _bridge_server_approval(client, rec["thread_id"], user_id,
                                                    result["interrupt"], project=project)
            if bridged:
                result = bridged
    return {"ok": True, "answer": result["answer"], "interrupt": bool(result["interrupt"])}


@app.get("/channels/local/outbox/{conv}")
async def local_outbox(conv: str):
    """测试通道回推查询。"""
    return {"messages": LocalChannel.outbox.get(conv, [])}


@app.get("/healthz")
async def healthz():
    return {"status": "ok"}


@app.post("/auth/register")
async def api_register(payload: dict):
    from . import accounts

    try:
        info = accounts.register(payload.get("username", "").strip(), payload.get("password", ""),
                                 invite_code=payload.get("invite_code"))
    except ValueError as e:
        raise HTTPException(400, str(e)) from e
    return {"ok": True, **info}


@app.post("/auth/login")
async def api_login(payload: dict):
    from . import accounts

    try:
        return accounts.login(payload.get("username", "").strip(), payload.get("password", ""))
    except ValueError as e:
        raise HTTPException(401, str(e)) from e


@app.post("/auth/logout")
async def api_logout(payload: dict):
    from . import accounts

    accounts.logout(payload.get("token", ""))
    return {"ok": True}


@app.get("/login", response_class=HTMLResponse)
async def login_page():
    return (Path(__file__).parent / "login.html").read_text()


@app.get("/projects")
async def api_projects():
    from ..project import list_projects, load_project

    out = []
    for name in list_projects():
        p = load_project(name)
        out.append({"name": name, "display_name": p.display_name,
                    "locked_tools": len(p.locked_tools), "skills": p.skills})
    return {"projects": out, "default": DEFAULT_PROJECT}


@app.get("/users")
async def api_users():
    return {"users": identity.list_users()}


@app.post("/users/{user_id}/role")
async def api_set_role(user_id: int, payload: dict):
    try:
        identity.set_role(user_id, payload.get("role", ""))
    except (ValueError, AssertionError) as e:
        raise HTTPException(400, str(e)) from e
    return {"ok": True}


@app.get("/messages")
async def api_messages(project: str | None = None, channel: str | None = None,
                       conv: str | None = None, limit: int = 100):
    return {"messages": identity.list_messages(project, channel, conv, limit)}


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
            rel_parts = f.relative_to(root).parts
            if f.is_file() and "skills" not in rel_parts:  # 各项目的技能装配目录不算产物
                rel = str(f.relative_to(root))
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


# ============ Playbook 模板 + 定时任务（M3-3/4） ============

@app.get("/playbooks")
async def api_playbooks():
    from . import playbook

    return {"playbooks": playbook.list_playbooks()}


@app.post("/playbooks")
async def api_playbook_upsert(spec: dict):
    from . import playbook

    if not spec.get("name") or not spec.get("prompt_template"):
        raise HTTPException(400, "需要 name 和 prompt_template")
    if spec.get("cron"):
        from croniter import croniter

        if not croniter.is_valid(spec["cron"]):
            raise HTTPException(400, f"cron 表达式非法: {spec['cron']}")
    return playbook.upsert(spec)


@app.delete("/playbooks/{pb_id}")
async def api_playbook_delete(pb_id: int):
    from . import playbook

    playbook.delete(pb_id)
    return {"ok": True}


async def _launch_playbook(pb: dict, params: dict | None, trigger: str) -> None:
    from . import playbook

    text = playbook.render(pb, params)
    playbook.mark_run(pb["id"])
    msg = {"channel_user_id": f"playbook:{pb['name']}", "display_name": f"模板[{pb['name']}]",
           "conversation_key": pb["conversation_key"], "text": text,
           "project": pb.get("project")}
    logger.info("playbook %s 发起（%s）→ %s:%s", pb["name"], trigger,
                pb["channel"], pb["conversation_key"])
    try:
        await _handle_message(pb["channel"], msg)
        logger.info("playbook %s 完成", pb["name"])
    except Exception:  # noqa: BLE001 —— 模板任务失败必须留痕并回推
        logger.exception("playbook %s 执行失败", pb["name"])
        try:
            await CHANNELS[pb["channel"]].send(
                pb["conversation_key"], f"⚠️ 模板任务「{pb['name']}」执行失败，详见网关日志。")
        except Exception:  # noqa: BLE001
            pass


@app.post("/playbooks/{pb_id}/run")
async def api_playbook_run(pb_id: int, payload: dict, background: BackgroundTasks):
    from . import playbook

    pb = next((p for p in playbook.list_playbooks() if p["id"] == pb_id), None)
    if not pb:
        raise HTTPException(404, "模板不存在")
    background.add_task(_launch_playbook, pb, payload.get("params"), "manual")
    return {"ok": True, "rendered": playbook.render(pb, payload.get("params"))}


@app.on_event("startup")
async def _cron_loop():
    async def loop():
        from . import playbook

        while True:
            try:
                for pb in playbook.due_playbooks():
                    await _launch_playbook(pb, None, "cron")
            except Exception:  # noqa: BLE001 —— 调度失败下一轮重试
                logger.exception("cron 轮询失败")
            await asyncio.sleep(30)

    asyncio.create_task(loop())
