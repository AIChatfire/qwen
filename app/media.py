"""输入素材（图片）的最小处理：形态校验 + 上游认识度判定。

本层**不下载、不转存**（v1 范围收敛）：
  · 上游是"**引用**上游已经认识的一个文件"（抓包实测：URL 指向上游 CDN / OSS 资源）；
  · 外链图能否直接充当 `files[].url`：**上游域内图已实测可用**（视频门 U-1 部分关闭 + chat 门
    2026-09-24 图片解析实测，见 docs/UPSTREAM.md §4.5）；**第三方域名未证** ⇒ 照发 + 告警
    （不静默、不假装已验证）；
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
            "本服务暂不接收 data: URI 图片（无上传链路）—— 请给可公网访问的图片 URL。",
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
    """OpenAI `image_url` → 上游 `messages[0].files[]` 的条目形状（i2v 与 t2t 图片解析通用）。

    形状依据 = 用户 2026-09-22 抓包 + chat 门 2026-09-24 图片解析实测（UPSTREAM §4.2/§4.5）：
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
        f"图片来自非上游域名（{host}）：上游实测接受的是「上游已认识的图」（chat 门图片解析与"
        "i2v 均用上游域内图验证，docs/UPSTREAM.md §4.5），外链图未验证 —— 已照发，"
        "失败时优先改用上游 CDN/OSS 地址。"
    )


# ------------------------------------------------------------------ 附件解析（上传链）

import base64  # noqa: E402
import ipaddress  # noqa: E402
import posixpath as _pp  # noqa: E402
import socket  # noqa: E402
import time  # noqa: E402

import httpx  # noqa: E402

#: 允许的附件 scheme。
_ALLOWED_SCHEMES = ("http", "https", "data")
#: 扩展名 → MIME 兜底表（响应无 Content-Type 时用）。
_EXT_TYPES = {
    ".pdf": "application/pdf", ".doc": "application/msword",
    ".docx": "application/vnd.openxmlformats-officedocument.wordprocessingml.document",
    ".txt": "text/plain", ".md": "text/markdown", ".csv": "text/csv",
    ".xls": "application/vnd.ms-excel",
    ".xlsx": "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet",
    ".pptx": "application/vnd.openxmlformats-officedocument.presentationml.presentation",
    ".mp3": "audio/mpeg", ".wav": "audio/wav", ".m4a": "audio/mp4", ".flac": "audio/flac",
    ".mp4": "video/mp4", ".mov": "video/quicktime", ".webm": "video/webm",
    **_IMAGE_SUFFIX_TYPES,
}


def _assert_public_host(url: str) -> str:
    """SSRF 防护：只放行解析到**公网**地址的 http(s) URL（私网/回环/链路本地一律拒绝）。"""
    from .errors import InvalidParameterError

    parts = urllib.parse.urlsplit(url)
    host = (parts.hostname or "").strip()
    if not host:
        raise InvalidParameterError("附件 URL 缺少主机名", param="attachment.url")
    try:
        infos = socket.getaddrinfo(host, None)
    except OSError as exc:
        raise InvalidParameterError(f"附件 URL 主机无法解析：{host}", param="attachment.url") from exc
    for info in infos:
        ip = ipaddress.ip_address(info[4][0])
        if (ip.is_private or ip.is_loopback or ip.is_link_local or ip.is_reserved
                or ip.is_multicast or ip.is_unspecified):
            raise InvalidParameterError(
                f"附件 URL 指向非公网地址（{ip}）—— 已拒绝", param="attachment.url")
    return url


def resolve_attachment(kind: str, source: str, *, max_bytes: int,
                       param: str) -> tuple[bytes, str, str]:
    """附件来源 → (bytes, filename, content_type)，供上传链使用。

    · `data:` URI ⇒ 直接解码（base64，仅 file/audio/image 文档化形态）；
    · http(s) ⇒ SSRF 防护（公网校验）+ 流式下载（超 max_bytes 即断）；
    · filename / content_type：优先响应头，缺失时按 URL 扩展名兜底。
    """
    from .errors import InvalidParameterError

    source = (source or "").strip()
    if source.startswith("data:"):
        try:
            head, b64 = source.split(",", 1)
        except ValueError as exc:
            raise InvalidParameterError("data: URI 形态错误（缺逗号）", param=param) from exc
        mime = head[5:].split(";", 1)[0] or "application/octet-stream"
        try:
            data = base64.b64decode(b64)
        except Exception as exc:
            raise InvalidParameterError("data: URI base64 解码失败", param=param) from exc
        return _cap(data, max_bytes, param), f"upload-{int(time.time())}.{_ext_of(mime)}", mime
    validate_scheme = urllib.parse.urlsplit(source).scheme.lower()
    if validate_scheme not in ("http", "https"):
        raise InvalidParameterError(
            f"附件来源只支持 http(s) 或 data: URI（收到 scheme={validate_scheme!r}）",
            param=param)
    _assert_public_host(source)
    with httpx.Client(timeout=httpx.Timeout(connect=15, read=120, write=60, pool=15),
                      trust_env=False, follow_redirects=True) as dl:
        try:
            with dl.stream("GET", source) as resp:
                if resp.status_code >= 400:
                    raise InvalidParameterError(
                        f"附件下载失败：源站 HTTP {resp.status_code}", param=param)
                declared = (resp.headers.get("content-type") or "").split(";")[0].strip()
                buf = bytearray()
                for chunk in resp.iter_bytes(65536):
                    buf.extend(chunk)
                    if len(buf) > max_bytes:
                        raise InvalidParameterError(
                            f"附件超过大小上限（>{max_bytes // 1_000_000}MB）", param=param)
        except httpx.HTTPError as exc:
            raise InvalidParameterError(
                f"附件下载失败：{type(exc).__name__}", param=param) from exc
    path = urllib.parse.urlsplit(source).path
    name = _pp.basename(path) or f"upload-{int(time.time())}"
    ext = _pp.splitext(name)[1].lower()
    ctype = declared or _EXT_TYPES.get(ext, "application/octet-stream")
    return _cap(bytes(buf), max_bytes, param), name, ctype


def _cap(data: bytes, max_bytes: int, param: str) -> bytes:
    if len(data) > max_bytes:
        from .errors import InvalidParameterError
        raise InvalidParameterError(
            f"附件超过大小上限（>{max_bytes // 1_000_000}MB）", param=param)
    if not data:
        raise InvalidParameterError("附件内容为空（0 字节）", param=param)
    return data


def _ext_of(mime: str) -> str:
    return {"application/pdf": "pdf", "text/plain": "txt", "audio/mpeg": "mp3",
            "audio/wav": "wav", "video/mp4": "mp4"}.get(mime, "bin")
