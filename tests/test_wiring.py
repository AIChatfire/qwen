"""接线：工厂可解析 / 路由存在 / 范围外端点确实不存在 / Dockerfile CMD 指向工厂。"""
from __future__ import annotations

from pathlib import Path

from app.main import create_app
from tests.conftest import AUTH_A, TASKS_PATH


def test_factory_resolves_and_routes_declared(settings):
    app = create_app(settings)
    assert app.title == "qwen-service"
    paths = {getattr(route, "path", "") for route in app.routes}
    for expected in ("/api/v3/contents/generations/tasks",
                     "/api/v3/contents/generations/tasks/{task_id}",
                     "/healthz", "/readyz", "/stats"):
        assert expected in paths, f"缺路由 {expected}"


def test_out_of_scope_endpoints_absent(client_app):
    """范围冻结：列表 / 取消删除刻意不实现 —— 路由层级表现为 404/405，而不是空数据。"""
    tc, *_ = client_app
    assert tc.get(TASKS_PATH, headers=AUTH_A).status_code in (404, 405)
    assert tc.delete(f"{TASKS_PATH}/cgt-x", headers=AUTH_A).status_code in (404, 405)


def test_healthz_and_readyz(client_app):
    tc, *_ = client_app
    assert tc.get("/healthz").json()["status"] == "ok"
    ready = tc.get("/readyz").json()
    assert ready["ready"] is True
    assert ready["accounts"] == 2


def test_stats_never_leaks_credentials(client_app):
    tc, *_ = client_app
    stats = tc.get("/stats").json()
    text = str(stats)
    assert "tok-" not in text
    assert "pw-a" not in text


def test_dockerfile_cmd_targets_the_factory():
    dockerfile = Path(__file__).resolve().parent.parent / "Dockerfile"
    assert "app.main:create_app()" in dockerfile.read_text(encoding="utf-8")
