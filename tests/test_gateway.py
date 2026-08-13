"""M2-7/8/9 网关单测：钉钉验签、本地通道解析、卡片格式（无 DB/网络部分）。"""

import base64
import hashlib
import hmac
import sys
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parents[1] / "src"))

import os

os.environ.setdefault("DASHSCOPE_API_KEY", "sk-dummy-for-test")
os.environ["DINGTALK_APP_SECRET"] = "test-secret"

from self_agent.gateway.app import _format_card  # noqa: E402
from self_agent.gateway.channels import DingtalkChannel, LocalChannel  # noqa: E402


def _sign(secret: str, ts: str) -> str:
    raw = f"{ts}\n{secret}".encode()
    return base64.b64encode(hmac.new(secret.encode(), raw, hashlib.sha256).digest()).decode()


def test_dingtalk_verify_ok_and_reject():
    ch = DingtalkChannel()
    ts = str(int(time.time() * 1000))
    assert ch.verify({"timestamp": ts, "sign": _sign("test-secret", ts)}, b"") is True
    assert ch.verify({"timestamp": ts, "sign": _sign("wrong", ts)}, b"") is False
    stale = str(int(time.time() * 1000) - 2 * 3600_000)  # 超时窗
    assert ch.verify({"timestamp": stale, "sign": _sign("test-secret", stale)}, b"") is False


def test_dingtalk_parse():
    ch = DingtalkChannel()
    msg = ch.parse({"conversationId": "cid1", "senderStaffId": "u1", "senderNick": "K",
                    "sessionWebhook": "https://oapi/x", "text": {"content": " 你好 "}})
    assert msg == {"channel_user_id": "u1", "display_name": "K",
                   "conversation_key": "cid1", "text": "你好"}
    assert ch._session_webhooks["cid1"] == "https://oapi/x"


def test_local_channel_roundtrip():
    ch = LocalChannel()
    msg = ch.parse({"user": "u", "text": "hi"})
    assert msg["conversation_key"] == "u"


def test_card_format_contains_action_and_id():
    card = _format_card(7, {"action_requests": [{"name": "dispatch_drone", "args": {"drone_id": "X"}}]})
    assert "dispatch_drone" in card and "审批单号：**7**" in card and "同意 7" in card
