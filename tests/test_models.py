"""`GET /v1/models` —— OpenAI 形态的能力清单。零网络。

口径与 `jimeng` / `hailuo` 对齐：**只列真正支持的**（不支持的进 `DELIBERATE_ABSENCES`，
出现在文档与门禁里、不出现在清单里）；`created` 恒 0（不编时间戳）。
"""
from __future__ import annotations

from app import models
from tests.conftest import AUTH_A, TASKS_PATH

MODELS_PATH = "/v1/models"

#: OpenAI 原生四键 —— 无论加多少扩展字段，这四个必须在。
OPENAI_KEYS = ("id", "object", "created", "owned_by")


def test_models_is_openai_shaped(client_app):
    tc, *_ = client_app
    resp = tc.get(MODELS_PATH)
    assert resp.status_code == 200
    body = resp.json()
    assert body["object"] == "list"
    assert isinstance(body["data"], list) and body["data"]

    for item in body["data"]:
        assert item["object"] == "model"
        assert item["created"] == 0, "不编时间戳（OpenAI 语义是模型创建时间，本服务无从得知）"
        assert item["owned_by"] == models.PROVIDER
        assert set(OPENAI_KEYS) <= set(item), "OpenAI 原生四键必须在"


def test_only_verified_capabilities_are_listed(client_app):
    """清单里出现的 id 必须都在 `CAPABILITIES` 里，且都是 verified —— 不许出现"疑似能力"。"""
    tc, *_ = client_app
    ids = {m["id"] for m in tc.get(MODELS_PATH).json()["data"]}
    assert ids == {"qwen/video"}
    assert all(c["verified"] for c in models.CAPABILITIES)


def test_deliberate_absences_never_appear(client_app):
    """刻意缺席的（参考图 / 尾帧 / 延长 / 音画 / 图片链路）不得出现在清单里 —— 那就是制造假能力。"""
    tc, *_ = client_app
    ids = {m["id"] for m in tc.get(MODELS_PATH).json()["data"]}
    assert models.DELIBERATE_ABSENCES, "缺席清单不能是空的，否则这条门禁是空转"
    for absent in models.DELIBERATE_ABSENCES:
        assert absent not in ids
        assert absent not in {c["id"] for c in models.CAPABILITIES}


def test_catalog_does_not_leak_mutable_state(client_app):
    """`catalog()` 必须返回副本：调用方改响应不能污染进程内的清单。"""
    tc, *_ = client_app
    first = tc.get(MODELS_PATH).json()["data"]
    first[0]["id"] = "tampered"
    first[0]["title"] = "tampered"
    again = tc.get(MODELS_PATH).json()["data"]
    assert again[0]["id"] == "qwen/video"
    assert again[0]["title"] == models.CAPABILITIES[0]["title"]


def test_models_needs_no_key_and_a_bogus_key_still_lists(client_app):
    """能力探测不需要 Key（探 Key 之前就要能拿到清单）；带错 Key 也照常返回。"""
    tc, *_ = client_app
    assert tc.get(MODELS_PATH, headers={"Authorization": "Bearer sk-nope"}).status_code == 200
    assert tc.get(MODELS_PATH, headers=AUTH_A).json()["object"] == "list"


def test_capability_facts_match_the_frozen_contract(client_app):
    """清单里的规格必须与契约同源 —— 别让"宣告的能力"与"实际受理的"漂移。"""
    from app.ark import RATIO_ENUM, UPSTREAM_FIXED_DURATION

    tc, *_ = client_app
    item = tc.get(MODELS_PATH).json()["data"][0]
    assert item["ratios"] == list(RATIO_ENUM)
    assert item["duration_s"] == UPSTREAM_FIXED_DURATION

    # 而"两张图 / 尾帧"这类契约拒绝的形态，不得在清单里被承诺支持
    assert item["max_input_images"] == 1
    assert item["accepts_image"] is True and item["requires_image"] is False


def test_models_route_does_not_disturb_the_ark_surface(client_app):
    """加了能力清单之后，方舟任务路径不受影响（列表端点仍刻意不存在）。"""
    tc, *_ = client_app
    assert tc.get(TASKS_PATH).status_code in (404, 405)
    assert tc.post(MODELS_PATH).status_code == 405
