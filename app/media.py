"""输入素材（首帧图）的最小处理：形态校验 + 上游认识度判定。

本层**不下载、不转存**（v1 范围收敛）：
  · 上游 i2v 是"**引用**上游已经认识的一张图"（抓包实测：URL 指向上游 CDN / OSS 资源）；
  · 外链图能否直接充当这个 `files[0].url` **未完全验证**（见 docs/UPSTREAM.md U-1/U-7）
    ⇒ 不是已知上游域名时**照发 + 告警**（不静默、不假装已验证）；
  · `data:` URI 无法转发（没有上传链路）⇒ **400**，明确说清"请给可公网访问的 URL"。
"""
from __future__ import annotations

import posixpath
import urllib.parse

#: 已知上游认识的图片来源（抓包实测出现过的域名族）。
KNOWN_IMAGE_HOSTS = ("cdn.qwenlm.ai",)
KNOWN_IMAGE_HOST_SUFFIXES = (".qwenlm.ai",)
KNOWN_IMAGE_HOST_PREFIXES = ("qwen-chat.oss-",)

_IMAGE_SUFFIX_TYPES = {
    ".png": "image/png",
    ".jpg": "image/jpeg",
    ".jpeg": "image/jpeg",
    ".webp": "image/webp",
    ".bmp": "image/bmp",
    ".gif": "image/gif",
}


def is_known_host(url: str) -> bool:
    """该 URL 是否落在"上游已知认识"的域名里（判据来自抓包，不含推断）。"""
    host = urllib.parse.urlsplit(url).hostname or ""
    if host in KNOWN_IMAGE_HOSTS:
        return True
    if host.endswith(KNOWN_IMAGE_HOST_SUFFIXES):
        return True
    return host.startswith(KNOWN_IMAGE_HOST_PREFIXES)


def validate_image_url(url: str, *, param: str) -> str:
    """形态校验：只收 http(s) 绝对地址；`data:` 明确拒绝（没有上传链路可承载）。"""
    from .errors import InvalidParameterError

    url = (url or "").strip()
    if not url:
        raise InvalidParameterError("image_url.url 不能为空", param=param)
    scheme = urllib.parse.urlsplit(url).scheme.lower()
    if scheme in ("http", "https"):
        return url
    if scheme == "data":
        raise InvalidParameterError(
            "本服务暂不接收 data: URI 首帧图（无上传链路）—— 请给可公网访问的图片 URL。",
            param=f"{param}.url",
        )
    raise InvalidParameterError(
        f"image_url.url 必须是 http(s) 绝对地址，收到 scheme={scheme or '(空)'!r}",
        param=f"{param}.url",
    )


def filename_and_type(url: str) -> tuple[str, str]:
    """从 URL 猜 `name` / `file_type`（猜不出时给中性值；只是上游的展示字段）。"""
    path = urllib.parse.urlsplit(url).path
    name = posixpath.basename(path) or "image.png"
    suffix = posixpath.splitext(name)[1].lower()
    return name, _IMAGE_SUFFIX_TYPES.get(suffix, "image/png")


def image_entry(url: str) -> dict:
    """`content[].image_url` → 上游 `messages[0].files[0]` 的条目形状。

    形状依据 = 用户 2026-09-22 抓包（见 docs/UPSTREAM.md §7.2）：
    `type/name/file_type/showType/status/file_class/url` —— 刻意**不加** `isQuote`、
    `id`/`itemId`（早先版本推断过这两处，实测抓包里没有；不要凭推断补字段）。
    """
    name, file_type = filename_and_type(url)
    return {
        "type": "image",
        "name": name,
        "file_type": file_type,
        "showType": "image",
        "status": "uploaded",
        "file_class": "vision",
        "url": url,
    }


def host_warning(url: str) -> str | None:
    """非已知上游域名 ⇒ 一条可行动的告警（照发，但把不确定性说出来）。"""
    if is_known_host(url):
        return None
    host = urllib.parse.urlsplit(url).hostname or "?"
    return (
        f"首帧图来自非上游域名（{host}）：i2v 的输入是「上游已认识的图」，"
        "外链图能否被接受尚未完全验证（docs/UPSTREAM.md U-1/U-7）—— 已照发，失败时优先改用上游 CDN 地址。"
    )
