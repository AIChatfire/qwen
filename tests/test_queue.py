"""轻量队列 / 重试 / 重启耐久 —— 经 ASGI + 假上游，零网络、零真实生成。

本文件直接对应用户 2026-09-22 的追问"这个服务可以自己做排队重试 并且重启不丢任务吗"，
逐条把它变成可执行的断言：
  · **自己排队**   —— 容量不足 ⇒ `queued`（不再硬 429），由协调器 / 后续 GET 出队提交；
  · **自己重试**   —— **只**重试可证明未提交的失败；含义不明的失败绝不自动重试（防重复计费）；
  · **重启不丢**   —— queued 任务与账号额度计数都在库里，新进程接着推进；
  · **不会无限囤** —— 队列深度超限 ⇒ 照旧 429 + Retry-After（背压保留）。
"""
from __future__ import annotations

import dataclasses
import time

from fastapi.testclient import TestClient

from app.main import create_app
from app.store import TaskStore
from app.upstream.qwen.accounts import AccountPool
from app.upstream.qwen.client import QwenClient
from tests.conftest import AUTH_A, RISK_BODY, TASKS_PATH

T2V_BODY = {"model": "qwen/video", "content": [{"type": "text", "text": "一只猫"}],
            "ratio": "16:9"}


def build(settings, fake, **overrides):
    """按覆盖后的 Settings 起一个独立 app（`task_db` 不变 ⇒ 多次调用共享同一个库）。

    返回 `(app, store, pool)`：`store/pool` 供用例直接检查落库状态与账号状态。
    """
    over = dataclasses.replace(settings, **overrides)
    store = TaskStore(over.task_db)
    pool = AccountPool(over, mint=lambda account: f"tok-{account.email}")
    client = QwenClient(over, transport=fake.transport())
    return create_app(over, store=store, pool=pool, client=client), store, pool


def _clear_backoff(store: TaskStore, *task_ids: str) -> None:
    """把退避窗拨到过去 —— 等价于"已经等够了重试间隔"（避免用例真 sleep）。"""
    for task_id in task_ids:
        record = store.get(task_id)
        assert record is not None
        record.next_attempt_at = 0
        store.put(record)


def _drain_to(tc: TestClient, task_id: str, status: str, *, tries: int = 4) -> dict:
    """连续 GET 直到期望状态（每次 GET 只推进一格：出队提交 / 回查一次）。"""
    view: dict = {}
    for _ in range(tries):
        view = tc.get(f"{TASKS_PATH}/{task_id}", headers=AUTH_A).json()
        if view["status"] == status:
            return view
    raise AssertionError(f"任务 {task_id} 未在 {tries} 次 GET 内到达 {status}：{view}")



def _exhaust_capacity(tc, fake, settings, *, posts: int = 2) -> None:
    """把 `daily_video_cap` 个窗口用完（每个账号各一次）——前置条件，非断言对象。"""
    for _ in range(posts):
        assert tc.post(TASKS_PATH, json=T2V_BODY, headers=AUTH_A).status_code == 200
    assert len(fake.calls("/api/v2/chat/completions")) == posts


# ------------------------------------------------------------------ 排队 + 出队


def test_capacity_exhausted_enqueues_then_drains_after_recovery(settings, fake_upstream):
    """容量不足 ⇒ 200 + `queued`（不再硬 429）；额度恢复后 GET 出队提交。"""
    app, store, pool = build(settings, fake_upstream,
                             daily_video_cap=1, account_wait_timeout=0.0)
    with TestClient(app) as tc:
        _exhaust_capacity(tc, fake_upstream, settings)

        third = tc.post(TASKS_PATH, json=T2V_BODY, headers=AUTH_A)
        assert third.status_code == 200, "容量不足由队列接住，不再回 429"
        queued_id = third.json()["id"]
        assert store.count_queued() == 1
        assert len(fake_upstream.calls("/api/v2/chat/completions")) == 2, "排队 ≠ 提前提交"

        record = store.get(queued_id)
        assert record.status == "queued"
        assert record.upstream_task_id == "" and record.account == "", \
            "排队中的记录必须是「未提交」的干净形态（无上游 id、无账号占位）"

        # 退避窗内：状态就是 queued，且零上游打扰
        assert tc.get(f"{TASKS_PATH}/{queued_id}",
                      headers=AUTH_A).json()["status"] == "queued"
        assert len(fake_upstream.calls("/api/v2/chat/completions")) == 2

        # 额度恢复（等价 UTC 日滚动：只清计数）
        for email in ("a@x.cn", "b@x.cn"):
            pool.get_state(email).day_used = 0
        _clear_backoff(store, queued_id)

        assert tc.get(f"{TASKS_PATH}/{queued_id}",
                      headers=AUTH_A).json()["status"] == "running"
        assert len(fake_upstream.calls("/api/v2/chat/completions")) == 3, "出队时才真提交"
        assert store.get(queued_id).upstream_task_id.startswith("tid-")
        view = _drain_to(tc, queued_id, "succeeded")   # 回查一次 running、再一次 succeeded
        assert view["content"]["video_url"].endswith(".mp4?key=k")


def test_coordinator_drains_queue_without_any_get(settings, fake_upstream):
    """没人查询也能推进：协调器把 `queued` 出队并一路推到 succeeded（生产默认开启）。"""
    app, store, pool = build(settings, fake_upstream, daily_video_cap=1,
                             account_wait_timeout=0.0,
                             coordinator_enabled=True, coordinator_tick=0.05)
    with TestClient(app) as tc:
        _exhaust_capacity(tc, fake_upstream, settings)
        queued_id = tc.post(TASKS_PATH, json=T2V_BODY, headers=AUTH_A).json()["id"]
        assert store.get(queued_id).status == "queued"

        for email in ("a@x.cn", "b@x.cn"):
            pool.get_state(email).day_used = 0
        _clear_backoff(store, queued_id)          # 退避窗拨过去，交给协调器

        deadline = time.time() + 5.0
        while time.time() < deadline and store.get(queued_id).status != "succeeded":
            time.sleep(0.05)
        assert store.get(queued_id).status == "succeeded"


# ------------------------------------------------------------------ 重试语义


def test_provably_unsubmitted_failure_is_requeued_then_succeeds_elsewhere(
        settings, fake_upstream):
    """风控（RGV587）⇒ 可证明未提交 ⇒ 排队重试；出队时自动落到未冷却的账号。"""
    app, store, pool = build(settings, fake_upstream)
    fake_upstream.fail_submits.append(RISK_BODY)
    with TestClient(app) as tc:
        resp = tc.post(TASKS_PATH, json=T2V_BODY, headers=AUTH_A)
        assert resp.status_code == 200, "风控不再直通 429 —— 队列接住并稍后换号重试"
        task_id = resp.json()["id"]

        record = store.get(task_id)
        assert record.status == "queued" and record.attempts == 1
        assert store.count_queued() == 1
        assert pool.get_state("a@x.cn").cooldown_reason == "risk", "失败账号要被冷却"

        view = tc.get(f"{TASKS_PATH}/{task_id}", headers=AUTH_A).json()
        assert view["status"] == "running"
        comp = fake_upstream.calls("/api/v2/chat/completions")
        assert len(comp) == 2, "重试只发一次（不连打）"
        assert comp[1].headers["cookie"] == "token=tok-b@x.cn", "重试换到未冷却的账号"
        done = _drain_to(tc, task_id, "succeeded")
        assert done["content"]["video_url"].endswith(".mp4?key=k")


def test_ambiguous_failure_on_create_is_reported_not_queued(settings, fake_upstream):
    """含义不明的失败（未知业务码 / 上游 5xx）**绝不自动重试** —— 建任务是计费动作。"""
    app, store, pool = build(settings, fake_upstream)
    with TestClient(app) as tc:
        fake_upstream.submit_response = {"success": False, "code": "Weird_Code",
                                         "details": "boom"}
        first = tc.post(TASKS_PATH, json=T2V_BODY, headers=AUTH_A)
        assert first.status_code == 502, "照实回报，不装作「已排队」"
        assert store.count_queued() == 0
        assert store.count() == 0, "也没落一条假任务（排队必须留下可查询的真记录）"

        fake_upstream.submit_status_code = 500
        second = tc.post(TASKS_PATH, json=T2V_BODY, headers=AUTH_A)
        assert second.status_code == 502
        assert store.count_queued() == 0


def test_dequeue_with_ambiguous_failure_marks_failed_and_stops(settings, fake_upstream):
    """出队提交遇到含义不明的失败 ⇒ 落 `failed` 并停止 —— 队列不得变成无限重试环。"""
    app, store, pool = build(settings, fake_upstream,
                             daily_video_cap=1, account_wait_timeout=0.0)
    with TestClient(app) as tc:
        _exhaust_capacity(tc, fake_upstream, settings)
        queued_id = tc.post(TASKS_PATH, json=T2V_BODY, headers=AUTH_A).json()["id"]

        for email in ("a@x.cn", "b@x.cn"):
            pool.get_state(email).day_used = 0
        _clear_backoff(store, queued_id)
        fake_upstream.submit_response = {"success": False, "code": "Weird_Code",
                                        "details": "boom"}

        for _ in range(3):     # 反复 GET 也不许再试
            view = tc.get(f"{TASKS_PATH}/{queued_id}", headers=AUTH_A).json()
            assert view["status"] == "failed"
        assert "不可自动重试" in view["error"]["message"]
        assert store.get(queued_id).attempts == 0, "attempts 只记可重试的那类失败"
        assert store.count_queued() == 0
        assert len(fake_upstream.calls("/api/v2/chat/completions")) == 3, "只发了一次出队尝试"


# ------------------------------------------------------------------ 背压与耐久


def test_queue_depth_is_bounded_and_returns_429_backpressure(settings, fake_upstream):
    """背压保留：排队深度超限 ⇒ 429 + Retry-After，且一条记录都不落。"""
    app, store, pool = build(settings, fake_upstream, daily_video_cap=0,
                             queue_max_depth=1)
    with TestClient(app) as tc:
        assert tc.post(TASKS_PATH, json=T2V_BODY, headers=AUTH_A).status_code == 200
        assert store.count_queued() == 1

        resp = tc.post(TASKS_PATH, json=T2V_BODY, headers=AUTH_A)
        assert resp.status_code == 429
        assert "Retry-After" in resp.headers
        assert resp.json()["error"]["code"] == "RateLimitExceeded"
        assert store.count() == 1, "被背压拒绝的请求不落库"
        assert fake_upstream.requests == [], "背压要在触上游之前生效"


def test_queued_work_and_pool_state_survive_restart(settings, fake_upstream):
    """重启不丢：queued 任务 + 账号额度计数都在库里，新进程继续把它推到成功。"""
    app1, store1, pool1 = build(settings, fake_upstream,
                                daily_video_cap=1, account_wait_timeout=0.0)
    with TestClient(app1) as tc:
        _exhaust_capacity(tc, fake_upstream, settings)
        queued_id = tc.post(TASKS_PATH, json=T2V_BODY, headers=AUTH_A).json()["id"]
        assert store1.count() == 3 and store1.count_queued() == 1

    # ---- 进程重启：新 store / 新池 / 新 app，共用同一个库（容量恢复：cap=3）
    app2, store2, pool2 = build(settings, fake_upstream)
    assert pool2.get_state("a@x.cn").day_used == 1, "额度计数从 KV 恢复（不是从 0 重来）"
    assert pool2.get_state("b@x.cn").day_used == 1

    _clear_backoff(store2, queued_id)   # 等价于"重启前已过完的退避秒"
    with TestClient(app2) as tc2:
        assert tc2.get(f"{TASKS_PATH}/{queued_id}",
                       headers=AUTH_A).json()["status"] == "running"
        assert _drain_to(tc2, queued_id, "succeeded")["content"]["video_url"]
    assert store2.count_queued() == 0
    assert pool2.get_state("a@x.cn").day_used == 2   # 重启后的这次提交照常计额度
