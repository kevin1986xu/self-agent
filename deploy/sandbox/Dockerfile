# P1 沙箱执行环境（M3-1）：技能脚本在此容器内运行，与宿主隔离。
# 工作区经 bind mount 进 /workspace，文件工具仍在宿主侧（快），仅 shell 隔离。
FROM python:3.13-slim

RUN pip install --no-cache-dir \
    python-pptx python-docx openpyxl reportlab pypdf \
    && useradd -m -u 1000 runner

# 中文字体（PDF 技能用）
RUN apt-get update && apt-get install -y --no-install-recommends fonts-wqy-zenhei \
    && rm -rf /var/lib/apt/lists/*

USER runner
WORKDIR /workspace
# 常驻睡眠，execute 经 docker exec 进入
CMD ["sleep", "infinity"]
