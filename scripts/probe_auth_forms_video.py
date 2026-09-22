#!/usr/bin/env python3
"""鉴权形态 × 视频（t2v）矩阵 —— 四形态构造**照图片侧口径**（`reverse-proxy/qwen/probe/probe_auth_forms_matrix.py`）。

| 形态 | 构造（与图片侧逐字段一致） | `chat_mode` |
|---|---|---|
| `cookie` | 完整浏览器指纹头 + `Cookie: token=<JWT>`（**只带 token**，其余 cookie 一律不带） | `normal` |
| `bearer` | 完整浏览器指纹头 + `Authorization: Bearer <JWT>`（**一个 cookie 都不带**） | `normal` |
| `guest` | 身份池三件套：身份 cookie + `bx-ua` + `bx-umidtoken` + `bx-v`（无 `token`） | `guest` |
| `anon` | 完整指纹头但**无任何凭据**（无 Cookie / Authorization / `bx-*`） | `normal` |

每形态两段（图片侧的判据结构，逐条搬来）：
  ① **免费段** `POST /api/v2/chats/new`（`chat_type=t2v`）—— 判「能不能建立视频会话」；
  ② **写端点** `POST /api/v2/chat/completions`（`stream:false`）—— 真正的鉴权门在这里：
     图片侧单变量实测显示 `bearer` 能过 ①、却在 ② 被拒（`qwen-chat-api.md` §2.13 第 2 条）。

## 纪律（照图片侧的踩坑清单，逐条落实）
- **凭据不进仓库**：账号密码从 `.env` 读（本仓 `.env` 已 gitignored），命令行只传邮箱；
  输出里只打印**掩码邮箱**与 JWT 账号 id，绝不回显密码/token 原文。
- **先跑 `cookie` 格**：它是唯一会消耗额度的格，必须在账号「干净」时跑；`bearer` 格与它之间**强制冷却**
  （默认 90s，图片侧教训：12s 内两次写请求即触发 x5sec 突发）。
- **命中 RGV587 立即停手**：不重试、不改载荷、不换账号（重试只会延长封锁）。
- **归属自证**：产物 URL 的 `/output/<uuid>/` 段应等于 token 的账号 id（图片侧踩过「图记到别人头上」）。
- **产物验真**：下载 + 容器 `mvhd` 时长核验（只看「流里出现 URL」会把死链当成功）。
- **⚠️ 与图片侧的口径偏差（如实标注）**：图片侧要求「一格一号」，本探针只有**一个账号** ⇒
  `cookie` 与 `bearer` 两格落在同一个号上（用冷却降低频控风险）。要严格版请给 6 个号。

## 用法
    # 只跑免费段（零消耗）：看四种形态谁能建立 t2v 会话
    python scripts/probe_auth_forms_video.py --email 2xx***@mail.xiuvi.cn
    # 全矩阵（cookie 格真发 1 发，消耗该号 1 次视频额度）
    python scripts/probe_auth_forms_video.py --email 2xx***@mail.xiuvi.cn \
        --pool /tmp/guest_ident_fresh.json --confirm --cooldown 90
"""
from __future__ import annotations

import argparse
import base64
import json
import pathlib
import re
import sys
import time
import uuid

import httpx

REPO_ROOT = pathlib.Path(__file__).resolve().parent.parent
sys.path.insert(0, str(REPO_ROOT))
sys.path.insert(0, str(pathlib.Path(__file__).resolve().parent))

from live_smoke import mp4_duration_seconds  # noqa: E402

from app.upstream.qwen.client import FEATURE_CONFIG  # noqa: E402
from app.upstream.qwen.signin import mint_token  # noqa: E402

BASE = "https://chat.qwen.ai"
API = f"{BASE}/api/v2"
UA = ("Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 "
      "(KHTML, like Gecko) Chrome/152.0.0.0 Safari/537.36")
RATIO = "16:9"
PROMPT = "一只橘猫坐在窗台上看雨，窗外霓虹灯光映在玻璃上，电影感镜头缓慢推进"
OWNER_RE = re.compile(r"/output/([0-9a-zA-Z\-]+)/")
_WEEKDAYS = ("Mon", "Tue", "Wed", "Thu", "Fri", "Sat", "Sun")
_MONTHS = ("Jan", "Feb", "Mar", "Apr", "May", "Jun", "Jul", "Aug", "Sep", "Oct", "Nov", "Dec")

ACCOUNT_FORMS = ("cookie", "bearer")
ALL_FORMS = ("cookie", "bearer", "guest", "anon")


def tz_header() -> str:
    now = time.gmtime()
    return (f"{_WEEKDAYS[now.tm_wday]} {_MONTHS[now.tm_mon - 1]} {now.tm_mday:02d} "
            f"{now.tm_year} {now.tm_hour:02d}:{now.tm_min:02d}:{now.tm_sec:02d} GMT+0800")


def _base_headers(referer: str) -> dict:
    """完整浏览器指纹（逐字段对齐 `http_t2i.headers` / `biz-api::build_headers`）——不含凭据。"""
    return {
        "Accept": "application/json",
        "Content-Type": "application/json",
        "User-Agent": UA,
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
    }


def anon_headers(referer: str) -> dict:
    """**完全未登录**：保留浏览器一致的指纹头，但**不带任何凭据**。"""
    return _base_headers(referer)


def account_headers(token: str, referer: str, form: str) -> dict:
    """`cookie` 只给 token；`bearer` 一个 cookie 都不带（与图片侧逐字一致）。"""
    headers = _base_headers(referer)
    if form == "cookie":
        headers["Cookie"] = f"token={token}"
    else:
        headers["Authorization"] = f"Bearer {token}"
    return headers


def guest_headers(ident: dict, referer: str) -> dict:
    """guest 三件套（身份 cookie 无 token + bx-ua + bx-umidtoken + bx-v）。"""
    headers = _base_headers(referer)
    headers.update({"Cookie": ident["cookie"], "bx-v": "2.5.37",
                    "bx-ua": ident["bx_ua"], "bx-umidtoken": ident["bx_umidtoken"]})
    return headers


def chat_mode_for(form: str) -> str:
    return "guest" if form == "guest" else "normal"


# ---------------------------------------------------------------- 输出与判读


def mask(email: str) -> str:
    local, _, domain = email.partition("@")
    return f"{local[:3]}***@{domain}" if domain else f"{local[:3]}***"


def account_id(token: str) -> str:
    """从 JWT 载荷取账号 id（不验签，仅用于核对归属）。"""
    try:
        payload = token.split(".")[1]
        payload += "=" * (-len(payload) % 4)
        return str(json.loads(base64.urlsafe_b64decode(payload)).get("id", ""))
    except Exception:  # noqa: BLE001
        return "?"


def describe(resp: httpx.Response) -> dict:
    actual = resp.headers.get("x-actual-status-code", "-")
    try:
        payload = resp.json()
    except ValueError:
        return {"http": resp.status_code, "actual": actual, "code": "", "details": "",
                "raw": resp.text[:200], "json": None}
    data = payload.get("data") if isinstance(payload.get("data"), dict) else {}
    return {"http": resp.status_code, "actual": actual,
            "code": str(data.get("code") or payload.get("code") or ""),
            "details": str(data.get("details") or data.get("message") or "")[:200],
            "ret": payload.get("ret"), "json": payload}


def classify(rec: dict) -> str:
    blob = json.dumps(rec, ensure_ascii=False).lower()
    if "rgv587" in blob or "fail_sys" in blob:
        return "RGV587 风控（凭据形态/指纹被拒 —— 与载荷无关）"
    if not rec.get("json"):
        return "非 JSON（可能 WAF 挑战页）"
    if rec.get("json", {}).get("success") is False:
        code = rec.get("code") or "?"
        if code == "Unauthorized" or rec.get("actual") == "401":
            return "未授权（Unauthorized）"
        return f"业务拒绝 code={code}"
    return "成功"


def read_env(path: pathlib.Path, keys: tuple[str, ...]) -> dict:
    out: dict[str, str] = {}
    for line in path.read_text(encoding="utf-8").splitlines():
        line = line.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        key, _, value = line.partition("=")
        if key.strip() in keys:
            out[key.strip()] = value.strip()
    return out


def password_for(env: dict, email: str) -> str:
    for entry in env.get("QWEN_ACCOUNTS", "").split(","):
        entry = entry.strip()
        if not entry or entry.startswith("#"):
            continue
        addr, sep, pw = entry.partition(":")
        if addr.strip() == email and sep:
            return pw.strip()
    return env.get("QWEN_ACCOUNT_PASSWORD", "")


# ---------------------------------------------------------------- 两段请求


def new_chat(client: httpx.Client, headers: dict, mode: str, model: str) -> tuple[str, dict]:
    body = {"title": "New Chat", "models": [model], "chat_mode": mode,
            "chat_type": "t2v", "timestamp": int(time.time() * 1000), "project_id": ""}
    resp = client.post(f"{API}/chats/new", json=body,
                       headers={**headers, "Referer": f"{BASE}/c/new-chat"})
    rec = describe(resp)
    chat_id = ""
    if rec.get("json") and rec["json"].get("success"):
        chat_id = str((rec["json"].get("data") or {}).get("id") or "")
    return chat_id, rec


def video_body(chat_id: str, mode: str, model: str) -> dict:
    now = int(time.time())
    message = {
        "id": None, "fid": str(uuid.uuid4()), "parentId": None,
        "childrenIds": [str(uuid.uuid4())], "role": "user", "content": PROMPT,
        "user_action": "chat", "timestamp": now, "models": [model], "model": "",
        "chat_type": "t2v", "feature_config": dict(FEATURE_CONFIG),
        "extra": {"meta": {"subChatType": "t2v", "size": RATIO}},
        "sub_chat_type": "t2v", "parent_id": None,
    }
    return {"stream": False, "version": "2.1", "incremental_output": True,
            "chatId": chat_id, "parentId": "", "chat_id": chat_id, "chat_mode": mode,
            "model": model, "parent_id": None, "messages": [message],
            "timestamp": now, "size": RATIO}


def submit(client: httpx.Client, headers: dict, chat_id: str, mode: str,
           model: str) -> tuple[str, dict]:
    resp = client.post(f"{API}/chat/completions", params={"chat_id": chat_id},
                       json=video_body(chat_id, mode, model),
                       headers={**headers, "Referer": f"{BASE}/c/{chat_id}"})
    rec = describe(resp)
    task_id = ""
    payload = rec.get("json") or {}
    messages = ((payload.get("data") or {}) or {}).get("messages") or []
    if messages and isinstance(messages[0], dict):
        task_id = str(((messages[0].get("extra") or {}).get("wanx") or {})
                       .get("task_id") or "")
    return task_id, rec


def poll(client: httpx.Client, headers: dict, task_id: str, tries: int,
         interval: float) -> tuple[str, str]:
    """回查直到终态；返回 (status, video_url)。只读，不消耗额度。"""
    url = ""
    status = "?"
    for _ in range(max(tries, 1)):
        time.sleep(interval)
        resp = client.get(f"{API}/task/status/{task_id}", headers=headers)
        data = (describe(resp).get("json") or {}).get("data") or {}
        status = str(data.get("task_status") or "?")
        if status == "success":
            url = str(data.get("content") or "")
            break
    return status, url


# ---------------------------------------------------------------- 主流程


def main() -> int:
    ap = argparse.ArgumentParser(description="鉴权形态 × 视频（t2v）矩阵")
    ap.add_argument("--email", required=True, help="账号邮箱（密码从 .env 读，不上命令行）")
    ap.add_argument("--env", default=str(REPO_ROOT / ".env"))
    ap.add_argument("--pool", help="guest 身份池 JSON；不给则只跑需要账号的格")
    ap.add_argument("--forms", default=",".join(ALL_FORMS))
    ap.add_argument("--confirm", action="store_true", help="真发写请求（否则只跑免费段）")
    ap.add_argument("--cooldown", type=float, default=90.0, help="账号两格之间的冷却秒数")
    ap.add_argument("--poll", type=int, default=15)
    ap.add_argument("--poll-interval", type=float, default=20.0)
    ap.add_argument("--out-dir", default=str(REPO_ROOT / "var" / "probe"))
    args = ap.parse_args()

    env = read_env(pathlib.Path(args.env), ("QWEN_ACCOUNTS", "QWEN_ACCOUNT_PASSWORD",
                                            "QWEN_SIGNIN_PROXY", "QWEN_CHAT_MODEL"))
    model = env.get("QWEN_CHAT_MODEL") or "qwen3.7-plus"
    password = password_for(env, args.email)
    if not password:
        print(f"❌ {mask(args.email)} 的密码在 .env 里找不到（QWEN_ACCOUNTS / QWEN_ACCOUNT_PASSWORD）")
        return 2
    print(f"账号 {mask(args.email)} | 模型 {model} | 形态 {args.forms} | "
          f"{'真发' if args.confirm else '仅免费段'}")

    forms = [f.strip() for f in args.forms.split(",") if f.strip()]
    client = httpx.Client(timeout=120.0, trust_env=False, follow_redirects=True)
    results: dict[str, dict] = {}
    token = ""

    # 账号 token 只铸一次（两个账号格共用）；登录走轮换出口
    if any(f in ACCOUNT_FORMS for f in forms):
        egress = env.get("QWEN_SIGNIN_PROXY", "")
        if not egress:
            print("❌ .env 缺 QWEN_SIGNIN_PROXY —— 铸造 token 必须走轮换出口")
            return 2
        print("── 铸 token（经轮换出口，免费）…")
        token = mint_token(egress, args.email, password, base=BASE, user_agent=UA)
        print(f"   ✅ token_len={len(token)} | 账号 id={account_id(token)[:8]}…")

    last_account_write = 0.0
    stop_account_writes = False
    for form in forms:
        row: dict = {"form": form, "chat_mode": chat_mode_for(form)}
        if form in ACCOUNT_FORMS and stop_account_writes:
            print(f"\n── [{form}] ⏭ 跳过：账号写请求已因风控停止")
            results[form] = {**row, "verdict": "⏭ 跳过（账号写请求因风控停止）"}
            continue
        print(f"\n── [{form}] ① 免费段 chats/new（chat_type=t2v）")
        if form in ACCOUNT_FORMS:
            headers = account_headers(token, f"{BASE}/", form)
        elif form == "guest":
            if not args.pool:
                print("   跳过：未给 --pool")
                results[form] = {**row, "skipped": "no pool"}
                continue
            pool = json.loads(pathlib.Path(args.pool).read_text(encoding="utf-8"))
            ident = pool[0]
            headers = guest_headers(ident, f"{BASE}/")
        else:
            headers = anon_headers(f"{BASE}/")

        chat_id, rec1 = new_chat(client, headers, row["chat_mode"], model)
        row["chats_new"] = rec1
        print(f"    {rec1['http']} | x-actual={rec1['actual']} | "
              f"success={(rec1.get('json') or {}).get('success')} | "
              f"code={rec1['code']!r} | {classify(rec1)}")

        if not chat_id:
            row["verdict"] = "❌ 会话都建不了：" + classify(rec1)
            results[form] = row
            continue

        if not args.confirm:
            row["verdict"] = "（未给 --confirm ⇒ 未发写请求）"
            results[form] = row
            continue

        if form in ACCOUNT_FORMS:
            gap = time.time() - last_account_write
            if last_account_write and gap < args.cooldown:
                wait = args.cooldown - gap
                print(f"    冷却 {wait:.0f}s（同号两格之间强制间隔，防 x5sec 突发）…")
                time.sleep(wait)

        print("    ── ② 写端点 chat/completions（stream=false）")
        task_id, rec2 = submit(client, headers, chat_id, row["chat_mode"], model)
        row["submit"] = rec2
        if form in ACCOUNT_FORMS:
            last_account_write = time.time()
        print(f"    {rec2['http']} | x-actual={rec2['actual']} | "
              f"code={rec2['code']!r} | details={rec2['details']!r}")
        if "风控" in classify(rec2):
            row["verdict"] = "🔴 " + classify(rec2) + " —— 停手，不重试"
            results[form] = row
            stop_account_writes = True
            print("    ⇒ 命中风控：**停止后续所有账号写请求**（重试只会延长封锁）；"
                  "guest / anon 两格不含账号凭据，继续跑")
            continue
        if not task_id:
            row["verdict"] = "❌ " + classify(rec2)
            results[form] = row
            continue

        row["task_id"] = task_id[:8] + "…"
        row["verdict"] = "✅ 上游受理（task_id 已回）"
        print(f"    ✅ task_id={task_id[:8]}… ⇒ 回查（只读）")
        if form == "cookie":
            status, url = poll(client, headers, task_id, args.poll, args.poll_interval)
            row["poll_status"] = status
            row["video_url"] = url
            if url:
                owner = (OWNER_RE.search(url) or [None, "?"])[1]
                row["resource_user_id"] = owner[:8] + "…"
                row["owner_matches_token"] = owner == account_id(token)
                print(f"    产物：{url[:96]}…")
                print(f"    归属自证：resource_user_id={owner[:8]}… | "
                      f"token 账号 id={account_id(token)[:8]}… ⇒ "
                      f"{'✅ 一致' if row['owner_matches_token'] else '🔴 不一致'}")
                blob = client.get(url).content
                tmp = pathlib.Path(args.out_dir) / f"authmatrix_{form}.mp4"
                tmp.parent.mkdir(parents=True, exist_ok=True)
                tmp.write_bytes(blob)
                duration = mp4_duration_seconds(tmp)
                row["bytes"] = len(blob)
                row["duration_s"] = duration
                print(f"    产物验真：{len(blob)} bytes | 容器时长={duration}s | 落盘 {tmp}")
            else:
                print(f"    回查未到 success（status={status}）")
        results[form] = row

    # ---- 汇总
    print("\n================ 矩阵结果 ================")
    print(f"{'形态':<8}{'①会话':<12}{'②写端点':<14}判决")
    for form in forms:
        row = results.get(form, {})
        c1 = row.get("chats_new") or {}
        c2 = row.get("submit") or {}
        print(f"{form:<8}{str(c1.get('actual', '-')):<12}{str(c2.get('actual', '-')):<14}"
              f"{row.get('verdict', '?')}")

    stamp = time.strftime("%Y%m%dT%H%M%SZ", time.gmtime())
    out = pathlib.Path(args.out_dir) / f"auth_forms_video_{stamp}.json"
    out.parent.mkdir(parents=True, exist_ok=True)
    out.write_text(json.dumps({"email": mask(args.email), "model": model,
                               "confirmed": args.confirm, "results": results},
                              ensure_ascii=False, indent=1), encoding="utf-8")
    print(f"\n原始记录：{out}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
