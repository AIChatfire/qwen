"""qwen 账号 signin（铸造 token JWT）—— **出口只走 HTTP(S) 代理**。

纪律（全部有实测教训）：
  · `POST /api/v2/auths/signin`，body `{"email", "password": sha256hex(password)}`；
    token **只在 `Set-Cookie`**（body 是账号记录，没有 token）；
  · **signin 有 IP 级频率墙**：同一出口几秒内连登多个账号会拿到 `aliyun_waf` 挑战页
    ⇒ 必须走**轮换出口**；直连登录是"把出口打进墙"的标准姿势；
  · 先 `GET /auth` 预热（best-effort），把 WAF 冷启动 cookie 种进会话；
  · 请求需带浏览器特征头（UA + `sec-ch-ua*`），否则同样吃挑战页 —— 单变量实测过。

**为什么是 HTTP 代理**（2026-09-22 对池的实测，`pool:2086`）：

| 语义 | 实测 | 为什么正好合适 |
|---|---|---|
| 每连接换出口 IP | 三次新 client = `.96 / .97 / .98` | **每次铸造换一个 IP** ⇒ 不撞登录 IP 墙 |
| 同连接复用同 IP | 同 client 两次请求 = 同一 IP | 一次铸造的「预热 + 登录」**全程一个 IP**（自洽）|

⇒ HTTP 形态同时满足"每次登录换 IP"与"一次登录内部不跳"。
对比此前自研的 SOCKS5 拨号器：它一次铸造开**两条连接**（预热 + 登录），
用"每连接换 IP"的池时那两条**可能落在两个 IP**。
故已按用户决策（2026-09-22：**不做兼容**）**删除 SOCKS 分支**，少约 130 行自研 socket/TLS/HTTP 解析。

移植自 `image-adapter/tools/token_service.py`（生产验证版，仅新增浏览器头与可注入 UA / transport）。
"""
from __future__ import annotations

import hashlib
import json
import re
import urllib.parse

import httpx

SIGNIN_PATH = "/api/v2/auths/signin"
WARM_PATH = "/auth"


class MintError(RuntimeError):
    """signin 未能产出 token（网络 / 出口 / 被拒）。"""


class WallError(MintError):
    """出口被打进了 WAF 挑战页。"""


def browser_hint_headers(user_agent: str) -> dict[str, str]:
    return {
        "Accept": "application/json, text/plain, */*",
        "Accept-Language": "zh-CN,zh;q=0.9",
        "User-Agent": user_agent,
        "sec-ch-ua": '"Chromium";v="152", "Not?A_Brand";v="24", "Google Chrome";v="152"',
        "sec-ch-ua-mobile": "?0",
        "sec-ch-ua-platform": '"macOS"',
        "Sec-Fetch-Dest": "empty",
        "Sec-Fetch-Mode": "cors",
        "Sec-Fetch-Site": "same-origin",
    }


# ------------------------------------------------------------------ token 铸造


def extract_token_from_setcookie(set_cookies: list[str]) -> str:
    for raw in set_cookies:
        found = re.search(r"(?:^|[;\s])token=([^;]+)", raw)
        if found:
            return found.group(1).strip()
    return ""


def mint_token(proxy_url: str, email: str, password: str, *,
               timeout: float = 40.0, base: str = "https://chat.qwen.ai",
               user_agent: str = "",
               transport: httpx.BaseTransport | None = None) -> str:
    """经**轮换出口**（HTTP(S) 代理）登录一个账号，返回 token JWT。失败抛 `MintError` / `WallError`。

    🔴 **每次调用新建一个 client** = 新连接 = **新出口 IP**（池按连接轮换）；
    而**同一个 client 内**做「预热 + 登录」，靠隧道复用保证这一次铸造**全程同一个 IP**。
    这两个语义都有实测支撑（见模块 docstring 的表）—— 别合并成一个长生命周期的 client，
    那会让多次登录共用一个出口，正好撞上 IP 级登录墙。

    `transport` 仅测试注入：httpx 的 `proxy=` 会以 mounts **覆盖**自定义 transport（二者不能共存），
    所以给了 transport 就**不走代理** —— 生产路径永远只在 `proxy=None` 时发生。
    """
    raw_url = proxy_url if "://" in proxy_url else f"http://{proxy_url}"
    scheme = urllib.parse.urlsplit(raw_url).scheme.lower()
    if scheme not in ("http", "https"):
        raise MintError(
            f"signin 出口只收 http(s):// 代理，收到 {scheme!r}（SOCKS 分支已移除）")

    digest = hashlib.sha256(password.encode()).hexdigest()
    common = {**browser_hint_headers(user_agent or _default_ua()),
              "Origin": base, "Referer": base + WARM_PATH}
    body = json.dumps({"email": email, "password": digest}).encode()

    kwargs: dict = {"timeout": timeout, "trust_env": False}
    if transport is not None:      # 测试注入：不走代理（见 docstring）
        kwargs["transport"] = transport
    else:
        kwargs["proxy"] = raw_url

    with httpx.Client(**kwargs) as client:
        try:  # 预热：拿 WAF 冷启动 cookie（best-effort，失败不阻断）
            client.get(f"{base}{WARM_PATH}", headers=common)
        except httpx.HTTPError:
            pass
        try:
            resp = client.post(f"{base}{SIGNIN_PATH}", content=body,
                               headers={**common, "Content-Type": "application/json"})
        except httpx.HTTPError as exc:
            raise MintError(f"signin 传输失败：{type(exc).__name__}: {exc}") from exc

    text = resp.content.decode("utf-8", "replace")
    if "aliyun_waf" in text:
        raise WallError("this egress is answering the WAF challenge page")
    if resp.status_code != 200:
        raise MintError(f"signin answered HTTP {resp.status_code}")
    token = extract_token_from_setcookie(resp.headers.get_list("set-cookie"))
    if not token:
        raise MintError("no token in Set-Cookie")
    return token


def _default_ua() -> str:
    from ...config import UA_DEFAULT

    return UA_DEFAULT
