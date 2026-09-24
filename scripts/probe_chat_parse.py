#!/usr/bin/env python3
"""chat（t2t）上游取证探针 —— 单发、不重试、不打印凭据；须显式 `--confirm` 才真发。

用途（对应 docs/UPSTREAM.md 未证实项）：
  · `--kind text`     ：抓 t2t 流式响应的**原始 SSE 事件形态**（回填 U-12）；
  · `--kind image|document|audio|video`：带 `files[]` 的多模态解析（图片/文件/音频/视频）——
    验证外链 URL 是否被上游接受、files 条目形状是否正确、`chat_type` 是否仍为 t2t。

纪律：
  · 每个进程**只发一发** completions（写端点勿连打）；失败即停，原样打印上游错误；
  · signin 走 `QWEN_SIGNIN_PROXY` 轮换出口（本脚本直接用 AccountPool，不经服务）；
  · 请求体与响应原文只在本地落档（`var/probe/<时间戳>_<kind>/`），凭据绝不落盘/打印。
"""
from __future__ import annotations

import argparse
import json
import sys
import time
from pathlib import Path

import httpx

REPO_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(REPO_ROOT))

from app.config import Settings  # noqa: E402
from app.upstream.qwen.accounts import AccountPool  # noqa: E402
from app.upstream.qwen.client import CHAT_FEATURE_CONFIG  # noqa: E402

#: 上游域内样例图（i2v 抓包同款资源，§4.2/§7.1 实证过的 OSS 地址）
UPSTREAM_IMAGE = "https://qwen-chat.oss-ap-southeast-1.aliyuncs.com/resources/i2v/1762498392.png"

PROMPTS = {
    "text": "只回答两个字：你好",
    "image": (UPSTREAM_IMAGE, "这张图片里是什么？用一句话回答"),
    "document": ("https://arxiv.org/pdf/1706.03762", "这份文档的标题是什么？用一句话回答"),
    "audio": ("https://www2.cs.uic.edu/~i101/SoundFiles/StarWars60.wav", "这段音频是什么？用一句话回答"),
    "video": ("https://www.w3schools.com/html/mov_bbb.mp4", "这段视频里是什么？用一句话回答"),
    "tools": "北京今天天气怎么样？必须调用提供的工具查询，不要直接回答。",
    "tools_no_search": "北京今天天气怎么样？必须调用提供的 get_weather 工具查询，不要用搜索，不要直接回答。",
}

#: OpenAI 风格 function 定义（探针就是要验证上游认不认这套）
TOOLS_DEF = [{
    "type": "function",
    "function": {
        "name": "get_weather",
        "description": "查询指定城市的实时天气",
        "parameters": {
            "type": "object",
            "properties": {"city": {"type": "string", "description": "城市名"}},
            "required": ["city"],
        },
    },
}]

#: files 条目形状（image = §4.2 抓包逐字；document/audio/video = 同构推测，探针就是要验证它）
def file_entry(kind: str, url: str) -> dict:
    meta = {
        "image": ("sample.png", "image/png", "image", "vision"),
        "document": ("paper.pdf", "application/pdf", "document", "document"),
        "audio": ("clip.wav", "audio/wav", "audio", "audio"),
        "video": ("clip.mp4", "video/mp4", "video", "video"),
    }[kind]
    name, file_type, show_type, file_class = meta
    return {"type": show_type, "name": name, "file_type": file_type,
            "showType": show_type, "status": "uploaded", "file_class": file_class,
            "url": url}


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("--kind",
                        choices=("text", "image", "document", "audio", "video",
                                 "tools", "tools_no_search"),
                        default="text")
    parser.add_argument("--model", default="qwen3.7-plus")
    parser.add_argument("--account", default="")
    parser.add_argument("--confirm", action="store_true", help="不加此参数绝不真发")
    args = parser.parse_args()
    if not args.confirm:
        print("[probe] 干跑：未加 --confirm，不发任何请求。请求形状已备好（见代码内形状表）。")
        return 0

    settings = Settings.from_env()
    if not settings.accounts:
        print("[probe] FAIL: 没有账号（QWEN_ACCOUNTS 未设置）")
        return 2
    email = args.account or next(iter(settings.accounts))
    pool = AccountPool(settings)
    token = pool.token_for(email)
    print(f"[probe] token 就绪 account={email.split('@')[0]}*** len={len(token)}")

    out_dir = REPO_ROOT / "var" / "probe" / f"{time.strftime('%Y%m%d_%H%M%S')}_{args.kind}"
    out_dir.mkdir(parents=True, exist_ok=True)

    # 🔴 头必须完整（UPSTREAM §2.4：探针头不全 = WAF 挑战页/非 JSON 的根因）——
    #    chats/new 与 completions 都复用 QwenClient.headers() 逐字段对齐的头集。
    from app.upstream.qwen.client import QwenClient

    qwc = QwenClient(settings)
    try:
        with httpx.Client(base_url=settings.base_url, timeout=120, trust_env=False) as client:
            # ① 建会话（免费段）
            resp = client.post("/api/v2/chats/new", json={
                "title": "New Chat", "models": [args.model], "chat_mode": "normal",
                "chat_type": "t2t", "timestamp": int(time.time() * 1000), "project_id": "",
            }, headers=qwc.headers(token))
            chat_id = ""
            try:
                chat_id = str((resp.json().get("data") or {}).get("id") or "")
            except ValueError:
                pass
            if not chat_id:
                print(f"[probe] FAIL chats/new HTTP {resp.status_code}: {resp.text[:200]}")
                return 1
            print(f"[probe] chats/new ok chat_id={chat_id}")

            # ② 单发 completions（stream:true，抓原始 SSE）
            if args.kind in ("text", "tools", "tools_no_search"):
                files, prompt = [], PROMPTS[args.kind]
            else:
                url, prompt = PROMPTS[args.kind]
                files = [file_entry(args.kind, url)]
            message = {
                "id": None, "fid": str(__import__("uuid").uuid4()), "parentId": None,
                "childrenIds": [str(__import__("uuid").uuid4())],
                "role": "user", "content": prompt, "user_action": "chat",
                "files": files, "timestamp": int(time.time()),
                "models": [args.model], "model": "", "chat_type": "t2t",
                "feature_config": dict(CHAT_FEATURE_CONFIG),
                "extra": {"meta": {"subChatType": "t2t"}},
                "sub_chat_type": "t2t", "parent_id": None,
            }
            body = {"stream": True, "version": "2.1", "incremental_output": True,
                    "chatId": chat_id, "parentId": "", "chat_id": chat_id,
                    "chat_mode": "normal", "model": args.model, "parent_id": None,
                    "messages": [message], "timestamp": int(time.time())}
            if args.kind == "tools" or args.kind == "tools_no_search":
                body["tools"] = TOOLS_DEF
                body["tool_choice"] = "auto"
            if args.kind == "tools_no_search":
                # 对照实验：关掉内置搜索，排除"auto_search 抢活"的干扰项
                body["messages"][0]["feature_config"]["auto_search"] = False
            (out_dir / "request.json").write_text(
                json.dumps(body, ensure_ascii=False, indent=2), encoding="utf-8")

            started = time.time()
            with client.stream("POST", "/api/v2/chat/completions",
                               params={"chat_id": chat_id}, json=body,
                               headers=qwc.headers(token, referer=f"{settings.base_url}/c/{chat_id}")) as resp:
                print(f"[probe] completions HTTP {resp.status_code} "
                      f"content-type={resp.headers.get('content-type')}")
                lines: list[str] = []
                for line in resp.iter_lines():
                    lines.append(line)
            elapsed = time.time() - started
            (out_dir / "response.sse").write_text("\n".join(lines), encoding="utf-8")

            print(f"[probe] 收到 {len(lines)} 行，耗时 {elapsed:.1f}s，原文落档 {out_dir}/response.sse")
            head = [ln for ln in lines if ln.strip()][:6]
            tail = [ln for ln in lines if ln.strip()][-3:]
            print("[probe] --- 头部原文 ---")
            for ln in head:
                print("   ", ln[:260])
            print("[probe] --- 尾部原文 ---")
            for ln in tail:
                print("   ", ln[:260])
    finally:
        qwc.close()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
