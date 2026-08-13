# self-agent 应用镜像（Aegra 服务端 + 网关共用，M3-8）
FROM python:3.13-slim

RUN apt-get update && apt-get install -y --no-install-recommends git fonts-wqy-zenhei \
    && rm -rf /var/lib/apt/lists/* \
    && pip install --no-cache-dir uv

WORKDIR /app
COPY pyproject.toml uv.lock ./
COPY src ./src
RUN uv pip install --system -e . \
    && pip install --no-cache-dir \
       "aegra-api @ git+https://github.com/ibbybuilds/aegra#subdirectory=libs/aegra-api"

COPY config ./config
COPY skills ./skills
COPY graph_entry.py aegra.json ./

# 容器内运行时 execute 本身已在容器隔离内，SANDBOX_MODE 保持 local
ENV WORKSPACE_ROOT=/data/workspace SANDBOX_MODE=local
VOLUME ["/data/workspace"]

# 默认起 Aegra；网关服务用 compose 覆盖 command
CMD ["uvicorn", "aegra_api.main:app", "--host", "0.0.0.0", "--port", "2027"]
