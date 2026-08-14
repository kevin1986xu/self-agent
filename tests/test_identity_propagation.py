"""下游身份传递单测：contextvar → httpx 请求头注入（MockTransport 捕获）。"""

import sys
from pathlib import Path
from types import SimpleNamespace

sys.path.insert(0, str(Path(__file__).parents[1] / "src"))

import os

os.environ.setdefault("DASHSCOPE_API_KEY", "sk-dummy-for-test")

import httpx
import pytest

from self_agent import identity_propagation as ip  # noqa: E402


@pytest.mark.asyncio
async def test_headers_injected_from_contextvar():
    captured = {}

    def handler(request: httpx.Request) -> httpx.Response:
        captured.update(dict(request.headers))
        return httpx.Response(200, json={})

    factory = ip.make_httpx_factory("drone-platform")
    client = factory(headers={"X-API-Key": "svc"})
    client._transport = httpx.MockTransport(handler)

    token = ip.current_caller.set(
        {"user_key": "u23", "bindings": {"drone-platform": "zhang.san"}})
    try:
        await client.post("http://x/mcp", json={})
    finally:
        ip.current_caller.reset(token)
        await client.aclose()
    assert captured["x-api-key"] == "svc"              # 服务级密钥保留
    assert captured["x-acting-user"] == "u23"          # 统一身份恒发
    assert captured["x-on-behalf-of"] == "zhang.san"   # 目标系统绑定身份


@pytest.mark.asyncio
async def test_no_caller_no_identity_headers():
    captured = {}

    def handler(request: httpx.Request) -> httpx.Response:
        captured.update(dict(request.headers))
        return httpx.Response(200, json={})

    client = ip.make_httpx_factory("drone-platform")()
    client._transport = httpx.MockTransport(handler)
    await client.post("http://x/mcp", json={})
    await client.aclose()
    assert "x-acting-user" not in captured
    assert "x-on-behalf-of" not in captured


@pytest.mark.asyncio
async def test_unbound_system_sends_acting_user_only():
    captured = {}

    def handler(request: httpx.Request) -> httpx.Response:
        captured.update(dict(request.headers))
        return httpx.Response(200, json={})

    client = ip.make_httpx_factory("other-system")()
    client._transport = httpx.MockTransport(handler)
    token = ip.current_caller.set({"user_key": "u7", "bindings": {"drone-platform": "x"}})
    try:
        await client.post("http://x/mcp", json={})
    finally:
        ip.current_caller.reset(token)
        await client.aclose()
    assert captured["x-acting-user"] == "u7"
    assert "x-on-behalf-of" not in captured  # 该系统未绑定


def test_resolve_caller_shared_for_anonymous():
    req = SimpleNamespace(runtime=SimpleNamespace(context={}), tool_call={})
    assert ip.resolve_caller(req) == {"user_key": "shared", "bindings": {}}
