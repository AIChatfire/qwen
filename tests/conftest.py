"""共享夹具：假上游（记录每个请求）+ 可注入的 Settings / Store / Pool / Client。

零网络纪律：全部用例走 `httpx.MockTransport`，不发任何真实请求（含 DNS）。
测试确定性：夹具里 **协调器关闭**（生产默认开启）—— 否则后台线程会与用例断言抢推进。
"""
from __future__ import annotations

import json

import httpx
import pytest

from app.config import Settings
from app.service import QwenVideoService
from app.store import TaskStore
from app.upstream.qwen.accounts import AccountPool
from app.upstream.qwen.client import QwenClient

CDN_IMAGE = "https://cdn.qwenlm.ai/output/u/image_gen/m1/1.png"
SUCCESS_URL = "https://cdn.qwenlm.ai/output/u/i2v/c/{task}.mp4?key=k"

#: 风控信封（HTTP 200 + ret 数组）——服务端的"可证明未提交"失败样本
RISK_BODY = {"ret": ["FAIL_SYS_USER_VALIDATE", "RGV587_ERROR::SM::哎哟喂,被挤爆啦,请稍后重试"],
             "data": {"url": "https://chat.qwen.ai/_____tmd_____/punish"}}


class FakeQwen:
    """内存假上游：按路径分流，记录**每一个**收到的请求（供"上游实际收到什么"断言）。"""

    def __init__(self) -> None:
        self.requests: list[httpx.Request] = []
        self.chat_seq = 0
        self.task_seq = 0
        #: task_id -> 待回放的 [(data, actual_status_code)]；只剩一条时重复回放
        self.status_scripts: dict[str, list[tuple[dict, int]]] = {}
        #: 非 None 时，提交端点固定回这个 body（用于错误映射用例）
        self.submit_response: dict | None = None
        self.submit_status_code = 200
        #: 提交端点的一次性失败队列（先到先消费；空了才走正常成功路径）
        self.fail_submits: list[dict] = []

    # ------------------------------------------------------------ 断言工具

    def calls(self, path_prefix: str) -> list[httpx.Request]:
        return [r for r in self.requests if r.url.path.startswith(path_prefix)]

    def bodies(self, path_prefix: str) -> list[dict]:
        return [json.loads(r.content) for r in self.calls(path_prefix)]

    def cookies(self, path_prefix: str) -> list[str]:
        return [r.headers.get("cookie", "") for r in self.calls(path_prefix)]

    # ------------------------------------------------------------ 假上游实现

    def handler(self, request: httpx.Request) -> httpx.Response:
        self.requests.append(request)
        path = request.url.path
        if path == "/api/v2/chats/new":
            self.chat_seq += 1
            return httpx.Response(
                200, json={"success": True, "data": {"id": f"chat-{self.chat_seq}"}})
        if path == "/api/v2/chat/completions":
            if self.fail_submits:
                return httpx.Response(200, json=self.fail_submits.pop(0))
            if self.submit_response is not None:
                return httpx.Response(self.submit_status_code, json=self.submit_response)
            self.task_seq += 1
            task_id = f"tid-{self.task_seq}"
            self.status_scripts.setdefault(task_id, [
                ({"task_status": "running"}, 200),
                ({"task_status": "success",
                  "content": SUCCESS_URL.format(task=task_id)}, 200),
            ])
            return httpx.Response(200, json={
                "success": True, "request_id": "r",
                "data": {"chat_id": "chat", "chat_type": "t2v",
                         "messages": [{"role": "assistant", "content": "",
                                       "extra": {"wanx": {"task_id": task_id}}}]},
            })
        if path.startswith("/api/v2/task/status/"):
            task_id = path.rsplit("/", 1)[-1]
            script = self.status_scripts.get(task_id)
            if script is None:
                return httpx.Response(200, headers={"x-actual-status-code": "404"},
                                      json={"success": False,
                                            "data": {"code": "Not_Found", "details": "Task not found"}})
            data, actual = script[0] if len(script) == 1 else script.pop(0)
            return httpx.Response(200, headers={"x-actual-status-code": str(actual)},
                                  json={"success": actual == 200, "data": data})
        raise AssertionError(f"unexpected upstream path: {path}")

    def transport(self) -> httpx.MockTransport:
        return httpx.MockTransport(self.handler)


@pytest.fixture
def fake_upstream() -> FakeQwen:
    return FakeQwen()


@pytest.fixture
def settings(tmp_path) -> Settings:
    return Settings(
        accounts={"a@x.cn": "pw-a", "b@x.cn": "pw-b"},
        task_db=f"sqlite:///{tmp_path}/qwen.db",
        data_dir=str(tmp_path),
        api_keys=["sk-a", "sk-b"],
        key_secret="secret-for-tests",
        signin_socks="socks5h://127.0.0.1:9",  # 不会被真的使用（mint 已注入）
        signin_min_interval=0.0,
        signin_wait_timeout=1.0,
        submit_min_interval=0.0,
        daily_video_cap=3,
        account_wait_timeout=0.05,
        coordinator_enabled=False,   # 测试确定性：不让后台线程抢推进
        queue_retry_base=0.0,        # 排队重试不等待（用例内即时出队）
    )


@pytest.fixture
def piped(settings, fake_upstream):
    """(service, store, pool, client, fake) —— 全部走 mock 上游，零网络。"""
    store = TaskStore(settings.task_db)
    pool = AccountPool(settings, mint=lambda account: f"tok-{account.email}")
    client = QwenClient(settings, transport=fake_upstream.transport())
    service = QwenVideoService(settings, store, pool, client)
    return service, store, pool, client, fake_upstream


@pytest.fixture
def client_app(settings, fake_upstream):
    """FastAPI 应用（注入 mock 上游）。产出 (TestClient, fake, store, settings)。"""
    from fastapi.testclient import TestClient

    from app.main import create_app

    store = TaskStore(settings.task_db)
    pool = AccountPool(settings, mint=lambda account: f"tok-{account.email}")
    client = QwenClient(settings, transport=fake_upstream.transport())
    app = create_app(settings, store=store, pool=pool, client=client)
    with TestClient(app) as test_client:
        yield test_client, fake_upstream, store, settings


AUTH_A = {"Authorization": "Bearer sk-a"}
AUTH_B = {"Authorization": "Bearer sk-b"}
TASKS_PATH = "/api/v3/contents/generations/tasks"
