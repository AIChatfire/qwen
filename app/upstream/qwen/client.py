"""qwen 网页端 HTTP 客户端 —— 头、请求体、响应判读。

🔴 三条最容易做错、且都有实测判决：
  1. **请求头必须完整**：缺 `Sec-Fetch-*` / `Timezone` / `X-Accel-Buffering`、或
     `Accept` 形态不对，会被判自动化 ⇒ `RGV587` 风控（2026-09-18 一整天的误诊，
     根因就是探针请求头不全；修法是逐字段对齐 `biz-api::build_headers`）。
  2. **`version: 0.2.0` 是 write 端点的硬门槛**：缺它 → HTTP 200 +
     `{"code":"Bad_Request"}`（文案像"请求体写错了"，极易误诊）；`chats/new` 不要求。
  3. **查询端点 HTTP 恒 200**：真码在响应头 `x-actual-status-code`；body 里 `success`
     才可信。**不得把 `task_status` 缺失解释成"还在跑"**（会无限轮询）。
"""
from __future__ import annotations

import time
import uuid
from datetime import datetime

import httpx

from ...config import Settings
from ...errors import (
    AuthenticationError,
    InvalidParameterError,
    NotFoundError,
    QuotaExhaustedError,
    RiskControlError,
    UpstreamError,
    UpstreamTimeoutError,
)
from .accounts import mask_email

_WEEKDAYS = ("Mon", "Tue", "Wed", "Thu", "Fri", "Sat", "Sun")
_MONTHS = ("Jan", "Feb", "Mar", "Apr", "May", "Jun",
           "Jul", "Aug", "Sep", "Oct", "Nov", "Dec")

#: 与抓包逐字一致的提交体固定片段。
FEATURE_CONFIG = {
    "thinking_enabled": False,
    "output_schema": "phase",
    "research_mode": "normal",
    "auto_thinking": False,
    "thinking_mode": "Fast",
    "auto_search": True,
}


def tz_header() -> str:
    now = datetime.now()
    return (f"{_WEEKDAYS[now.weekday()]} {_MONTHS[now.month - 1]} {now.day:02d} "
            f"{now.year} {now.hour:02d}:{now.minute:02d}:{now.second:02d} GMT+0800")


class QwenClient:
    def __init__(self, settings: Settings, *, transport: httpx.BaseTransport | None = None) -> None:
        self.settings = settings
        self._client = httpx.Client(
            base_url=settings.base_url,
            timeout=settings.upstream_timeout,
            transport=transport,
            trust_env=settings.trust_env,
        )

    def close(self) -> None:
        self._client.close()

    # ------------------------------------------------------------------ 头部

    def headers(self, token: str, *, referer: str | None = None,
                extra_cookies: str = "") -> dict[str, str]:
        s = self.settings
        cookie = f"token={token}"
        if extra_cookies:
            cookie = f"{cookie}; {extra_cookies}"
        return {
            "Accept": "application/json",
            "Content-Type": "application/json",
            "User-Agent": s.user_agent,
            "Origin": s.base_url,
            "Referer": referer or f"{s.base_url}/",
            "source": "web",
            "version": s.version_header,
            "Accept-Language": "zh-CN,zh;q=0.9",
            "Connection": "keep-alive",
            "Sec-Fetch-Dest": "empty",
            "Sec-Fetch-Mode": "cors",
            "Sec-Fetch-Site": "same-origin",
            "Timezone": tz_header(),
            "X-Request-Id": str(uuid.uuid4()),
            "X-Accel-Buffering": "no",
            "sec-ch-ua": '"Chromium";v="152", "Not?A_Brand";v="24", "Google Chrome";v="152"',
            "sec-ch-ua-mobile": "?0",
            "sec-ch-ua-platform": '"macOS"',
            "Cookie": cookie,
        }

    # ------------------------------------------------------------------ 判读

    def _decode(self, resp: httpx.Response, *, op: str, raise_business: bool = True) -> dict:
        """解包 + 归类。`raise_business=False` 时把 `success:false` 原样返回 ——
        查询端点用它：404 / 401 是**任务状态**，由 service 层解释，不是抛异常的场景。"""
        text = resp.text or ""
        if "aliyun_waf" in text:
            raise UpstreamError(f"{op}: 上游返回 WAF 挑战页（凭据/出口问题）")
        try:
            payload = resp.json()
        except ValueError:
            payload = None
        if isinstance(payload, dict):
            ret = payload.get("ret")
            if isinstance(ret, list) and any("RGV587" in str(x) or "FAIL_SYS" in str(x) for x in ret):
                raise RiskControlError(
                    f"{op}: 上游 x5sec 风控（RGV587）—— 按 429 退避，勿连续重试", retry_after=60.0)
        if "RGV587" in text or "FAIL_SYS_USER_VALIDATE" in text:
            raise RiskControlError(f"{op}: 上游 x5sec 风控（RGV587）", retry_after=60.0)
        if resp.status_code >= 400:
            raise UpstreamError(f"{op}: 上游 HTTP {resp.status_code}: {text[:200]}")
        if payload is None:
            raise UpstreamError(f"{op}: 上游响应不是 JSON（{resp.headers.get('content-type', '?')}）")
        if raise_business and isinstance(payload, dict) and payload.get("success") is False:
            self._raise_business_error(payload, op=op)
        return payload

    def _raise_business_error(self, payload: dict, *, op: str) -> None:
        data = payload.get("data") if isinstance(payload.get("data"), dict) else {}
        code = str(payload.get("code") or data.get("code") or "")
        details = str(payload.get("details") or data.get("details") or "")
        if code in ("Bad_Request",) or "Bad_Request" in details:
            raise InvalidParameterError(
                f"{op}: 上游判 Bad_Request —— 若确认请求体与抓包一致，先检查 `version: "
                f"{self.settings.version_header}` 头是否存在（缺该头必被判 Bad_Request）。"
                + (f" 上游原文：{details[:160]}" if details else ""))
        if code in ("Unauthorized",) or "Unauthorized" in details:
            raise AuthenticationError(f"{op}: 上游凭据失效（Unauthorized）")
        if code in ("Not_Found",) or "Task not found" in details or "CHAT_NOT_FOUND" in details:
            raise NotFoundError(f"{op}: 上游不存在（Not_Found）：{details[:160] or code}")
        if "额度" in details or "quota" in f"{code} {details}".lower():
            raise QuotaExhaustedError(f"{op}: 上游额度已用尽：{details[:160] or code}")
        raise UpstreamError(f"{op}: 上游拒绝：{code or '(无码)'} {details[:200]}")

    # ------------------------------------------------------------------ 端点

    def new_chat(self, token: str, *, extra_cookies: str = "") -> str:
        """建会话。返回 `data.id`（作 chat_id，可长期复用）。"""
        body = {
            "title": "New Chat",
            "models": [self.settings.chat_model],
            "chat_mode": "normal",
            "chat_type": "t2v",
            "timestamp": int(time.time() * 1000),
            "project_id": "",
        }
        resp = self._client.post(
            "/api/v2/chats/new", json=body,
            headers=self.headers(token, referer=f"{self.settings.base_url}/",
                                 extra_cookies=extra_cookies))
        payload = self._decode(resp, op="chats/new")
        chat_id = str(((payload.get("data") or {}).get("id")) or "").strip()
        if not chat_id:
            raise UpstreamError("chats/new 未返回 data.id")
        return chat_id

    def build_submit_body(self, chat_id: str, *, prompt: str, ratio: str, chat_type: str,
                          image_url: str | None, chat_model: str | None = None,
                          ts: int | None = None) -> dict:
        """提交体构造（纯函数；dry_run 也走这里，保证"预演即真发"）。"""
        from ... import media

        model = chat_model or self.settings.chat_model
        now = int(ts if ts is not None else time.time())
        message: dict = {
            "id": None,
            "fid": str(uuid.uuid4()),
            "parentId": None,
            "childrenIds": [str(uuid.uuid4())],
            "role": "user",
            "content": prompt,
            "user_action": "chat",
            "timestamp": now,
            "models": [model],
            "model": "",
            "chat_type": chat_type,
            "feature_config": dict(FEATURE_CONFIG),
            "extra": {"meta": {"subChatType": chat_type, "size": ratio}},
            "sub_chat_type": chat_type,
            "parent_id": None,
        }
        if image_url:
            message["files"] = [media.image_entry(image_url)]
        return {
            "stream": False,
            "version": "2.1",
            "incremental_output": True,
            "chatId": chat_id,
            "parentId": "",
            "chat_id": chat_id,
            "chat_mode": "normal",
            "model": model,
            "parent_id": None,
            "messages": [message],
            "timestamp": now,
            "size": ratio,
        }

    def submit_video(self, token: str, *, chat_id: str, prompt: str, ratio: str,
                     chat_type: str, image_url: str | None, extra_cookies: str = "",
                     chat_model: str | None = None) -> str:
        """提交生成；`stream:false` 时**同步**返回 task_id。"""
        body = self.build_submit_body(chat_id, prompt=prompt, ratio=ratio,
                                      chat_type=chat_type, image_url=image_url,
                                      chat_model=chat_model)
        try:
            resp = self._client.post(
                "/api/v2/chat/completions", params={"chat_id": chat_id}, json=body,
                headers=self.headers(token,
                                     referer=f"{self.settings.base_url}/c/{chat_id}",
                                     extra_cookies=extra_cookies))
        except httpx.TimeoutException as exc:
            raise UpstreamTimeoutError(f"提交超时：{exc}") from exc
        except httpx.HTTPError as exc:
            raise UpstreamError(f"提交传输失败：{type(exc).__name__}: {exc}") from exc
        payload = self._decode(resp, op="chat/completions")
        messages = ((payload.get("data") or {}).get("messages") or [])
        task_id = ""
        if messages and isinstance(messages[0], dict):
            wanx = (messages[0].get("extra") or {}).get("wanx") or {}
            task_id = str(wanx.get("task_id") or "").strip()
        if not task_id:
            raise UpstreamError(
                "提交响应里没有 task_id（期望路径 data.messages[0].extra.wanx.task_id）")
        return task_id

    def task_status(self, token: str, task_id: str, *, extra_cookies: str = "") -> dict:
        """查询任务。返回 `{actual_status_code, success, data}`（不做状态解释）。"""
        try:
            resp = self._client.get(
                f"/api/v2/task/status/{task_id}",
                headers=self.headers(token, referer=f"{self.settings.base_url}/",
                                     extra_cookies=extra_cookies))
        except httpx.TimeoutException as exc:
            raise UpstreamTimeoutError(f"查询超时：{exc}") from exc
        except httpx.HTTPError as exc:
            raise UpstreamError(f"查询传输失败：{type(exc).__name__}: {exc}") from exc
        raw_actual = resp.headers.get("x-actual-status-code")
        try:
            actual = int(raw_actual) if raw_actual else resp.status_code
        except ValueError:
            actual = resp.status_code
        payload = self._decode(resp, op="task/status", raise_business=False)
        data = payload.get("data") if isinstance(payload.get("data"), dict) else {}
        return {"actual_status_code": actual, "success": bool(payload.get("success")), "data": data}


__all__ = ["QwenClient", "FEATURE_CONFIG", "mask_email", "tz_header"]
