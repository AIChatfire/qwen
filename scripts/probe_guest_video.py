#!/usr/bin/env python3
"""guest（匿名访客身份）能否提交**视频**任务 —— 单发判定探针。

背景：`docs/UPSTREAM.md` §9.1/U-9 —— guest 通路在**图片/文本**面已证可用（4 鉴权 × 5 形态矩阵
guest 5/5），但**视频面零证据**。本探针用**恰好一发**真实提交给出判决。

## 为什么不复用本服务的 client
本服务的 `QwenClient` 凭据恒为 `Cookie: token=<JWT>` + `chat_mode: "normal"`（账号门）。
guest 是**另一扇门**：cookie 无 `token`，靠**设备指纹三件套**（身份 cookie + `bx-ua` + `bx-umidtoken`），
且 `chat_mode` 与 `Referer` 都要切到 guest。两扇门不能混。

## 请求形态依据（逐条有出处）
- guest 门字段：`reference: reverse-proxy/qwen/probe/guest_t2i.py`（已验证可批量出图的 guest 工具）
  —— cookie / `chat_mode:"guest"` / `bx-*` 头 / Referer 取法照抄该工具。
- 视频体（t2v 三处同标 + `stream:false` + `size`）：本服务 `app/upstream/qwen/client.py`
  （2026-09-22 真实出片过 5.042s 的同一份体）。
- `version: 0.2.0`：**发**。guest 抓包曾缺该头（§2.6 冲突未定论），但已验证的 guest 工具
  `guest_t2i.py` 是发的 ⇒ 取"已验证"一侧。

## 纪律（写死在代码里，不靠人记）
1. **单发**：一次运行只发**一发** `chat/completions`；命中任何写类拒绝都**不重试、不换身份**；
2. **不打印凭据**：cookie / bx 串只输出长度与指纹前缀；
3. 必须显式 `--confirm` 才真发（默认只做免费段会话测试）。
4. `chats/new` 是**免费端点**，可安全用于"这身份还活着吗"的前置判定。

## 用法
    # 仅免费段（判断身份有效性 + guest 门是否接受 t2v 会话）
    python scripts/probe_guest_video.py --pool /tmp/guest_ident_fresh.json
    # 真发一发（会消耗该 guest 身份的一次额度，若能力存在）
    python scripts/probe_guest_video.py --pool /tmp/guest_ident_fresh.json --confirm
"""
from __future__ import annotations

import argparse
import hashlib
import json
import logging
import pathlib
import sys
import time
import uuid

import httpx

BASE = "https://chat.qwen.ai"
API = f"{BASE}/api/v2"
CHAT_MODEL = "qwen3.7-plus"

#: 与 `app/ark.py` 一致：上游只吃这 5 个比例；探针用最大众的 16:9。
RATIO = "16:9"
PROMPT = "一只橘猫坐在窗台上看雨"

#: 与 `app/upstream/qwen/client.py::FEATURE_CONFIG` 逐字段一致（抓包固定片段）。
FEATURE_CONFIG = {
    "thinking_enabled": False,
    "output_schema": "phase",
    "research_mode": "normal",
    "auto_thinking": False,
    "thinking_mode": "Fast",
    "auto_search": True,
}

_WEEKDAYS = ("Mon", "Tue", "Wed", "Thu", "Fri", "Sat", "Sun")
_MONTHS = ("Jan", "Feb", "Mar", "Apr", "May", "Jun", "Jul", "Aug", "Sep", "Oct", "Nov", "Dec")


def tz_header() -> str:
    now = time.gmtime()
    return (f"{_WEEKDAYS[now.tm_wday]} {_MONTHS[now.tm_mon - 1]} {now.tm_mday:02d} "
            f"{now.tm_year} {now.tm_hour:02d}:{now.tm_min:02d}:{now.tm_sec:02d} GMT+0800")


def ua_for(ident: dict) -> str:
    """按该身份**生成时的 Chrome 版本**构造 UA（身份池 meta 有记录，避免全局硬编码漂移）。"""
    version = str((ident.get("meta") or {}).get("ua_version") or "152.0.0.0")
    return ("Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 "
            f"(KHTML, like Gecko) Chrome/{version} Safari/537.36")


def headers(ident: dict, referer: str) -> dict:
    """完整浏览器指纹 + guest 三件套（逐字段对齐 guest_t2i.py / biz-api::build_headers）。"""
    return {
        "Accept": "application/json",
        "Content-Type": "application/json",
        "User-Agent": ua_for(ident),
        "Origin": BASE,
        "Referer": referer,
        "source": "web",
        "version": "0.2.0",
        "Accept-Language": "zh-CN,zh;q=0.9",
        "Connection": "keep-alive",
        "Sec-Fetch-Dest": "empty",
        "Sec-Fetch-Mode": "cors",
        "Sec-Fetch-Site": "same-origin",
        "Timezone": tz_header(),
        "X-Request-Id": str(uuid.uuid4()),
        "X-Accel-Buffering": "no",
        "sec-ch-ua": '"Chromium";v="152", "Not?A_Brand";v="24", "Google Chrome";v="152"',
        "sec-ch-ua-mobile": "?0",
        "sec-ch-ua-platform": '"macOS"',
        "Cookie": ident["cookie"],
        "bx-v": "2.5.37",
        "bx-ua": ident["bx_ua"],
        "bx-umidtoken": ident["bx_umidtoken"],
    }


def guest_chat_body(model: str, chat_type: str) -> dict:
    """`chats/new` 体：guest 门的 chat_type 用本次要生成的形态（视频探针用 t2v）。"""
    return {"title": "New Chat", "models": [model], "chat_mode": "guest",
            "chat_type": chat_type, "timestamp": int(time.time() * 1000), "project_id": ""}


def video_body(chat_id: str, chat_type: str) -> dict:
    """视频提交体 —— 与本服务已验证出片的那份**同构**，只把 `chat_mode` 切到 guest。"""
    now = int(time.time())
    message = {
        "id": None,
        "fid": str(uuid.uuid4()),
        "parentId": None,
        "childrenIds": [str(uuid.uuid4())],
        "role": "user",
        "content": PROMPT,
        "user_action": "chat",
        "timestamp": now,
        "models": [CHAT_MODEL],
        "model": "",
        "chat_type": chat_type,
        "feature_config": dict(FEATURE_CONFIG),
        "extra": {"meta": {"subChatType": chat_type, "size": RATIO}},
        "sub_chat_type": chat_type,
        "parent_id": None,
    }
    return {
        "stream": False,
        "version": "2.1",
        "incremental_output": True,
        "chatId": chat_id,
        "parentId": "",
        "chat_id": chat_id,
        "chat_mode": "guest",
        "model": CHAT_MODEL,
        "parent_id": None,
        "messages": [message],
        "timestamp": now,
        "size": RATIO,
    }


def fingerprint(seed: str) -> str:
    return hashlib.sha256(seed.encode()).hexdigest()[:12]


def describe(resp: httpx.Response) -> str:
    """把响应压成一行可读判据（HTTP 码 + 真码头 + 业务码/文案）。"""
    head = resp.headers.get("x-actual-status-code", "-")
    try:
        payload = resp.json()
    except ValueError:
        return f"HTTP {resp.status_code} | x-actual={head} | 非 JSON：{resp.text[:160]!r}"
    data = payload.get("data") if isinstance(payload.get("data"), dict) else {}
    code = str(data.get("code") or payload.get("code") or "")
    details = str(data.get("details") or data.get("message") or payload.get("details") or "")
    ret = payload.get("ret")
    extra = f" | ret={ret}" if ret else ""
    return (f"HTTP {resp.status_code} | x-actual={head} | success={payload.get('success')}"
            f" | code={code!r} | details={details[:160]!r}{extra}")


def classify(text: str) -> str:
    """把响应文本映射成判决语（**只描述现象，不猜**）。"""
    low = text.lower()
    if "rgv587" in low or "fail_sys" in low:
        return "❌ 风控 RGV587 —— 身份/指纹或频率问题，**与视频能力无关**，停手等冷却"
    if "unauthorized" in low or "401" in low:
        return "❌ 未授权 —— 身份已失效或 guest 门不认，**与视频能力无关**"
    if "rateLimited".lower() in low or "额度" in text:
        return "⚠️ 额度类拒绝 —— **说明请求已被受理**（gate 过了），只是拿不到额度；看 details 是生图额度还是视频额度"
    if "task_id" in text or "wanx" in text:
        return "✅ 上游**受理了视频任务**并回了 task_id ⇒ guest × 视频**可用**（继续轮询看是否真出片）"
    return "❓ 形态未知，人工判读原文"


def main() -> int:
    logging.basicConfig(level=logging.INFO, format="%(message)s")
    ap = argparse.ArgumentParser(description="guest 身份能否提交视频任务 —— 单发判定")
    ap.add_argument("--pool", required=True, help="身份池 JSON（含 cookie/bx_ua/bx_umidtoken）")
    ap.add_argument("--index", type=int, default=0, help="用第几个身份（默认 0）")
    ap.add_argument("--chat-type", default="t2v", choices=["t2v", "i2v"])
    ap.add_argument("--confirm", action="store_true",
                    help="真发一发（默认只做免费段 chats/new；加此开关才会消耗额度）")
    ap.add_argument("--poll", type=int, default=6, help="提交成功后回查次数（只读；0=不查）")
    ap.add_argument("--poll-interval", type=float, default=20.0)
    args = ap.parse_args()

    pool = json.loads(pathlib.Path(args.pool).read_text(encoding="utf-8"))
    ident = pool[args.index]
    ident_id = fingerprint(ident["cookie"])
    logging.info("身份 #%s 指纹=%s | cookie 长度=%s | bx_ua 长度=%s | meta=%s",
                 args.index, ident_id, len(ident["cookie"]), len(ident.get("bx_ua", "")),
                 json.dumps(ident.get("meta") or {}, ensure_ascii=False)[:200])

    client = httpx.Client(timeout=90.0, trust_env=False)   # 🔴 直连：别让本地代理介入

    # ---------------- 第 1 段：免费端点（chats/new）—— 判"身份还活着吗 + guest 门收不收该形态"
    logging.info("── [免费段] POST /api/v2/chats/new（chat_type=%s, chat_mode=guest）", args.chat_type)
    r1 = client.post(f"{API}/chats/new", json=guest_chat_body(CHAT_MODEL, args.chat_type),
                     headers=headers(ident, f"{BASE}/c/new-chat"))
    logging.info("   %s", describe(r1))
    if not r1.headers.get("content-type", "").startswith("application/json"):
        logging.info("   非 JSON（可能 WAF 挑战页），片段：%r", r1.text[:200])
    payload1 = r1.json() if r1.headers.get("content-type", "").startswith("application/json") else {}
    if not payload1.get("success"):
        logging.info("   判决：%s", classify(r1.text[:400]))
        logging.info("   ⇒ 免费段即失败，**不发**提交（守住单发纪律）")
        return 1
    chat_id = str((payload1.get("data") or {}).get("id") or "")
    logging.info("   ✅ 会话已建：chat_id=%s（guest 门接受 chat_type=%s 的会话）",
                 chat_id[:8] + "…", args.chat_type)

    if not args.confirm:
        logging.info("── 未给 --confirm ⇒ 到此为止（未发任何生成请求）")
        return 0

    # ---------------- 第 2 段：**恰好一发**真提交
    logging.info("── [真发·仅此一发] POST /api/v2/chat/completions（stream=false）")
    r2 = client.post(f"{API}/chat/completions", params={"chat_id": chat_id},
                     json=video_body(chat_id, args.chat_type),
                     headers=headers(ident, f"{BASE}/c/{chat_id}"))
    logging.info("   %s", describe(r2))
    logging.info("   原始响应体（前 600 字符）：%s", r2.text[:600])
    logging.info("   判决：%s", classify(r2.text[:600]))

    task_id = ""
    try:
        data2 = (r2.json().get("data") or {})
        msgs = data2.get("messages") or []
        if msgs and isinstance(msgs[0], dict):
            task_id = str(((msgs[0].get("extra") or {}).get("wanx") or {}).get("task_id") or "")
    except ValueError:
        pass

    if not task_id:
        logging.info("── 未拿到 task_id ⇒ 到此为止")
        return 1

    logging.info("── 拿到 task_id=%s ⇒ 回查 %s 次（只读，不消耗额度）", task_id[:8] + "…", args.poll)
    for i in range(args.poll):
        time.sleep(args.poll_interval)
        r3 = client.get(f"{API}/task/status/{task_id}",
                        headers=headers(ident, f"{BASE}/"))
        logging.info("   [%s/%s] %s", i + 1, args.poll, describe(r3))
        if r3.text and ("success" in r3.text or "content" in r3.text):
            try:
                if (r3.json().get("data") or {}).get("task_status") == "success":
                    logging.info("   ✅ 出片：%s", (r3.json().get("data") or {}).get("content", "")[:200])
                    break
            except ValueError:
                pass
    return 0


if __name__ == "__main__":
    sys.exit(main())
