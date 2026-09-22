"""token 过期自动续期 —— **主动**（按 JWT 的 `exp`，带保守上限）与
**被动**（上游判 401 ⇒ 立即重铸 ⇒ **原请求重试一次**）两条路。全程零网络、零真实生成。

为什么被动那条必须存在：`exp` 是上游**自称**的（实测 30 天），服务端完全可能提前失效
（自称 30 天、实际 7 天就 401 是有先例的形态）⇒ 主动续期只能"别太频繁换"，"换得太晚"必须由
401 当场重铸兜住。判据也成立：401 = 上游**未受理**，重试不会重复计费。
"""
from __future__ import annotations

import dataclasses

import pytest
from fastapi.testclient import TestClient

from app.errors import CredentialUnavailableError
from app.main import create_app
from app.store import TaskStore
from app.upstream.qwen.accounts import AccountPool
from app.upstream.qwen.client import QwenClient
from tests.conftest import AUTH_A, TASKS_PATH, UNAUTHORIZED_BODY

T2V_BODY = {"model": "qwen/video", "content": [{"type": "text", "text": "一只猫"}],
            "ratio": "16:9"}
CRED = "hmac-sha256:test"


# ------------------------------------------------------------------ 写端点（提交）


def test_submit_401_remints_and_retries_in_place(piped):
    """写端点判 401 ⇒ 重铸 + **当场重试**（不经队列、不重复落库、不冷却账号）。"""
    service, store, pool, client, fake = piped
    fake.fail_submits.append(UNAUTHORIZED_BODY)

    result = service.create(T2V_BODY, CRED)

    record = store.get(result["id"])
    assert record.status == "running", "重试后应已提交成功（而不是落 queued 等下一轮）"
    assert store.count_queued() == 0
    assert store.count() == 1, "重试不得产生第二条任务记录"
    assert len(fake.calls("/api/v2/chat/completions")) == 2, "恰好两次：原请求 + 重试一次"
    state = pool.get_state("a@x.cn")
    assert state.mints == 2, "第二次用的是新铸的 token"
    assert state.cooldown_until == 0.0, "自愈成功 ⇒ 不该冷却账号"


def test_persistent_401_is_reported_and_account_cooled(piped):
    """重铸后仍 401 ⇒ 不是"token 过期"而是凭据/账号问题：**只试两次**、冷却、照实回报。"""
    service, store, pool, client, fake = piped
    fake.fail_submits.extend([UNAUTHORIZED_BODY, UNAUTHORIZED_BODY])

    service.create(T2V_BODY, CRED)          # 队列开启 ⇒ AuthenticationError 可入队

    assert len(fake.calls("/api/v2/chat/completions")) == 2, "不得无限重试"
    state = pool.get_state("a@x.cn")
    assert state.cooldown_reason == "auth"
    assert state.cooldown_until > 0
    assert state.mints == 2
    assert store.count_queued() == 1, "缺省口径下这类失败可排队（可证明未受理）"


def test_persistent_401_in_strict_mode_surfaces_401(settings, fake_upstream):
    """严格模式（关队列）：把 401 照实抛给调用方（而不是 502/503 之类的误报）。"""
    over = dataclasses.replace(settings, submit_queue_enabled=False)
    store = TaskStore(over.task_db)
    pool = AccountPool(over, mint=lambda account: f"tok-{account.email}")
    client = QwenClient(over, transport=fake_upstream.transport())
    app = create_app(over, store=store, pool=pool, client=client)

    fake_upstream.fail_submits.extend([UNAUTHORIZED_BODY, UNAUTHORIZED_BODY])
    with TestClient(app) as tc:
        resp = tc.post(TASKS_PATH, json=T2V_BODY, headers=AUTH_A)
    assert resp.status_code == 401
    assert resp.json()["error"]["code"] == "AuthenticationError"
    assert store.count() == 0


# ------------------------------------------------------------------ 查询（轮询）


def test_query_401_remints_and_returns_real_state(piped):
    """查询被判 401 ⇒ 重铸后重试一次 ⇒ **同一轮 GET 就能拿到真实状态**（不再白丢一轮）。"""
    service, store, pool, client, fake = piped
    task_id = service.create(T2V_BODY, CRED)["id"]
    upstream_id = store.get(task_id).upstream_task_id
    fake.status_scripts[upstream_id] = [
        ({"code": "Unauthorized", "details": "您没有权限访问此资源"}, 401),
        ({"task_status": "success",
          "content": "https://cdn.qwenlm.ai/output/u/t2v/c/x.mp4?key=k"}, 200),
    ]

    view = service.get(task_id, CRED)

    assert view["status"] == "succeeded"
    assert view["content"]["video_url"].endswith(".mp4?key=k")
    state = pool.get_state("a@x.cn")
    assert state.mints == 2
    assert state.cooldown_until == 0.0


def test_query_persistent_401_is_credential_unavailable(piped):
    """两次都 401 ⇒ 503 语义（部署问题），**不是** 401 —— 否则调用方会去改自己的 Key。"""
    service, store, pool, client, fake = piped
    task_id = service.create(T2V_BODY, CRED)["id"]
    upstream_id = store.get(task_id).upstream_task_id
    fake.status_scripts[upstream_id] = [({"code": "Unauthorized", "details": "无权限"}, 401)]

    with pytest.raises(CredentialUnavailableError):
        service.get(task_id, CRED)

    state = pool.get_state("a@x.cn")
    assert state.cooldown_reason == "auth"
    assert state.mints == 2
