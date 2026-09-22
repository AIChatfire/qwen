"""对外宣告的模型清单 —— `GET /v1/models`（**OpenAI 形态**）。

口径（沿用 `jimeng` / `hailuo` 家族纪律）：

  · 🔴 **只列本服务真正支持的东西**。不支持的能力（尾帧 / 参考图 / 视频入参 / 延长 / 音画联合）
    进 `DELIBERATE_ABSENCES` —— 出现在文档与门禁里，**不出现在清单里**：列出来就是"制造假能力"。
  · `verified=True` 表示**已端到端实测**（2026-09-22 经本服务真实出片：t2v 与 i2v 各一条、
    产物容器时长 5.042s、归属自证一致，见 `docs/UPSTREAM.md` §7.1）。
  · 扩展字段是**加性**的：OpenAI 原生只有 `id` / `object` / `created` / `owned_by`，
    其余（`title` / `media` / `accepts_image` / `duration_s` / `ratios` / `notes`…）
    是本服务的诚实补充；对 unknown field 报错的严格客户端，只取前四个键即可。
  · `created` **恒为 0**：OpenAI 语义是"模型创建时间"，本服务无从得知 ——
    **不编时间戳**（"看起来合理"的常量比留空更糟）。
"""
from __future__ import annotations

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
                 "图片入参只支持**单张首帧**，尾帧/参考图/视频入参一律 400（见 INTERFACE §2.2）。",
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
    """`GET /v1/models` 的 `data`（OpenAI 形态 + 加性扩展）。返回**副本**，调用方改不动本模块状态。"""
    return [dict(item) for item in CAPABILITIES]


__all__ = [
    "CAPABILITIES",
    "DELIBERATE_ABSENCES",
    "FIXED_DURATION_S",
    "PROVIDER",
    "RATIO_ENUM",
    "catalog",
]
