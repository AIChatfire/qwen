"""gunicorn 配置 —— 单进程 uvicorn worker。

**WORKERS 默认 1 是架构约束，不是保守参数**：账号节奏（提交最小间隔 / signin 全局节流 /
风控冷却）都是进程内状态，N 个 worker 会把节奏按 N 倍放大 —— 那正是上游风控最敏感的维度。
要提吞吐的正确顺序：先把单账号节奏调稳（`QWEN_SUBMIT_MIN_INTERVAL` / 多账号池），
再考虑共享后端改造。
"""
from __future__ import annotations

import os

bind = f"{os.environ.get('HOST', '0.0.0.0')}:{os.environ.get('PORT', '8400')}"

workers = int(os.environ.get("WORKERS", "1"))
# ⚠️ 用独立包 `uvicorn-worker`，**不要**写 `uvicorn.workers.UvicornWorker`（已弃用，终将移除）。
worker_class = "uvicorn_worker.UvicornWorker"

# 提交/查询本身是秒级（不阻塞在"等出片"上——轮询由调用方 GET 驱动）
timeout = int(os.environ.get("GUNICORN_TIMEOUT", "120"))
graceful_timeout = 30
keepalive = 5

accesslog = "-"
errorlog = "-"
loglevel = os.environ.get("LOG_LEVEL", "INFO").lower()
