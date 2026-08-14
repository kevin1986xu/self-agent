"""自助登录：账号 + 会话（用户体系第 4 步）。

- 密码：scrypt（标准库，随机盐，不引第三方依赖），只存哈希；
- 会话 token：随机 43 字符，库中只存 SHA-256 散列，默认 7 天过期；
- 注册模式 REGISTER_MODE：open（默认）/ invite（需 REGISTER_INVITE_CODE）/ closed；
- 登录限速：同一用户名连续失败 5 次锁 60 秒（进程内计数，MVP 口径）；
- 每个账号绑定一条 user_identity（channel='web'）——与渠道身份同一张人表，
  记忆/审计/审批的归属键全网统一为 u<user_id>。
"""

from __future__ import annotations

import hashlib
import os
import secrets
import time

from . import identity

SESSION_TTL_S = int(os.environ.get("SESSION_TTL_S", 7 * 24 * 3600))

_DDL = """
CREATE TABLE IF NOT EXISTS user_account (
    account_id BIGSERIAL PRIMARY KEY,
    username TEXT UNIQUE NOT NULL,
    password_hash TEXT NOT NULL,
    salt TEXT NOT NULL,
    user_id BIGINT REFERENCES user_identity(user_id),
    created_at TIMESTAMPTZ DEFAULT now()
);
CREATE TABLE IF NOT EXISTS auth_session (
    token_hash TEXT PRIMARY KEY,
    account_id BIGINT REFERENCES user_account(account_id) ON DELETE CASCADE,
    created_at TIMESTAMPTZ DEFAULT now(),
    expires_at TIMESTAMPTZ NOT NULL
);
"""

_fail_counts: dict[str, list[float]] = {}


def _conn():
    conn = identity._conn()
    conn.execute(_DDL)
    return conn


def _hash(password: str, salt: bytes) -> str:
    return hashlib.scrypt(password.encode(), salt=salt, n=2**14, r=8, p=1).hex()


def _valid_username(u: str) -> bool:
    import re

    return bool(re.fullmatch(r"[a-zA-Z0-9_\-一-鿿]{2,32}", u))


def register(username: str, password: str, *, invite_code: str | None = None) -> dict:
    mode = os.environ.get("REGISTER_MODE", "open")
    if mode == "closed":
        raise ValueError("注册已关闭，请联系管理员")
    if mode == "invite" and invite_code != os.environ.get("REGISTER_INVITE_CODE"):
        raise ValueError("邀请码错误")
    if not _valid_username(username):
        raise ValueError("用户名需 2-32 位字母/数字/下划线/中文")
    if len(password) < 8:
        raise ValueError("密码至少 8 位")
    salt = secrets.token_bytes(16)
    user_id = identity.get_or_create_user("web", username, username)
    with _conn() as c:
        exists = c.execute("SELECT 1 FROM user_account WHERE username=%s", (username,)).fetchone()
        if exists:
            raise ValueError("用户名已存在")
        c.execute(
            "INSERT INTO user_account (username, password_hash, salt, user_id) VALUES (%s,%s,%s,%s)",
            (username, _hash(password, salt), salt.hex(), user_id))
    return {"username": username, "user_id": user_id}


def login(username: str, password: str) -> dict:
    now = time.time()
    fails = [t for t in _fail_counts.get(username, []) if now - t < 60]
    if len(fails) >= 5:
        raise ValueError("失败次数过多，请 1 分钟后再试")
    with _conn() as c:
        row = c.execute(
            "SELECT account_id, password_hash, salt, user_id FROM user_account WHERE username=%s",
            (username,)).fetchone()
    if not row or _hash(password, bytes.fromhex(row[2])) != row[1]:
        _fail_counts[username] = fails + [now]
        raise ValueError("用户名或密码错误")
    account_id, _, _, user_id = row[0], row[1], row[2], row[3]
    token = secrets.token_urlsafe(32)
    with _conn() as c:
        c.execute(
            "INSERT INTO auth_session (token_hash, account_id, expires_at)"
            " VALUES (%s,%s, now() + make_interval(secs => %s))",
            (hashlib.sha256(token.encode()).hexdigest(), account_id, SESSION_TTL_S))
        c.execute("DELETE FROM auth_session WHERE expires_at < now()")  # 顺手清过期
    role = identity.get_role(user_id)
    return {"token": token, "username": username, "user_id": user_id, "role": role,
            "expires_in": SESSION_TTL_S}


def logout(token: str) -> None:
    with _conn() as c:
        c.execute("DELETE FROM auth_session WHERE token_hash=%s",
                  (hashlib.sha256(token.encode()).hexdigest(),))


def verify_session(token: str) -> dict | None:
    """token → {username, user_id, role} 或 None。auth.py（Aegra 侧）与网关守卫共用。"""
    if not token:
        return None
    with _conn() as c:
        row = c.execute(
            """SELECT a.username, a.user_id FROM auth_session s
               JOIN user_account a ON a.account_id = s.account_id
               WHERE s.token_hash=%s AND s.expires_at > now()""",
            (hashlib.sha256(token.encode()).hexdigest(),)).fetchone()
    if not row:
        return None
    return {"username": row[0], "user_id": row[1], "role": identity.get_role(row[1])}
