"""任务持久化：跨实例可读（重启不丢）/ 终态过滤 / prune 只碰过期终态 / KV。"""
from __future__ import annotations

import time

from app.store import TaskRecord, TaskStore


def make_rec(local_id: str = "cgt-1", **overrides) -> TaskRecord:
    rec = TaskRecord(local_id=local_id, upstream_task_id="t1", account="a@x.cn",
                     credential_id="hmac-sha256:x", model_requested="qwen/video",
                     prompt="p", ratio="16:9", status="running",
                     created_at=int(time.time()))
    for key, value in overrides.items():
        setattr(rec, key, value)
    return rec


def test_cross_instance_read_survives_restart(settings):
    """这条专门钉"重启丢任务"：进程内 dict 实现下，其余用例全绿、只有它会红。"""
    store1 = TaskStore(settings.task_db)
    store1.put(make_rec(status="succeeded", video_url="https://x/v.mp4"))
    store2 = TaskStore(settings.task_db)   # 新实例 = 进程重启
    rec = store2.get("cgt-1")
    assert rec is not None
    assert rec.status == "succeeded"
    assert rec.video_url == "https://x/v.mp4"


def test_degradations_roundtrip(settings):
    store = TaskStore(settings.task_db)
    rec = make_rec()
    rec.set_degradations(["第一条", "第二条"])
    store.put(rec)
    assert store.get("cgt-1").degradations == ["第一条", "第二条"]


def test_kv_roundtrip_and_delete(settings):
    store1 = TaskStore(settings.task_db)
    assert store1.kv_get("chat:a@x.cn") is None
    store1.kv_set("chat:a@x.cn", "chat-9")
    store2 = TaskStore(settings.task_db)
    assert store2.kv_get("chat:a@x.cn") == "chat-9"
    store2.kv_delete("chat:a@x.cn")
    assert store1.kv_get("chat:a@x.cn") is None


def test_list_active_excludes_terminal(settings):
    store = TaskStore(settings.task_db)
    store.put(make_rec("cgt-run", status="running"))
    store.put(make_rec("cgt-done", status="succeeded"))
    store.put(make_rec("cgt-failed", status="failed"))
    actives = {rec.local_id for rec in store.list_active()}
    assert actives == {"cgt-run"}
    assert store.count() == 3 and store.count_active() == 1


def test_prune_removes_only_terminal_records_past_retention(settings):
    store = TaskStore(settings.task_db)
    old = int(time.time()) - 30 * 86400
    store.put(make_rec("cgt-old-done", status="succeeded", created_at=old))
    store.put(make_rec("cgt-old-run", status="running", created_at=old))
    store.put(make_rec("cgt-new", status="succeeded"))
    removed = store.prune(days=7)
    assert removed == 1
    assert store.get("cgt-old-done") is None
    assert store.get("cgt-old-run") is not None   # 非终态不动
    assert store.get("cgt-new") is not None


def test_list_recent_filters_by_credential(settings):
    store = TaskStore(settings.task_db)
    store.put(make_rec("cgt-a", credential_id="hmac-sha256:aa"))
    store.put(make_rec("cgt-b", credential_id="hmac-sha256:bb"))
    ids = {rec.local_id for rec in store.list_recent(credential_id="hmac-sha256:aa")}
    assert ids == {"cgt-a"}
