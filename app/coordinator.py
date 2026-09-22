"""单实例后台协调器（**默认开启**，`COORDINATOR_ENABLED=0` 关闭）。

惰性轮询（只在调用方 GET 时推进）做不到三件事，协调器补上：
  1. **出队**：队列里的 `queued` 任务在容量恢复后要有人把它递交给上游（没人查询也会推进）；
  2. **超时看门狗**：没人查的任务会永远停在 `running`；协调器把超过 `TASK_TIMEOUT`
     的任务置 `expired`；
  3. **产物就绪度**：完成后尽快把 `succeeded` 落库，调用方首次 GET 即可见。

⚠️ 多 worker 部署要另做选主/租约去重（本服务默认 `WORKERS=1`，与 jimeng 同口径）；
⚠️ 它**只推进自己的任务记录**（账号在记录里），不引入任何新的上游写操作。
"""
from __future__ import annotations

import asyncio
import logging

from .config import Settings
from .service import QwenVideoService
from .store import TaskStore

logger = logging.getLogger("qwen.coordinator")


class Coordinator:
    def __init__(self, service: QwenVideoService, store: TaskStore, settings: Settings) -> None:
        self.service = service
        self.store = store
        self.settings = settings

    async def run(self) -> None:
        logger.info("协调器启动（tick=%ss, timeout=%ss）",
                    self.settings.coordinator_tick, self.settings.task_timeout)
        ticks = 0
        while True:
            try:
                await asyncio.to_thread(self.service.poll_active_once)
                ticks += 1
                if ticks % 720 == 0:  # 约每小时清一次过期记录（保留期 7 天）
                    removed = await asyncio.to_thread(
                        self.store.prune, self.settings.task_retention_days)
                    if removed:
                        logger.info("清理过期任务记录 %s 条", removed)
            except Exception as exc:  # noqa: BLE001 - 协调器不许把主进程带崩
                logger.warning("协调器 tick 失败（继续下一轮）：%s", exc)
            await asyncio.sleep(self.settings.coordinator_tick)
