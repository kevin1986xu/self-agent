"""模型档位（技术方案 4.8）。强档：Lead/doc-writer；便宜档：research/data-analyst。

Qwen 不达标时的混合路由（Lead 切 Claude）只需改 MODEL_STRONG 环境变量指向
其他 OpenAI 兼容端点，或在此处按档位换实现。
"""

from langchain_openai import ChatOpenAI

from . import settings


def build_model(tier: str = "strong") -> ChatOpenAI:
    name = settings.MODEL_STRONG if tier == "strong" else settings.MODEL_CHEAP
    return ChatOpenAI(
        model=name,
        api_key=settings.DASHSCOPE_API_KEY,
        base_url=settings.DASHSCOPE_BASE_URL,
        temperature=0,
    )
