"""环境配置。所有密钥经环境变量注入，绝不落代码与日志。

LLM 接入协议统一为 OpenAI 兼容格式：任何提供 /chat/completions 的端点均可
（OpenAI、DashScope、阿里 token plan、vLLM、Ollama、其他中转…），
通过 LLM_BASE_URL + LLM_API_KEY + 模型名三元组切换，代码不感知供应商。

支持按档位覆盖（混合路由，技术方案 4.8）：
  MODEL_STRONG / MODEL_CHEAP                — 两档模型名
  LLM_BASE_URL_STRONG / LLM_API_KEY_STRONG  — 强档单独端点（可选）
  LLM_BASE_URL_CHEAP  / LLM_API_KEY_CHEAP   — 便宜档单独端点（可选）
未设置档位覆盖时回落到全局 LLM_BASE_URL / LLM_API_KEY。
"""

import os
from pathlib import Path

from dotenv import load_dotenv

PROJECT_ROOT = Path(__file__).resolve().parents[2]

load_dotenv(PROJECT_ROOT / ".env")


def _env(*names: str, default: str = "") -> str:
    """按顺序取第一个非空环境变量（兼容旧命名）。"""
    for n in names:
        v = os.environ.get(n)
        if v:
            return v
    return default


# 全局 LLM 端点（DASHSCOPE_* 为兼容旧命名的回落）
LLM_API_KEY = _env("LLM_API_KEY", "DASHSCOPE_API_KEY")
LLM_BASE_URL = _env(
    "LLM_BASE_URL",
    "DASHSCOPE_BASE_URL",
    default="https://token-plan.cn-beijing.maas.aliyuncs.com/compatible-mode/v1",
)

# 档位：模型名 + 可选的端点/key 覆盖
MODEL_STRONG = _env("MODEL_STRONG", default="qwen3.7-plus")
MODEL_CHEAP = _env("MODEL_CHEAP", default="qwen3.7-plus")


def llm_tier(tier: str) -> dict:
    """返回某档位的 (model, base_url, api_key) 配置。"""
    suffix = tier.upper()
    return {
        "model": MODEL_STRONG if tier == "strong" else MODEL_CHEAP,
        "base_url": _env(f"LLM_BASE_URL_{suffix}", default=LLM_BASE_URL),
        "api_key": _env(f"LLM_API_KEY_{suffix}", default=LLM_API_KEY),
    }


# MCP
UAV_MCP_API_KEY = os.environ.get("UAV_MCP_API_KEY", "")
MCP_CONFIG_PATH = Path(
    os.environ.get("MCP_CONFIG_PATH", PROJECT_ROOT / "config" / "mcp_config.json")
)

# 会话工作区根目录（P0 沙箱：每会话一个受限子目录）
WORKSPACE_ROOT = Path(os.environ.get("WORKSPACE_ROOT", PROJECT_ROOT / ".workspace"))
