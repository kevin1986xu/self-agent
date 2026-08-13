"""环境配置。所有密钥经环境变量注入，绝不落代码与日志。"""

import os
from pathlib import Path

from dotenv import load_dotenv

PROJECT_ROOT = Path(__file__).resolve().parents[2]

load_dotenv(PROJECT_ROOT / ".env")

# LLM（阿里 token plan 的 OpenAI 兼容端点；api_key 必须显式传，见技术方案 4.8。
# 与既有 deerflow/config.yaml 的生效配置对齐：qwen3.7-plus @ token-plan）
DASHSCOPE_API_KEY = os.environ.get("DASHSCOPE_API_KEY", "")
DASHSCOPE_BASE_URL = os.environ.get(
    "DASHSCOPE_BASE_URL",
    "https://token-plan.cn-beijing.maas.aliyuncs.com/compatible-mode/v1",
)
MODEL_STRONG = os.environ.get("MODEL_STRONG", "qwen3.7-plus")
MODEL_CHEAP = os.environ.get("MODEL_CHEAP", "qwen3.7-plus")

# MCP
UAV_MCP_API_KEY = os.environ.get("UAV_MCP_API_KEY", "")
MCP_CONFIG_PATH = Path(
    os.environ.get("MCP_CONFIG_PATH", PROJECT_ROOT / "config" / "mcp_config.json")
)

# 会话工作区根目录（P0 沙箱：每会话一个受限子目录）
WORKSPACE_ROOT = Path(os.environ.get("WORKSPACE_ROOT", PROJECT_ROOT / ".workspace"))
