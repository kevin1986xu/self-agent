"""Aegra 接入鉴权（用户体系第 2 步，aegra.json 的 auth.path 指向本文件）。

Token 注册表来自环境变量 AUTH_TOKENS：`token:身份:角色` 逗号分隔，如
  AUTH_TOKENS=tk-alice-xxx:alice:admin,tk-bob-yyy:bob:member
Web 端（agent-chat-ui 设置页 API Key 栏）/ API 调用方带
`Authorization: Bearer <token>`。

未设置 AUTH_TOKENS 时为开发模式：放行为匿名（与此前行为一致）；
设置后无有效 token 一律 401。鉴权用户身份会进入 run 的上下文
（langgraph_auth_user），框架据此做按用户的记忆隔离与审计归属。
"""

import os

from langgraph_sdk import Auth

auth = Auth()


def _registry() -> dict[str, dict]:
    out = {}
    for item in os.environ.get("AUTH_TOKENS", "").split(","):
        parts = item.strip().split(":")
        if len(parts) >= 2 and parts[0]:
            out[parts[0]] = {"identity": parts[1],
                             "role": parts[2] if len(parts) > 2 else "member"}
    return out


@auth.authenticate
async def authenticate(headers: dict) -> dict:
    registry = _registry()
    if not registry:  # 开发模式：未配置 token 即匿名放行
        return {"identity": "anonymous", "permissions": [], "role": "member"}

    def _h(name: str) -> str:
        for k, v in headers.items():
            key = k.decode() if isinstance(k, bytes) else k
            if key.lower() == name:
                return v.decode() if isinstance(v, bytes) else v
        return ""

    token = _h("authorization").removeprefix("Bearer ").strip() or _h("x-api-key")
    user = registry.get(token)
    if not user:
        raise Auth.exceptions.HTTPException(status_code=401, detail="无效或缺失的访问令牌")
    return {"identity": user["identity"], "permissions": [user["role"]],
            "role": user["role"], "display_name": user["identity"]}
