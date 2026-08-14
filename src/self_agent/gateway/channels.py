"""渠道适配器（M2-7/8）。

统一契约：verify(request) 验签 → parse(payload) 抽 (channel_user_id, display_name,
conversation_key, text) → send(conversation_key, markdown) 回推。

- local：本地测试通道（无签名，回复入内存队列供轮询）——确认卡片闭环的
  自动化测试载体；
- dingtalk：钉钉机器人（HMAC-SHA256 验签 + sessionWebhook 回推），填
  DINGTALK_APP_SECRET 即可联调；
- wechat_work：企业微信（AES 回调加解密），待 CorpID/Secret/Token 后按同一
  契约补 verify/parse/send —— 骨架已留。
"""

from __future__ import annotations

import base64
import hashlib
import hmac
import os
import time
from collections import defaultdict

import httpx


class LocalChannel:
    """测试通道：POST /channels/local/webhook {"user":..,"conv":..,"text":..}"""

    name = "local"
    outbox: dict[str, list[str]] = defaultdict(list)

    def verify(self, headers: dict, body: bytes) -> bool:
        return True

    def parse(self, payload: dict) -> dict:
        return {
            "channel_user_id": str(payload["user"]),
            "display_name": payload.get("name"),
            "conversation_key": str(payload.get("conv", payload["user"])),
            "text": payload["text"],
            "project": payload.get("project"),
        }

    async def send(self, conversation_key: str, markdown: str) -> None:
        self.outbox[conversation_key].append(markdown)


class DingtalkChannel:
    """钉钉企业内部机器人。验签：timestamp+secret 的 HMAC-SHA256。"""

    name = "dingtalk"

    def __init__(self) -> None:
        self.secret = os.environ.get("DINGTALK_APP_SECRET", "")
        self._session_webhooks: dict[str, str] = {}

    def verify(self, headers: dict, body: bytes) -> bool:
        if not self.secret:
            return False
        ts = headers.get("timestamp", "")
        sign = headers.get("sign", "")
        if not ts or abs(time.time() * 1000 - int(ts)) > 3600_000:
            return False
        raw = f"{ts}\n{self.secret}".encode()
        expect = base64.b64encode(hmac.new(self.secret.encode(), raw, hashlib.sha256).digest()).decode()
        return hmac.compare_digest(expect, sign)

    def parse(self, payload: dict) -> dict:
        conv = payload.get("conversationId", "")
        # sessionWebhook 有效期内可直接回推该会话
        if payload.get("sessionWebhook"):
            self._session_webhooks[conv] = payload["sessionWebhook"]
        return {
            "channel_user_id": payload.get("senderStaffId") or payload.get("senderId", ""),
            "display_name": payload.get("senderNick"),
            "conversation_key": conv,
            "text": (payload.get("text") or {}).get("content", "").strip(),
        }

    async def send(self, conversation_key: str, markdown: str) -> None:
        webhook = self._session_webhooks.get(conversation_key)
        if not webhook:
            raise RuntimeError(f"钉钉会话 {conversation_key} 无有效 sessionWebhook")
        async with httpx.AsyncClient(timeout=10) as client:
            await client.post(webhook, json={
                "msgtype": "markdown",
                "markdown": {"title": "无人机智能体", "text": markdown},
            })


class WeChatWorkChannel:
    """企业微信骨架：回调 AES 加解密与应用消息发送待凭据后实现（同一契约）。"""

    name = "wechat_work"

    def verify(self, headers: dict, body: bytes) -> bool:
        raise NotImplementedError("待 WECOM_CORP_ID/SECRET/TOKEN/AES_KEY 配置后实现")

    def parse(self, payload: dict) -> dict:
        raise NotImplementedError

    async def send(self, conversation_key: str, markdown: str) -> None:
        raise NotImplementedError


CHANNELS = {c.name: c for c in (LocalChannel(), DingtalkChannel(), WeChatWorkChannel())}
