"""错误分类 —— 把上游五花八门的失败收敛成方舟形状的对外错误。

对外错误体（对齐方舟原生形态）:
    {"error": {"code": "...", "message": "... Request ID: <rid>", "type": "..."}}

🔴 三条纪律（都是既有项目踩过的）：
  1. **风控 ≠ 限流**：都是 429，但风控（RGV587）重试会加深标记（不自动重试，给足 retry_after）；
     限流退避即可。
  2. **额度耗尽 ≠ 限流**：都是 429，但额度耗尽重试无意义（等到下一个 UTC 日）。
  3. **不要过度脱敏**：调用方需要的"可行动事实"（哪个字段错了、是不是风控、要不要退避）必须留下；
     去掉的只是**上游实现细节**。我们自己的报文原样保留，只统一补 Request ID。
"""
from __future__ import annotations

from typing import Any


class AdapterError(Exception):
    """适配层错误基类，自带对外错误体所需的元信息。"""

    status_code: int = 500
    code: str = "InternalError"
    err_type: str = "InternalServiceError"
    retryable: bool = False

    def __init__(
        self,
        message: str,
        *,
        param: str | None = None,
        retry_after: float | None = None,
        extra: dict[str, Any] | None = None,
    ) -> None:
        super().__init__(message)
        self.message = message
        self.param = param
        self.retry_after = retry_after
        self.extra = dict(extra or {})

    def to_body(self, request_id: str) -> dict[str, Any]:
        err: dict[str, Any] = {
            "code": self.code,
            "message": f"{self.message} Request ID: {request_id}",
            "type": self.err_type,
        }
        if self.param:
            err["param"] = self.param
        if self.extra:
            err["detail"] = dict(self.extra)
        return {"error": err}

    def __str__(self) -> str:  # pragma: no cover - 便于日志
        return f"{type(self).__name__}: {self.message}"


class InvalidParameterError(AdapterError):
    """请求本身写错（我们能判定）→ 调用方改请求。"""

    status_code = 400
    code = "InvalidParameter"
    err_type = "BadRequest"


class AuthenticationError(AdapterError):
    """调用方 Key 缺失/无效（本服务的门禁）。"""

    status_code = 401
    code = "AuthenticationError"
    err_type = "Unauthorized"


class NotFoundError(AdapterError):
    """任务不存在，或不属于本次请求的 Key。"""

    status_code = 404
    code = "InvalidEndpointOrModel.NotFound"
    err_type = "NotFound"


class RateLimitedError(AdapterError):
    """并发/节奏限流：账号池暂时排不上（退避可重试）。"""

    status_code = 429
    code = "RateLimitExceeded"
    err_type = "TooManyRequests"
    retryable = True


class RiskControlError(AdapterError):
    """上游 x5sec 风控（RGV587）：**不自动重试**，让调用方按 429 退避。"""

    status_code = 429
    code = "ServerOverloaded"
    err_type = "TooManyRequests"


class QuotaExhaustedError(AdapterError):
    """账号额度耗尽（视频 3 次/天，t2v 与 i2v 共用）。"""

    status_code = 429
    code = "QuotaExceeded"
    err_type = "TooManyRequests"


class CredentialUnavailableError(AdapterError):
    """上游凭据铸造/续期失败 —— **部署问题，不是调用方的错**（503 语义）。

    刻意与 400/401 分开：混淆会让调用方去改请求体，越改越远。
    """

    status_code = 503
    code = "CredentialUnavailable"
    err_type = "ServiceUnavailable"
    retryable = True


class UpstreamError(AdapterError):
    """上游 5xx / 非 JSON / WAF 页等，本层无法归因的故障。"""

    status_code = 502
    code = "InternalServiceError"
    err_type = "InternalServiceError"
    retryable = True


class UpstreamTimeoutError(AdapterError):
    status_code = 504
    code = "InternalServiceError"
    err_type = "InternalServiceError"
    retryable = True


__all__ = [
    "AdapterError",
    "AuthenticationError",
    "CredentialUnavailableError",
    "InvalidParameterError",
    "NotFoundError",
    "QuotaExhaustedError",
    "RateLimitedError",
    "RiskControlError",
    "UpstreamError",
    "UpstreamTimeoutError",
]
