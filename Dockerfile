# syntax=docker/dockerfile:1
FROM python:3.11-slim

# uv：从官方镜像直接 COPY，避免 curl 安装脚本
COPY --from=ghcr.io/astral-sh/uv:latest /uv /uvx /usr/local/bin/

# 系统依赖：CJK/emoji 字体（防中文页面乱码影响选择器）+ tzdata（供 TZ 使用）
# Chromium 运行库在 uv sync 之后用 `playwright install-deps chromium` 走 apt 安装
RUN apt-get update && apt-get install -y --no-install-recommends \
        fonts-noto-color-emoji \
        fonts-wqy-zenhei \
        fonts-freefont-ttf \
        ca-certificates \
        tzdata \
    && rm -rf /var/lib/apt/lists/*

WORKDIR /app
ENV PYTHONUNBUFFERED=1 \
    PYTHONIOENCODING=utf-8

# 先装依赖（利用层缓存）
COPY pyproject.toml uv.lock .python-version ./
RUN uv sync --frozen --no-dev \
    && uv run playwright install-deps chromium \
    && uv run python -m cloakbrowser install \
    && rm -rf ~/.cache/uv

# 代码
COPY checkin.py ./
COPY webui.py ./
COPY utils/ ./utils/
COPY scripts/ ./scripts/
COPY docker/entrypoint.sh /entrypoint.sh
RUN chmod +x /entrypoint.sh

# /app/data 为持久化挂载点（balance_hash、browser_profiles、截图都写这里）
RUN mkdir -p /app/data
VOLUME /app/data

ENTRYPOINT ["/entrypoint.sh"]
