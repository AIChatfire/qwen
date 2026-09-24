"""qwen 附件上传链 —— getstsToken → OSS V4 签名 PUT → files[] 条目。

契约（2026-09-24 实测 + 参考 `image-adapter/script_store/qwen/images@v1.py`：
图片侧已端到端验证的同一条链；登记 UPSTREAM §4.7）：

  1. `POST /api/v2/files/getstsToken` body `{"filename", "filesize"(字符串), "filetype"}`
     —— filetype ∈ image/video/audio/file（前端 getFileType 四类）；
     file_type 形态缺失 ⇒ `Bad_Request "Invalid file information!"`。
     → `data{access_key_id, access_key_secret, security_token, bucketname, endpoint,
        file_path, file_url, file_id, region}`。
  2. PUT（**虚拟主机** `https://{bucket}.{endpoint}/{file_path}`，V4 头签名）：
     · 派生：`k_date = HMAC("aliyun_v4" + sk, date)`（🔴 前缀是 aliyun_v4，
       不是 aliyun_v4_request —— 预签名 URL 与两种错误派生均 403 实测）；
       `k_region → k_service("oss") → k_signing("aliyun_v4_request")`；
     · canonical：`PUT / {encode(/bucket/file_path)} / 空 query / 头（content-type、
       x-oss-content-sha256:UNSIGNED-PAYLOAD、x-oss-date、x-oss-security-token）/ 空 / UNSIGNED-PAYLOAD`；
     · region 需剥掉返回值里的 `oss-` 前缀（对全局加速端点会被判签名 region 错误）。
  3. files[] 条目 = 前端完整形状（`url` 用带签名的 file_url，300s 有效——用完即弃；
     `file_class`: file 类是 "document"，其余与 kind 同值）。

图片直链（§4.2 已证）与上传链（本模块）并存：图片 http(s) 直链走 §4.2 直引；
文件/音频/视频/data: 图片走本模块上传。
"""
from __future__ import annotations

import hashlib
import hmac
import urllib.parse
import uuid
from datetime import UTC, datetime

import httpx

from ...errors import UpstreamError

#: kind → (showType, file_class)（2026-09-24 逐类实测 + 前端形状对齐）。
ENTRY_SHAPES: dict[str, tuple[str, str]] = {
    "image": ("image", "vision"),
    "file": ("file", "document"),
    "audio": ("audio", "audio"),
    "video": ("video", "video"),
}


def _filetype_of(mime: str) -> str:
    """MIME → getstsToken 的四类之一（前端 getFileType 同款）。"""
    low = (mime or "").lower()
    if low.startswith("image"):
        return "image"
    if low.startswith("video"):
        return "video"
    if low.startswith("audio"):
        return "audio"
    return "file"


def build_entry(kind: str, filename: str, file_type: str, sts: dict) -> dict:
    """前端完整 files[] 条目形状（含 file:{}/greenNet/itemId —— 逐字段镜像，不省略）。"""
    fid = str(sts.get("file_id") or "")
    show, file_class = ENTRY_SHAPES[kind]
    return {
        "type": show,
        "file": {},
        "id": fid,
        "url": sts.get("file_url") or "",
        "name": filename,
        "collection_name": "",
        "progress": 100,
        "status": "uploaded",
        "greenNet": "success",
        "size": sts.get("_size") or 0,
        "error": "",
        "itemId": fid or uuid.uuid4().hex,
        "file_type": file_type,
        "showType": show,
        "file_class": file_class,
    }


def get_sts_token(client: httpx.Client, token: str, *, filename: str,
                  content_type: str, data: bytes, headers_fn) -> dict:
    """`POST /api/v2/files/getstsToken` → data（STS 凭证 + bucket/endpoint/file_path）。"""
    body = {"filename": filename,
            "filesize": str(len(data)),
            "filetype": _filetype_of(content_type)}
    resp = client.post("/api/v2/files/getstsToken", json=body,
                       headers=headers_fn(token, referer=f"{client.base_url}/"))
    try:
        payload = resp.json()
    except ValueError as exc:
        raise UpstreamError(
            f"getstsToken 响应不是 JSON（HTTP {resp.status_code}）") from exc
    if not payload.get("success"):
        d = payload.get("data") or {}
        raise UpstreamError(
            f"getstsToken 被拒：{d.get('code', '?')} {str(d.get('details', ''))[:120]}")
    sts = payload.get("data") or {}
    missing = [k for k in ("access_key_id", "access_key_secret", "security_token",
                           "bucketname", "endpoint", "file_path") if not sts.get(k)]
    if missing:
        raise UpstreamError(f"getstsToken 响应缺字段：{missing}")
    sts["_size"] = len(data)
    return sts


def _oss_target(sts: dict) -> tuple[str, str]:
    """(bucket 限定 host, 裸 region)。region 剥 `oss-` 前缀（对加速端点是签名错误源）。"""
    endpoint = str(sts.get("endpoint") or "").strip()
    for prefix in ("https://", "http://"):
        if endpoint.startswith(prefix):
            endpoint = endpoint[len(prefix):]
    endpoint = endpoint.rstrip("/")
    bucket = str(sts.get("bucketname") or "").strip()
    host = endpoint if endpoint.startswith(bucket + ".") else f"{bucket}.{endpoint}"
    region = str(sts.get("region") or "").strip()
    if region.startswith("oss-"):
        region = region[len("oss-"):]
    return host, (region or "cn-hangzhou")


def _encode_path(path: str) -> str:
    if not path.startswith("/"):
        path = "/" + path
    return urllib.parse.quote(path, safe="/-_.~")


def _v4_put_headers(sts: dict, canonical_path: str, mime: str) -> dict:
    """OSS V4 头签名（UNSIGNED-PAYLOAD 模式，逐字对齐 image-adapter 已验证实现）。"""
    ak = sts["access_key_id"]
    sk = sts["access_key_secret"]
    sts_token = sts.get("security_token") or ""
    now = datetime.now(UTC)
    date = now.strftime("%Y%m%d")
    ts = now.strftime("%Y%m%dT%H%M%SZ")
    _, region = _oss_target(sts)
    scope = f"{date}/{region}/oss/aliyun_v4_request"
    payload_hash = "UNSIGNED-PAYLOAD"
    signable = {"x-oss-content-sha256": payload_hash, "x-oss-date": ts,
                "content-type": mime}
    if sts_token:
        signable["x-oss-security-token"] = sts_token
    canonical_headers = "".join(f"{k}:{str(signable[k]).strip()}\n" for k in sorted(signable))
    canonical_request = "\n".join(["PUT", _encode_path(canonical_path), "",
                                   canonical_headers, "", payload_hash])
    string_to_sign = "\n".join(["OSS4-HMAC-SHA256", ts, scope,
                                hashlib.sha256(canonical_request.encode()).hexdigest()])
    key = hmac.new(("aliyun_v4" + sk).encode(), date.encode(), hashlib.sha256).digest()
    key = hmac.new(key, region.encode(), hashlib.sha256).digest()
    key = hmac.new(key, b"oss", hashlib.sha256).digest()
    key = hmac.new(key, b"aliyun_v4_request", hashlib.sha256).digest()
    signature = hmac.new(key, string_to_sign.encode(), hashlib.sha256).hexdigest()
    out = {k: v for k, v in signable.items()}
    out["Authorization"] = ("OSS4-HMAC-SHA256 Credential=" + ak + "/" + scope
                            + ",Signature=" + signature)
    return out


def upload_attachment(client: httpx.Client, token: str, *, kind: str, filename: str,
                      content_type: str, data: bytes, headers_fn,
                      extra_cookies: str = "") -> dict:
    """完整上传：getstsToken → OSS V4 签名 PUT → 前端形状 files[] 条目。"""
    sts = get_sts_token(client, token, filename=filename, content_type=content_type,
                        data=data, headers_fn=headers_fn)
    host, _ = _oss_target(sts)
    path = str(sts.get("file_path") or "")
    url = f"https://{host}{_encode_path('/' + path)}"
    canonical_path = f"/{sts.get('bucketname')}/{path}"
    hdrs = _v4_put_headers(sts, canonical_path, content_type)
    put = client.put(url, content=data, headers=hdrs, timeout=300)
    if put.status_code not in (200, 201):
        raise UpstreamError(f"OSS PUT 失败 HTTP {put.status_code}: {put.text[:120]}")
    return build_entry(kind, filename, content_type, sts)
