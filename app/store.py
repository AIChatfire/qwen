"""任务持久化 —— SQLModel（SQLite 默认 / PostgreSQL 可选）。

🔴 视频侧硬约束（与图片侧不同）：任务记录要能扛住**进程重启**且保留 ≥7 天 ——
契约要求 `GET /tasks/{id}` 在 7 天窗口内可用，重启后查不到 = 调用方以为还在跑的任务凭空消失。
所以**不允许"进程内 dict 兜底"**；DSN 配错要直接抛（不静默退化成内存）。

存储层接口化：`put/get/delete/list_recent/list_active/count/count_queued/prune` + `kv_*`，
换后端时调用方零改动。

加字段的口径（沿用 jimeng 的教训）：**SQLModel 的 `create_all` 只建表、不改已有表** ——
本类启动期用 `_ensure_columns()` 做幂等补列；以后加列照此，别走手工 ALTER。
"""
from __future__ import annotations

import json
import time
from pathlib import Path

from sqlmodel import Field, Session, SQLModel, create_engine, select


class TaskRecord(SQLModel, table=True):
    """一条视频任务。`local_id`（cgt-…）对外；`upstream_task_id` 只在本层与观测面用。"""

    __tablename__ = "qwen_tasks"

    local_id: str = Field(primary_key=True)
    upstream_task_id: str = ""
    chat_id: str = ""
    account: str = ""
    credential_id: str = ""
    model_requested: str = ""
    prompt: str = ""
    ratio: str = ""
    image_url: str = ""
    status: str = ""
    video_url: str | None = None
    error_code: str | None = None
    error_message: str | None = None
    degradations_json: str = "[]"
    #: 轻量队列：已尝试提交次数 / 下次可尝试的 epoch 秒（退避窗）
    attempts: int = 0
    next_attempt_at: int = 0
    created_at: int = 0
    updated_at: int = 0

    @property
    def degradations(self) -> list[str]:
        try:
            data = json.loads(self.degradations_json or "[]")
        except ValueError:
            return []
        return [str(x) for x in data] if isinstance(data, list) else []

    def set_degradations(self, items: list[str]) -> None:
        self.degradations_json = json.dumps(list(items), ensure_ascii=False)


class KVRecord(SQLModel, table=True):
    """通用小状态：每账号复用的 `chat_id`、账号池的额度/冷却快照。"""

    __tablename__ = "qwen_kv"

    key: str = Field(primary_key=True)
    value: str = ""
    updated_at: int = 0


TERMINAL_STATUSES = ("succeeded", "failed", "expired")


class TaskStore:
    def __init__(self, dsn: str) -> None:
        if not dsn:
            raise ValueError("TASK_DB 不能为空（不静默退回内存后端）")
        connect_args: dict = {}
        if dsn.startswith("sqlite"):
            connect_args = {"check_same_thread": False}
            path_part = dsn.split("///", 1)[-1]
            if path_part and path_part != ":memory:":
                Path(path_part).parent.mkdir(parents=True, exist_ok=True)
        self.engine = create_engine(dsn, connect_args=connect_args, pool_pre_ping=True)
        SQLModel.metadata.create_all(self.engine)
        self._ensure_columns()

    def _ensure_columns(self) -> None:
        """幂等补列（`create_all` 不改已有表）。旧库升级后 `put()` 不会再报 column does not exist。"""
        wanted = {
            "attempts": "INTEGER NOT NULL DEFAULT 0",
            "next_attempt_at": "INTEGER NOT NULL DEFAULT 0",
        }
        table = TaskRecord.__tablename__
        with self.engine.begin() as conn:
            if self.engine.dialect.name == "sqlite":
                rows = conn.exec_driver_sql(f"PRAGMA table_info({table})").fetchall()
                existing = {row[1] for row in rows}
            else:
                rows = conn.exec_driver_sql(
                    "SELECT column_name FROM information_schema.columns WHERE table_name = %s",
                    (table,)).fetchall()
                existing = {row[0] for row in rows}
            for name, ddl in wanted.items():
                if name not in existing:
                    conn.exec_driver_sql(f"ALTER TABLE {table} ADD COLUMN {name} {ddl}")

    # ------------------------------------------------------------------ 任务

    def put(self, rec: TaskRecord) -> TaskRecord:
        rec.updated_at = int(time.time())
        if not rec.created_at:
            rec.created_at = rec.updated_at
        with Session(self.engine) as session:
            merged = session.merge(rec)
            session.commit()
            session.refresh(merged)
            return merged

    def get(self, local_id: str) -> TaskRecord | None:
        with Session(self.engine) as session:
            return session.get(TaskRecord, local_id)

    def delete(self, local_id: str) -> bool:
        with Session(self.engine) as session:
            rec = session.get(TaskRecord, local_id)
            if rec is None:
                return False
            session.delete(rec)
            session.commit()
            return True

    def list_recent(self, *, credential_id: str | None = None, limit: int = 50) -> list[TaskRecord]:
        with Session(self.engine) as session:
            stmt = select(TaskRecord).order_by(TaskRecord.created_at.desc()).limit(limit)  # type: ignore[attr-defined]
            if credential_id is not None:
                stmt = (
                    select(TaskRecord)
                    .where(TaskRecord.credential_id == credential_id)
                    .order_by(TaskRecord.created_at.desc())  # type: ignore[attr-defined]
                    .limit(limit)
                )
            return list(session.exec(stmt))

    def list_active(self) -> list[TaskRecord]:
        with Session(self.engine) as session:
            stmt = select(TaskRecord).where(TaskRecord.status.not_in(TERMINAL_STATUSES))  # type: ignore[attr-defined]
            return list(session.exec(stmt))

    def count(self) -> int:
        with Session(self.engine) as session:
            return len(list(session.exec(select(TaskRecord))))

    def count_active(self) -> int:
        return len(self.list_active())

    def count_queued(self) -> int:
        with Session(self.engine) as session:
            stmt = select(TaskRecord).where(TaskRecord.status == "queued")  # type: ignore[attr-defined]
            return len(list(session.exec(stmt)))

    def prune(self, days: int = 7) -> int:
        """删除早于 `days` 天的**终态**记录（保留期按 `created_at` 起算，非终态一律不动）。"""
        cutoff = int(time.time()) - days * 86400
        removed = 0
        with Session(self.engine) as session:
            stmt = select(TaskRecord).where(TaskRecord.created_at < cutoff)  # type: ignore[attr-defined]
            for rec in list(session.exec(stmt)):
                if rec.status in TERMINAL_STATUSES:
                    session.delete(rec)
                    removed += 1
            session.commit()
        return removed

    # ------------------------------------------------------------------ KV

    def kv_get(self, key: str) -> str | None:
        with Session(self.engine) as session:
            rec = session.get(KVRecord, key)
            return rec.value if rec else None

    def kv_set(self, key: str, value: str) -> None:
        with Session(self.engine) as session:
            rec = session.get(KVRecord, key) or KVRecord(key=key)
            rec.value = value
            rec.updated_at = int(time.time())
            session.merge(rec)
            session.commit()

    def kv_delete(self, key: str) -> None:
        with Session(self.engine) as session:
            rec = session.get(KVRecord, key)
            if rec:
                session.delete(rec)
                session.commit()
