"""FastAPI 应用工厂 —— 路由 / 错误信封 / 鉴权 / 健康检查 一处收拢。

对外路由（**方舟契约范围冻结**：核心两个端点；方舟的列表与取消刻意不实现 ⇒ 路由不存在）：
    POST /api/v3/contents/generations/tasks       创建（只回 {"id": …}；容量不足默认排队）
    GET  /api/v3/contents/generations/tasks/{id}  查询（方舟任务对象形状；顺带推进任务）
    GET  /v1/models                              模型清单（**OpenAI 形态**，能力探测用）
    GET  /healthz /readyz /stats                 运维面（不含任何凭据原文）

⚠️ `GET /v1/models` **不属于方舟任务契约**：它是给 OpenAI 系客户端 / 网关做"能力探测"的清单，
   与"范围冻结"（方舟的列表 / 取消不做、不返回假数据）不冲突 —— 那是**任务列表**，
   这是**能力列表**。它不涉密 ⇒ 不校验 Key。

调试面：`X-Avm-Dry-Run: 1` 请求头 ⇒ 跑完整翻译后返回"将要发出的请求"，**零上游调用、零落库**。
"""
from __future__ import annotations

import asyncio
import contextlib
import hashlib
import hmac
import json
import logging
import secrets
import uuid
from contextlib import asynccontextmanager

from fastapi import FastAPI, Request
from fastapi.responses import JSONResponse

from . import __version__, models
from .config import Settings
from .coordinator import Coordinator
from .errors import AdapterError, AuthenticationError, InvalidParameterError
from .service import QwenVideoService
from .store import TaskStore
from .upstream.qwen.accounts import AccountPool
from .upstream.qwen.client import QwenClient

logger = logging.getLogger("qwen.main")

#: 账号池状态的 KV 键（额度计数 + 冷却；**不含 token**）
POOL_STATE_KEY = "state:pool"


def fingerprint(secret: str, key: str) -> str:
    """API Key → 指纹（HMAC-SHA256，永不落明文；裸 sha256 对低熵 Key 不够）。"""
    return "hmac-sha256:" + hmac.new(secret.encode(), key.encode(), hashlib.sha256).hexdigest()


def bind_pool_state(store: TaskStore, pool: AccountPool) -> None:
    """账号池的**耐久化**：启动时恢复、变更即落 KV ⇒ 重启不丢额度计数与冷却。

    token 刻意**不进持久层**（重启重新铸造，免费；避免凭据落盘）。
    """
    raw = store.kv_get(POOL_STATE_KEY)
    if raw:
        try:
            pool.restore(json.loads(raw))
        except ValueError:
            logger.warning("账号池状态恢复失败（忽略，按空状态起）")
    pool.on_change = lambda: store.kv_set(
        POOL_STATE_KEY, json.dumps(pool.snapshot(), ensure_ascii=False))


def create_app(settings: Settings | None = None, *, store: TaskStore | None = None,
               pool: AccountPool | None = None, client: QwenClient | None = None,
               service: QwenVideoService | None = None) -> FastAPI:
    settings = settings or Settings.from_env()
    logging.basicConfig(
        level=getattr(logging, settings.log_level.upper(), logging.INFO),
        format="%(asctime)s %(levelname)s %(name)s %(message)s",
    )
    store = store or TaskStore(settings.task_db)
    pool = pool or AccountPool(settings)
    client = client or QwenClient(settings)
    service = service or QwenVideoService(settings, store, pool, client)
    secret = settings.resolved_key_secret()
    bind_pool_state(store, pool)

    @asynccontextmanager
    async def lifespan(app: FastAPI):
        background: asyncio.Task | None = None
        if settings.coordinator_enabled:
            background = asyncio.create_task(
                Coordinator(service, store, settings).run(), name="qwen-coordinator")
        try:
            yield
        finally:
            if background is not None:
                background.cancel()
                with contextlib.suppress(asyncio.CancelledError, Exception):
                    await background
            client.close()

    app = FastAPI(title="qwen-service", version=__version__, lifespan=lifespan)

    @app.middleware("http")
    async def request_id_middleware(request: Request, call_next):
        request_id = request.headers.get("x-request-id") or str(uuid.uuid4())
        request.state.request_id = request_id
        response = await call_next(request)
        response.headers["x-request-id"] = request_id
        return response

    @app.exception_handler(AdapterError)
    async def adapter_error_handler(request: Request, exc: AdapterError):
        request_id = getattr(request.state, "request_id", "")
        headers: dict[str, str] = {}
        if exc.retry_after:
            headers["Retry-After"] = str(int(max(1.0, exc.retry_after)))
        return JSONResponse(status_code=exc.status_code,
                            content=exc.to_body(request_id), headers=headers)

    def credential_id_of(request: Request) -> str:
        if not settings.api_keys:
            # 未配置 API_KEYS ⇒ 鉴权关闭（仅限内网；接入文档已显式声明）
            return fingerprint(secret, "open")
        authorization = request.headers.get("authorization", "")
        if not authorization.lower().startswith("bearer "):
            raise AuthenticationError("缺少 Authorization: Bearer <API Key>")
        key = authorization[7:].strip()
        if not any(secrets.compare_digest(key, known) for known in settings.api_keys):
            raise AuthenticationError("API Key 无效")
        return fingerprint(secret, key)

    def credential_id_optional(request: Request) -> str | None:
        """**可选的**调用方 Key —— 只给「按 id 即凭据」的**读单条任务**用。

        三种情况分得很清（刻意不合并，与 `../jimeng` 同口径）：

        · **完全没带** `Authorization` ⇒ 返回 `None`，**放行**。
          理由：`task_id` 只在受理时返回给带 Key 的调用方，调用方可以把结果链接直接分享出去；
        · **带了但无效**（不在白名单）⇒ **照旧 401** —— 不能因为"反正放行"就把错的 Key
          蒙过去，那会让调用方的配置错误被静默吞掉（最难查的一类问题）；
        · **带了且有效、但不是该任务的属主** ⇒ 返回该指纹，由 `service.get` 判 404。
          ⚠️ 这里比 `jimeng` **严一档**：jimeng 对"非属主"与"没带"同一待遇（都放行），
          本服务保留**跨 Key 读 ⇒ 404**（ADR-003 口径）—— 不冲突：调用方要么不带 Key，
          要么用原 Key 读；而"拿着甲 Key 去探乙 Key 的任务"仍然读不到。
        """
        authorization = request.headers.get("authorization", "").strip()
        if not authorization:
            return None
        return credential_id_of(request)

    @app.post("/api/v3/contents/generations/tasks")
    async def create_generation_task(request: Request):
        credential_id = credential_id_of(request)
        try:
            body = await request.json()
        except ValueError as exc:
            raise InvalidParameterError("请求体不是合法 JSON") from exc
        if not isinstance(body, dict):
            raise InvalidParameterError("请求体必须是 JSON 对象")
        dry_run = request.headers.get("x-avm-dry-run", "").strip().lower() in ("1", "true", "yes")
        result = await asyncio.to_thread(service.create, body, credential_id, dry_run=dry_run)
        return JSONResponse(status_code=200, content=result)

    @app.get("/api/v3/contents/generations/tasks/{task_id}")
    async def get_generation_task(task_id: str, request: Request):
        """查询任务 —— **不强制 Key：`task_id` 本身就是凭据**（方舟语义，同 `../jimeng`）。

        带了 Key 才按归属过滤（不匹配 ⇒ 404，防跨 Key 探测）；完全没带 ⇒ 直接按 id 读。
        """
        credential_id = credential_id_optional(request)
        result = await asyncio.to_thread(service.get, task_id, credential_id)
        return JSONResponse(status_code=200, content=result)

    @app.get("/v1/models")
    async def list_models() -> dict:
        """本服务对外宣告的模型清单（**OpenAI 形态**）。

        只列**真正支持**的；刻意缺席的能力见 `app/models.py::DELIBERATE_ABSENCES`。
        **不校验 Key**：清单不涉密，而 OpenAI 系客户端 / 网关（new-api 等）常在填 Key 之前
        先探一次能力；这里返回 401 会让"探测失败"被误读成"服务不可用"。
        """
        return {"object": "list", "data": models.catalog()}

    @app.get("/healthz")
    async def healthz():
        return {"status": "ok", "version": __version__}

    @app.get("/readyz")
    async def readyz():
        store_ok = True
        try:
            store.count()
        except Exception:  # noqa: BLE001 - 探活不许炸
            store_ok = False
        return {
            "ready": bool(settings.ready and store_ok),
            "accounts": len(settings.accounts),
            "store": "ok" if store_ok else "error",
            "upstream": settings.base_url,
            "coordinator": settings.coordinator_enabled,
        }

    @app.get("/stats")
    async def stats():
        return {
            "accounts": pool.stats(),
            "tasks": {"total": store.count(), "active": store.count_active(),
                      "queued": store.count_queued()},
        }

    return app
