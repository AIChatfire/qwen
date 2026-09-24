"""对外宣告的模型清单 —— `GET /v1/models`（**OpenAI 形态**）。

2026-09-24 起清单 = **两部分**（用户指令：把上游 `GET /api/models` 注册进来）：
  1. **注册的上游 chat 模型**（`ChatModelRegistry`：TTL 缓存 + 失败回退上一份好清单）；
  2. 本服务视频能力条目 `qwen/video`（`CAPABILITIES`，原样保留 —— 视频方舟门不变）。

口径（沿用 `jimeng` / `hailuo` 家族纪律）：

  · 🔴 **只列真正支持的东西**。chat 门只做 t2t ⇒ 上游条目里 `chat_type` 不含 `t2t` 或
    已下线（`is_active: false`）的**不注册**；不支持的能力（尾帧 / 参考图 / 视频入参 /
    延长 / 音画联合）进 `DELIBERATE_ABSENCES` —— 出现在文档与门禁里，**不出现在清单里**。
  · `verified=True` 表示**已端到端实测**（2026-09-22 经本服务真实出片：t2v 与 i2v 各一条、
    产物容器时长 5.042s、归属自证一致，见 `docs/UPSTREAM.md` §7.1）。
    chat 模型**尚未实测**（适配纪律：不发真实生成请求）⇒ `verified: False`，诚实标注。
  · 扩展字段是**加性**的：OpenAI 原生只有 `id` / `object` / `created` / `owned_by`，
    其余（`title` / `media` / `accepts_image` / `notes`…）是本服务的诚实补充；
    对 unknown field 报错的严格客户端，只取前四个键即可。
  · `created`：**视频条目恒为 0**（本服务无从得知，不编时间戳）；chat 条目用上游自带的
    `info.created_at`（有真实来源，不算编造），缺省回 0。
"""
from __future__ import annotations

import logging
import threading
import time
from collections.abc import Callable

#: 本服务唯一受理的 provider 段（`model` 形态 = `qwen/<任意名>` 或裸名）。
PROVIDER = "qwen"

#: 上游出片固定 ~5.042s（n≥5 次容器实测），**不可指定**。
FIXED_DURATION_S = 5

#: 上游只吃这 5 个比例（与 `app/ark.py::RATIO_ENUM` 同源；枚举外一律落 1:1 + 降级说明）。
RATIO_ENUM = ("1:1", "3:4", "4:3", "16:9", "9:16")

#: 能力清单。一个模型两种形态：文生视频（t2v）与**单张首帧**图生视频（i2v）。
CAPABILITIES: list[dict] = [
    {
        "id": "qwen/video",
        "object": "model",
        "created": 0,
        "owned_by": PROVIDER,
        "title": "Qwen 视频生成（文生视频 / 单首帧图生视频）",
        "media": "video",
        "accepts_image": True,       # i2v：单张首帧
        "requires_image": False,     # t2v 同样支持
        "requires_prompt": True,     # 两种形态都必须有 text
        "max_input_images": 1,       # 上游只吃一张首帧（两张即 400，不替你挑一张）
        "duration_s": FIXED_DURATION_S,
        "ratios": list(RATIO_ENUM),
        "verified": True,
        "notes": "出片时长由上游固定 ~5.042s：请求 duration 会被吸附（>5）或 400（<5）；"
                 "图片入参只支持**单张首帧**，尾帧/参考图/视频入参一律 400（见 INTERFACE §2.2）。"
                 "视频任务走方舟契约门 POST /api/v3/contents/generations/tasks。",
    },
]

#: 刻意缺席的能力 —— 出现在文档与门禁里，**不出现在 `catalog()` 里**。
DELIBERATE_ABSENCES: dict[str, str] = {
    "qwen/image": "图片生成/编辑是另一条链路（image-adapter），本服务只做视频。",
    "qwen/video-ref": "上游没有参考图/参考视频能力 ⇒ `reference_image` 一律 400。",
    "qwen/video-last-frame": "上游没有尾帧能力 ⇒ `last_frame` 一律 400（也不悄悄当首帧）。",
    "qwen/video-extend": "上游没有视频延长/续写能力。",
    "qwen/video-audio": "上游产物无音轨（不支持音画联合）。",
}


def catalog() -> list[dict]:
    """视频能力条目（OpenAI 形态 + 加性扩展）。返回**副本**，调用方改不动本模块状态。"""
    return [dict(item) for item in CAPABILITIES]


def chat_entry_from_upstream(item: dict) -> dict | None:
    """上游 `GET /api/models` 条目 → OpenAI 形态条目；**不能跑 chat 的返回 None**（不注册）。

    过滤口径（2026-09-24 冻结）：`info.is_active is False`（上游已下线）或
    `info.meta.chat_type` 不含 `t2t` ⇒ 不注册 —— 列出来就是"制造假能力"。
    """
    item_id = str(item.get("id") or "").strip()
    if not item_id:
        return None
    info = item.get("info") if isinstance(item.get("info"), dict) else {}
    meta = info.get("meta") if isinstance(info.get("meta"), dict) else {}
    if info.get("is_active") is False:
        return None
    chat_types = meta.get("chat_type")
    if not isinstance(chat_types, list) or "t2t" not in chat_types:
        return None
    capabilities = meta.get("capabilities") if isinstance(meta.get("capabilities"), dict) else {}
    entry: dict = {
        "id": item_id,
        "object": "model",
        "created": int(info.get("created_at") or 0),
        "owned_by": PROVIDER,
        "title": str(item.get("name") or item_id),
        "media": "text",
        "task": "chat",
        "accepts_image": bool(capabilities.get("vision")),   # 图片解析：跟随上游 vision 能力
        "requires_prompt": True,
        "thinking": bool(capabilities.get("thinking")),
        "verified": False,
        "notes": "注册自上游 GET /api/models（TTL 缓存）；chat 任务走 POST /v1/chat/completions。"
                 "本门实测支持的输入 = 文本 + 单张图片（解析，§4.5）；document/video/audio "
                 "需上游 OSS 上传链路，未实现（U-15）。"
                 "🔴 context_length 是上游**自报**值（1M）：实测经网页端接口 ≈5 万汉字"
                 "（usage≈4 万 tokens）内可靠；≥6 万字触发上游 WAF（换出口/代理不可绕过，U-17）。",
    }
    #: 上游模型自报的输入能力（capabilities.*，照值透传 —— 只是上游的宣告，
    #: 本门实际支持面以 notes 为准）。
    for key in ("vision", "document", "video", "audio"):
        if key in capabilities:
            entry[key] = bool(capabilities.get(key))
    context_length = meta.get("max_context_length")
    if isinstance(context_length, int) and context_length > 0:
        entry["context_length"] = context_length
    return entry


class ChatModelRegistry:
    """上游模型清单的 TTL 缓存 —— `/v1/models` 的 chat 注册源。

    · 到期前直接用缓存；到期后拉一次上游（`fetch` 由装配层注入 = `client.list_upstream_models`）；
    · 拉取失败 ⇒ 回退**上一份好清单**，并把到期时间顺延一个 TTL（负缓存，不每次都打上游）；
    · 首拉失败且无缓存 ⇒ 空清单（端点此时只剩 `qwen/video` 条目，下次请求会再试）。
    🔴 绝不让 `/v1/models` 因为上游抖动而 5xx —— 能力探测挂了会把"上游清单拉不到"
    误读成"服务不可用"。
    """

    def __init__(self, fetch: Callable[[], list[dict]], *, ttl: float = 300.0,
                 clock: Callable[[], float] | None = None) -> None:
        self._fetch = fetch
        self._ttl = max(float(ttl), 1.0)
        self._clock = clock or time.monotonic
        self._entries: list[dict] = []
        self._expires_at = 0.0
        self._lock = threading.Lock()
        self._last_error = ""
        self._fetches = 0

    def entries(self) -> list[dict]:
        """注册条目（映射 + 过滤后的副本；线程安全）。"""
        now = self._clock()
        if now < self._expires_at:
            with self._lock:
                return [dict(item) for item in self._entries]
        try:
            raw = self._fetch()
            self._fetches += 1
        except Exception as exc:  # noqa: BLE001 - 回退旧清单，绝不外抛
            self._last_error = f"{type(exc).__name__}: {exc}"
            logging.getLogger("qwen.models").warning(
                "上游模型清单拉取失败（沿用上一份注册清单）：%s", self._last_error)
            with self._lock:
                self._expires_at = now + self._ttl
                return [dict(item) for item in self._entries]
        mapped = [entry for entry in (chat_entry_from_upstream(x) for x in raw) if entry]
        with self._lock:
            self._entries = mapped
            self._expires_at = now + self._ttl
            self._last_error = ""
        return [dict(item) for item in mapped]

    def stats(self) -> dict:
        """观测面用：缓存状态（不含清单内容）。"""
        with self._lock:
            return {
                "cached_models": len(self._entries),
                "expires_in_s": max(0, int(self._expires_at - self._clock())),
                "fetches": self._fetches,
                "last_error": self._last_error or None,
            }


__all__ = [
    "CAPABILITIES",
    "ChatModelRegistry",
    "DELIBERATE_ABSENCES",
    "FIXED_DURATION_S",
    "PROVIDER",
    "RATIO_ENUM",
    "catalog",
    "chat_entry_from_upstream",
]
