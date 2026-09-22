# qwen-service —— chat.qwen.ai 视频（t2v / i2v）的火山方舟 Seedance 契约出口
#
# ⚠️ `CMD` 里那对**括号不能省**：目标是**工厂**而不是模块级 `app` 对象。
#    写成 `app.main:app` 会得到 `App failed to load.`（`tests/test_wiring.py` 钉住这条）。

FROM python:3.13-slim AS base

# --- 版本与来源（发布工作流注入；label 也是 GHCR 包与仓库关联的依据）
ARG APP_VERSION=0.0.0-dev
LABEL org.opencontainers.image.title="qwen" \
      org.opencontainers.image.version="$APP_VERSION" \
      org.opencontainers.image.source="https://github.com/AIChatfire/qwen" \
      org.opencontainers.image.licenses="MIT"

ENV PYTHONUNBUFFERED=1 \
    PYTHONDONTWRITEBYTECODE=1 \
    PIP_NO_CACHE_DIR=1 \
    PIP_DISABLE_PIP_VERSION_CHECK=1 \
    PORT=8400 \
    DATA_DIR=/app/var \
    TZ=Asia/Shanghai

WORKDIR /app

# --- 依赖单独一层：改代码不会让依赖重装
COPY requirements.txt ./
RUN pip install --no-cache-dir -r requirements.txt

# --- 应用代码（docs / tests / .github / var 不进镜像，见 .dockerignore）
COPY app ./app
COPY gunicorn_conf.py ./

# --- 非 root 运行
# 任务库与 hmac 密钥默认落在 DATA_DIR（SQLite）⇒ 目录必须对运行用户可写。
RUN useradd --create-home --uid 10001 qwen \
    && mkdir -p /app/var \
    && chown -R qwen:qwen /app
USER qwen

EXPOSE 8400

# --- 存活探针：**零依赖、不触上游、不消耗额度**（/healthz 不查库、不发上游请求）
HEALTHCHECK --interval=30s --timeout=5s --start-period=10s --retries=3 \
    CMD python -c "import os,sys,urllib.request;p=os.environ.get('PORT','8400');sys.exit(0 if urllib.request.urlopen('http://127.0.0.1:'+p+'/healthz',timeout=4).status==200 else 1)"

CMD ["gunicorn", "-c", "gunicorn_conf.py", "app.main:create_app()"]
