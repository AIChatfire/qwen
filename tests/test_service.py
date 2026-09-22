"""引擎级：经 FastAPI（ASGI）+ 假上游，断言「上游实际收到了什么」。零网络、零真实生成。"""
from __future__ import annotations

import dataclasses
import json

from app.main import create_app
from app.store import TaskStore
from app.upstream.qwen.accounts import AccountPool
from app.upstream.qwen.client import QwenClient
from tests.conftest import AUTH_A, AUTH_B, CDN_IMAGE, TASKS_PATH

I2V_BODY = {
    "model": "qwen/video",
    "content": [
        {"type": "text", "text": "一位潜水员探索沉船"},
        {"type": "image_url", "image_url": {"url": CDN_IMAGE}, "role": "first_frame"},
    ],
    "ratio": "16:9",
}
T2V_BODY = {
    "model": "qwen/video",
    "content": [{"type": "text", "text": "一只猫"}],
    "ratio": "16:9",
}


def post(client_app, body=None, headers=None):
    tc, *_ = client_app
    return tc.post(TASKS_PATH, json=body or I2V_BODY, headers=headers or AUTH_A)


def test_create_returns_only_id_and_upstream_receives_fully_marked_i2v(client_app):
    tc, fake, store, settings = client_app
    resp = post(client_app)
    assert resp.status_code == 200
    assert list(resp.json().keys()) == ["id"]          # 创建响应只回 id（方舟原生）
    assert resp.json()["id"].startswith("cgt-")
    assert store.count() == 1

    assert fake.calls("/api/v2/chats/new"), "必须先建会话拿 chat_id"
    comp = fake.calls("/api/v2/chat/completions")
    assert len(comp) == 1
    sent = json.loads(comp[0].content)
    message = sent["messages"][0]
    # i2v 三处同标 + size 两处同值
    assert message["chat_type"] == message["sub_chat_type"] == "i2v"
    assert message["extra"]["meta"]["subChatType"] == "i2v"
    assert sent["size"] == message["extra"]["meta"]["size"] == "16:9"
    assert message["files"][0]["url"] == CDN_IMAGE
    assert message["files"][0]["file_class"] == "vision"
    assert sent["stream"] is False
    # 头与凭据
    assert comp[0].headers["version"] == "0.2.0"
    assert comp[0].headers["source"] == "web"
    assert comp[0].headers["cookie"] == "token=tok-a@x.cn"
    assert comp[0].url.params["chat_id"].startswith("chat-")


def test_t2v_marks_three_places_and_sends_no_files(client_app):
    tc, fake, store, settings = client_app
    assert post(client_app, T2V_BODY).status_code == 200
    sent = fake.bodies("/api/v2/chat/completions")[0]
    message = sent["messages"][0]
    assert message["chat_type"] == message["sub_chat_type"] == "t2v"
    assert message["extra"]["meta"]["subChatType"] == "t2v"
    assert "files" not in message


def test_get_polls_until_success_then_stops_touching_upstream(client_app):
    tc, fake, store, settings = client_app
    task_id = post(client_app).json()["id"]
    first = tc.get(f"{TASKS_PATH}/{task_id}", headers=AUTH_A).json()
    assert first["status"] == "running"
    second = tc.get(f"{TASKS_PATH}/{task_id}", headers=AUTH_A).json()
    assert second["status"] == "succeeded"
    assert second["content"]["video_url"].endswith(".mp4?key=k")
    assert second["duration"] == 5
    polls = len(fake.calls("/api/v2/task/status/"))
    tc.get(f"{TASKS_PATH}/{task_id}", headers=AUTH_A)
    assert len(fake.calls("/api/v2/task/status/")) == polls   # 终态不再回查


def test_other_key_gets_404_with_zero_upstream_calls(client_app):
    tc, fake, store, settings = client_app
    task_id = post(client_app).json()["id"]
    before = len(fake.requests)
    resp = tc.get(f"{TASKS_PATH}/{task_id}", headers=AUTH_B)
    assert resp.status_code == 404
    assert len(fake.requests) == before        # 本地拦下：根本没问上游
    assert resp.json()["error"]["code"] == "InvalidEndpointOrModel.NotFound"


def test_unknown_key_is_401(client_app):
    tc, *_ = client_app
    resp = tc.get(f"{TASKS_PATH}/cgt-x", headers={"Authorization": "Bearer sk-nope"})
    assert resp.status_code == 401


def test_dry_run_returns_payload_with_zero_upstream_and_zero_records(client_app):
    tc, fake, store, settings = client_app
    resp = tc.post(TASKS_PATH, json=I2V_BODY, headers={**AUTH_A, "X-Avm-Dry-Run": "1"})
    assert resp.status_code == 200
    payload = resp.json()
    assert payload["dry_run"] is True
    assert payload["upstream"]["headers"]["Cookie"] == "token=<redacted>"
    assert payload["upstream"]["body"]["messages"][0]["chat_type"] == "i2v"
    assert fake.requests == []
    assert store.count() == 0


def test_ratio_outside_enum_recorded_as_degradation(client_app):
    tc, fake, store, settings = client_app
    body = {"model": "qwen/video", "content": [{"type": "text", "text": "x"}],
            "ratio": "adaptive"}
    task_id = post(client_app, body).json()["id"]
    sent = fake.bodies("/api/v2/chat/completions")[0]
    assert sent["size"] == "1:1"
    view = tc.get(f"{TASKS_PATH}/{task_id}", headers=AUTH_A).json()
    assert view["ratio"] == "1:1"
    assert any("1:1" in item for item in view["degradations"])


def test_two_images_rejected_with_400_and_zero_upstream(client_app):
    tc, fake, store, settings = client_app
    body = {"model": "qwen/video", "content": [
        {"type": "text", "text": "x"},
        {"type": "image_url", "image_url": {"url": CDN_IMAGE}, "role": "first_frame"},
        {"type": "image_url", "image_url": {"url": CDN_IMAGE}, "role": "last_frame"}]}
    resp = post(client_app, body)
    assert resp.status_code == 400
    assert fake.requests == []


def test_duration_eight_clamped_and_reported_without_leaking_upstream(client_app):
    tc, fake, store, settings = client_app
    task_id = post(client_app, {**T2V_BODY, "duration": 8}).json()["id"]
    sent = fake.bodies("/api/v2/chat/completions")[0]
    assert "duration" not in sent      # 上游没有该参数，绝不透传
    view = tc.get(f"{TASKS_PATH}/{task_id}", headers=AUTH_A).json()
    assert any("吸附" in item for item in view["degradations"])


def test_pool_exhausted_is_429_with_retry_after_and_zero_upstream(settings, fake_upstream):
    """**严格模式**（SUBMIT_QUEUE_ENABLED=0）：上限闸门必须生效在「发出请求之前」——用 0 额度假设置接死。"""
    from fastapi.testclient import TestClient

    over = dataclasses.replace(settings, daily_video_cap=0, submit_queue_enabled=False)
    store = TaskStore(over.task_db)
    pool = AccountPool(over, mint=lambda account: "tok")
    client = QwenClient(over, transport=fake_upstream.transport())
    app = create_app(over, store=store, pool=pool, client=client)
    with TestClient(app) as tc:
        resp = tc.post(TASKS_PATH, json=T2V_BODY, headers=AUTH_A)
    assert resp.status_code == 429
    assert "Retry-After" in resp.headers
    assert fake_upstream.requests == []


def test_terminal_task_survives_restart_without_refetching(settings, fake_upstream):
    """跨实例可读：新 store / 新 app 读同一份库；终态不再打扰上游。"""
    from fastapi.testclient import TestClient

    store1 = TaskStore(settings.task_db)
    app1 = create_app(settings, store=store1,
                      pool=AccountPool(settings, mint=lambda a: "tok"),
                      client=QwenClient(settings, transport=fake_upstream.transport()))
    with TestClient(app1) as tc:
        task_id = tc.post(TASKS_PATH, json=T2V_BODY, headers=AUTH_A).json()["id"]
        for _ in range(3):   # 1 次 running → 1 次 succeeded → 1 次纯读
            tc.get(f"{TASKS_PATH}/{task_id}", headers=AUTH_A)

    store2 = TaskStore(settings.task_db)          # 进程重启语义
    app2 = create_app(settings, store=store2,
                      pool=AccountPool(settings, mint=lambda a: "tok"),
                      client=QwenClient(settings, transport=fake_upstream.transport()))
    before = len(fake_upstream.requests)
    with TestClient(app2) as tc2:
        view = tc2.get(f"{TASKS_PATH}/{task_id}", headers=AUTH_A).json()
    assert view["status"] == "succeeded"
    assert len(fake_upstream.requests) == before
