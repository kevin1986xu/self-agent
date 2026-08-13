"""模型档位（技术方案 4.8）。强档：Lead/doc-writer；便宜档：research/data-analyst。

接入协议统一为 OpenAI 兼容格式，供应商无关；混合路由（如强档换 Claude 的
OpenAI 兼容中转、便宜档留 Qwen）只需设置对应档位的环境变量，代码不动。
"""

from langchain_openai import ChatOpenAI

from . import settings


def build_model(tier: str = "strong") -> ChatOpenAI:
    cfg = settings.llm_tier(tier)
    return ChatOpenAI(
        model=cfg["model"],
        api_key=cfg["api_key"],
        base_url=cfg["base_url"],
        temperature=0,
    )
