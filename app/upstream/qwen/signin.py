"""qwen 账号 signin（铸造 token JWT）。

纪律（全部有实测教训）：
  · `POST /api/v2/auths/signin`，body `{"email", "password": sha256hex(password)}`；
    token **只在 `Set-Cookie`**（body 是账号记录，没有 token）；
  · **signin 有 IP 级频率墙**：同一出口几秒内连登多个账号会拿到 `aliyun_waf` 挑战页
    ⇒ 本函数必须走**轮换出口**（SOCKS5 池，每连接换 IP）；直连登录是"把出口打进墙"的标准姿势；
  · 先 `GET /auth` 预热（best-effort），把 WAF 冷启动 cookie 种进会话；
  · 请求需带浏览器特征头（UA + `sec-ch-ua*`），否则同样吃挑战页 —— 单变量实测过。

移植自 `image-adapter/tools/token_service.py`（生产验证版，仅新增浏览器头与可注入 UA），
保持**纯标准库**（SOCKS5 握手很短，自己实现，省一个镜像依赖）。
"""
from __future__ import annotations

import hashlib
import json
import re
import socket
import ssl
import struct
import urllib.parse

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


# --------------------------------------------------------------- SOCKS5 拨号


def parse_socks_url(url: str) -> tuple[str, int, str, str]:
    """`socks5h://user:pass@host:port` -> (host, port, user, password)。"""
    parts = urllib.parse.urlsplit(url if "://" in url else "socks5h://" + url)
    if not parts.hostname or not parts.port:
        raise ValueError("socks url needs host:port")
    return (parts.hostname, parts.port, urllib.parse.unquote(parts.username or ""),
            urllib.parse.unquote(parts.password or ""))


class Socks5Dialer:
    """**每次调用开一条新连接** —— 这正是"换 IP"的机制（池的粘性仅限单连接）。"""

    def __init__(self, url: str, *, timeout: float = 30.0) -> None:
        self.host, self.port, self.user, self.password = parse_socks_url(url)
        self.timeout = timeout

    def open(self, target_host: str, target_port: int = 443) -> socket.socket:
        sock = socket.create_connection((self.host, self.port), self.timeout)
        sock.settimeout(self.timeout)
        try:
            self._handshake(sock, target_host, target_port)
        except Exception:
            sock.close()
            raise
        return sock

    def _handshake(self, sock: socket.socket, host: str, port: int) -> None:
        methods = b"\x02" if self.user else b"\x00"
        sock.sendall(b"\x05" + bytes([len(methods)]) + methods)
        chosen = self._recv(sock, 2)
        if chosen[1] == 0x02:
            user = self.user.encode()
            password = self.password.encode()
            sock.sendall(b"\x01" + bytes([len(user)]) + user
                         + bytes([len(password)]) + password)
            if self._recv(sock, 2)[1] != 0x00:
                raise MintError("socks auth rejected")
        elif chosen[1] != 0x00:
            raise MintError(f"socks method rejected ({chosen[1]})")
        raw = host.encode()
        sock.sendall(b"\x05\x01\x00\x03" + bytes([len(raw)]) + raw
                     + struct.pack("!H", port))
        head = self._recv(sock, 4)
        if head[1] != 0x00:
            raise MintError(f"socks connect failed (code {head[1]})")
        if head[3] == 0x01:
            self._recv(sock, 4)
        elif head[3] == 0x03:
            self._recv(sock, self._recv(sock, 1)[0])
        elif head[3] == 0x04:
            self._recv(sock, 16)
        self._recv(sock, 2)

    @staticmethod
    def _recv(sock: socket.socket, count: int) -> bytes:
        buf = b""
        while len(buf) < count:
            chunk = sock.recv(count - len(buf))
            if not chunk:
                raise MintError("socks peer closed the connection")
            buf += chunk
        return buf


def http_over_socket(sock: socket.socket, host: str, method: str, path: str,
                     *, body: bytes | None = None, headers: dict | None = None,
                     timeout: float = 30.0) -> tuple[int, dict[str, list[str]], bytes]:
    """在已连通的 socket 上做一次 HTTP/1.1 交换（`Connection: close` + 读到 EOF）。"""
    ctx = ssl.create_default_context()
    tls = ctx.wrap_socket(sock, server_hostname=host)
    tls.settimeout(timeout)
    try:
        lines = [f"{method} {path} HTTP/1.1", f"Host: {host}", "Connection: close"]
        for name, value in (headers or {}).items():
            lines.append(f"{name}: {value}")
        if body is not None:
            lines.append(f"Content-Length: {len(body)}")
        raw = ("\r\n".join(lines) + "\r\n\r\n").encode() + (body or b"")
        tls.sendall(raw)
        buf = b""
        while True:
            chunk = tls.recv(65536)
            if not chunk:
                break
            buf += chunk
    finally:
        tls.close()
    head, _, payload = buf.partition(b"\r\n\r\n")
    head_lines = head.decode("latin-1").split("\r\n")
    status = int(head_lines[0].split(" ")[1])
    got: dict[str, list[str]] = {}
    for line in head_lines[1:]:
        name, _, value = line.partition(":")
        got.setdefault(name.strip().lower(), []).append(value.strip())
    return status, got, payload


# ------------------------------------------------------------------ token 铸造


def extract_token_from_setcookie(set_cookies: list[str]) -> str:
    for raw in set_cookies:
        found = re.search(r"(?:^|[;\s])token=([^;]+)", raw)
        if found:
            return found.group(1).strip()
    return ""


def mint_token(socks_url: str, email: str, password: str, *,
               timeout: float = 40.0, base: str = "https://chat.qwen.ai",
               user_agent: str = "") -> str:
    """经轮换出口登录一个账号，返回 token JWT。失败抛 `MintError` / `WallError`。"""
    host = urllib.parse.urlsplit(base).netloc
    digest = hashlib.sha256(password.encode()).hexdigest()
    common = {**browser_hint_headers(user_agent or _default_ua()),
              "Origin": base, "Referer": base + WARM_PATH}
    dialer = Socks5Dialer(socks_url, timeout=timeout)

    # 预热：拿 WAF 冷启动 cookie（best-effort，与生产版行为一致）
    try:
        sock = dialer.open(host)
        try:
            http_over_socket(sock, host, "GET", WARM_PATH, headers=common, timeout=timeout)
        finally:
            sock.close()
    except Exception:  # noqa: BLE001 - 预热失败不阻断，正式请求会再撞一次
        pass

    body = json.dumps({"email": email, "password": digest}).encode()
    sock = dialer.open(host)
    try:
        status, headers, payload = http_over_socket(
            sock, host, "POST", SIGNIN_PATH, body=body,
            headers={**common, "Content-Type": "application/json"},
            timeout=timeout)
    finally:
        try:
            sock.close()
        except OSError:
            pass

    text = payload.decode("utf-8", "replace")
    if "aliyun_waf" in text:
        raise WallError("this egress is answering the WAF challenge page")
    if status != 200:
        raise MintError(f"signin answered HTTP {status}")
    token = extract_token_from_setcookie(headers.get("set-cookie", []))
    if not token:
        raise MintError("no token in Set-Cookie")
    return token


def _default_ua() -> str:
    from ...config import UA_DEFAULT

    return UA_DEFAULT
