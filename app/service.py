"""业务编排 —— 创建 / 查询 / 轮询；账号归属与凭据绑定在这里收口。

职责边界：
  · `ark.py` 只做纯翻译（无 IO）；
  · `upstream/qwen/*` 只做上游交互（不写任务表）；
  · **本模块**决定"谁来跑、状态怎么落、错误怎么分类回报"。
"""
from __future__ import annotations

import logging
import random
import string
import time

from .ark import CreatePlan, ark_task_view, translate_ark_create
from .config import Settings
from .errors import (
    AdapterError,
    AuthenticationError,
    CredentialUnavailableError,
    NotFoundError,
    QuotaExhaustedError,
    RiskControlError,
    UpstreamError,
    UpstreamTimeoutError,
)
from .store import TERMINAL_STATUSES, TaskRecord, TaskStore
from .upstream.qwen.accounts import AccountPool, AccountState, mask_email
from .upstream.qwen.client import QwenClient

logger = logging.getLogger("qwen.service")

_LOCAL_ID_ALPHABET = string.ascii_lowercase + string.digits


class QwenVideoService:
    def __init__(self, settings: Settings, store: TaskStore, pool: AccountPool,
                 client: QwenClient) -> None:
        self.settings = settings
        self.store = store
        self.pool = pool
        self.client = client

    # ------------------------------------------------------------------ 创建

    def create(self, body: dict, credential_id: str, *, dry_run: bool = False) -> dict:
        """方舟创建请求 → `{"id": "cgt-…"}`（dry_run 时返回"将要发出的请求"）。"""
        plan = translate_ark_create(body)
        if dry_run:
            return self._dry_run_view(plan)
        account = self.pool.acquire_with_wait()
        return self._create_on_account(plan, account, credential_id)

    def _dry_run_view(self, plan: CreatePlan) -> dict:
        """跑完完整翻译、直接返回将要发出的 payload —— **不提交、不占号、不落库**。"""
        headers = self.client.headers("<token>", referer=f"{self.settings.base_url}/c/<chat_id>")
        headers["Cookie"] = "token=<redacted>"
        return {
            "dry_run": True,
            "upstream": {
                "method": "POST",
                "url": f"{self.settings.base_url}/api/v2/chat/completions?chat_id=<chat_id>",
                "headers": headers,
                "body": self.client.build_submit_body(
                    "<chat_id>", prompt=plan.prompt, ratio=plan.ratio,
                    chat_type=plan.chat_type, image_url=plan.image_url),
            },
            "degradations": plan.degradations,
        }

    def _extra_cookies(self, email: str) -> str:
        return (self.settings.account_cookies or {}).get(email, "")

    def _create_on_account(self, plan: CreatePlan, account: AccountState,
                           credential_id: str) -> dict:
        email = account.email
        extra = self._extra_cookies(email)
        token = self.pool.token_for(email)
        chat_id = self._chat_id_for(account, token, extra)
        try:
            task_id = self._submit(token, chat_id, plan, extra)
        except NotFoundError:
            # 会话可能被上游回收（"CHAT_NOT_FOUND"）—— 重建一次，只重试这一种
            logger.warning("chat_id 失效，重建后重试一次（%s）", mask_email(email))
            self.store.kv_delete(f"chat:{email}")
            chat_id = self._chat_id_for(account, token, extra)
            try:
                task_id = self._submit(token, chat_id, plan, extra)
            except AdapterError as exc:
                self._report_submit_failure(email, exc)
                raise
        except AdapterError as exc:
            self._report_submit_failure(email, exc)
            raise

        self.pool.report_submitted(email)
        now = int(time.time())
        record = TaskRecord(
            local_id=self._new_local_id(now),
            upstream_task_id=task_id,
            chat_id=chat_id,
            account=email,
            credential_id=credential_id,
            model_requested=plan.model_requested,
            prompt=plan.prompt,
            ratio=plan.ratio,
            image_url=plan.image_url or "",
            status="running",
        )
        record.set_degradations(plan.degradations)
        self.store.put(record)
        return {"id": record.local_id}

    def _submit(self, token: str, chat_id: str, plan: CreatePlan, extra: str) -> str:
        return self.client.submit_video(
            token, chat_id=chat_id, prompt=plan.prompt, ratio=plan.ratio,
            chat_type=plan.chat_type, image_url=plan.image_url, extra_cookies=extra)

    def _report_submit_failure(self, email: str, exc: AdapterError) -> None:
        if isinstance(exc, RiskControlError):
            self.pool.report_failure(email, "risk")
        elif isinstance(exc, AuthenticationError):
            self.pool.report_failure(email, "auth")
        elif isinstance(exc, QuotaExhaustedError):
            self.pool.report_failure(email, "quota")
        elif isinstance(exc, (UpstreamError, UpstreamTimeoutError)):
            self.pool.report_failure(email, "transport")
        else:
            self.pool.report_failure(email, "refused")

    def _chat_id_for(self, account: AccountState, token: str, extra: str) -> str:
        key = f"chat:{account.email}"
        cached = self.store.kv_get(key)
        if cached:
            return cached
        chat_id = self.client.new_chat(token, extra_cookies=extra)
        self.store.kv_set(key, chat_id)
        return chat_id

    @staticmethod
    def _new_local_id(now: int) -> str:
        stamp = time.strftime("%Y%m%d%H%M%S", time.gmtime(now))
        tail = "".join(random.choices(_LOCAL_ID_ALPHABET, k=5))
        return f"cgt-{stamp}-{tail}"

    # ------------------------------------------------------------------ 查询

    def get(self, local_id: str, credential_id: str) -> dict:
        record = self.store.get(local_id)
        if record is None or record.credential_id != credential_id:
            # 归属不符 ⇒ 本地直接 404，**根本不发上游请求**（ADR-003 口径）
            raise NotFoundError(f"任务 {local_id} 不存在")
        if record.status not in TERMINAL_STATUSES:
            record = self.poll_record(record)
        return ark_task_view(record)

    def poll_record(self, record: TaskRecord) -> TaskRecord:
        """回查上游一次并落库。**不改写非终态之外的语义**：解析不出来按失败报。"""
        now = int(time.time())
        if now - record.created_at > self.settings.task_timeout:
            record.status = "expired"
            return self.store.put(record)
        account = self.pool.get_state(record.account)
        if account is None:
            record.status = "failed"
            record.error_code = "internal"
            record.error_message = "任务所属账号不在当前配置中"
            return self.store.put(record)

        token = self.pool.token_for(account.email)
        extra = self._extra_cookies(account.email)
        result = self.client.task_status(token, record.upstream_task_id, extra_cookies=extra)
        actual = int(result["actual_status_code"])
        data: dict = result["data"] or {}

        if actual == 401 or data.get("code") == "Unauthorized":
            self.pool.invalidate_token(account.email)
            self.pool.report_failure(account.email, "auth")
            raise CredentialUnavailableError("上游 401 —— 账号 token 已失效（已标记重铸后重试）")

        if actual == 404 or data.get("code") == "Not_Found":
            record.status = "failed"
            record.error_code = "NotFound"
            record.error_message = "上游任务不存在（Not_Found）"
        elif result["success"]:
            upstream_status = str(data.get("task_status") or "")
            if upstream_status == "success":
                url = str(data.get("content") or "").strip()
                if url:
                    record.status = "succeeded"
                    record.video_url = url
                else:
                    record.status = "failed"
                    record.error_code = "internal"
                    record.error_message = "上游报成功但未返回产物地址（零产物按失败，不报成功）"
            elif upstream_status == "running":
                record.status = "running"
            else:
                record.status = "failed"
                record.error_code = "internal"
                record.error_message = f"上游任务失败：{self._failure_detail(data) or '未给出原因'}"
        else:
            record.status = "failed"
            record.error_code = "internal"
            record.error_message = f"上游查询被拒：{self._failure_detail(data) or '未给出原因'}"

        if record.status in TERMINAL_STATUSES:
            self.pool.report_finished(account.email)
        return self.store.put(record)

    @staticmethod
    def _failure_detail(data: dict) -> str:
        detail = data.get("detail") if isinstance(data.get("detail"), dict) else {}
        return str(data.get("message") or detail.get("info")
                   or data.get("details") or "").strip()

    # ------------------------------------------------------------------ 协调器

    def poll_active_once(self) -> int:
        """给后台协调器用：把所有非终态任务各回查一次（失败吞掉并记日志）。"""
        polled = 0
        for record in self.store.list_active():
            try:
                self.poll_record(record)
                polled += 1
            except AdapterError as exc:
                logger.warning("协调器回查 %s 失败（保持原状态）：%s", record.local_id, exc)
        return polled


__all__ = ["QwenVideoService"]
