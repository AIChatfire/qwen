"""敏感面门禁 —— 公开仓的**已跟踪文件**里不得出现私域主机 / 内网 IP / 凭据样态。零网络。

背景（2026-09-22）：一次「一律脱敏」把 6 处真实基础设施标识清出公开面。本门禁防复发。
它刻意做成**形状门禁**而不是"真实标识黑名单"——把要藏的东西写进禁止清单，等于连同门禁一起公开。

三条判据：
  1. **主机名**：只允许 `ALLOWED_HOSTS` 里显式登记的（每条带理由）+ RFC 保留 TLD（`.example` /
     `.local` / `.invalid` / `.test`，占位符专用，永不可能是真基础设施）；
  2. **IP**：只允许 `0.0.0.0` / `127.0.0.1`；任何私网地址（10/172.16-31/192.168）一律判红；
  3. **凭据样态**：JWT 原文 / `Bearer <长串>` / `token=<长值>` / 私钥块 / 常见密钥前缀。

范围 = `git ls-files`（只扫入库文件；`.env`、`var/` 等 gitignored 的自然不在内）。
⚠️ 两条**防空转**断言：文件数下界 + 扫到的主机数下界 —— 正则或范围写错时要红，而不是静默变绿。
"""
from __future__ import annotations

import re
import subprocess
from pathlib import Path

REPO = Path(__file__).resolve().parent.parent

#: 允许出现在公开仓里的主机名 —— 每条必须能说出理由（新域名要显式加进来，别改成宽正则）
ALLOWED_HOSTS: dict[str, str] = {
    "chat.qwen.ai": "服务对象（上游）",
    "cdn.qwenlm.ai": "上游产物 CDN",
    "qwen-chat.oss-ap-southeast-1.aliyuncs.com": "i2v 冒烟用的上游样例图",
    "ghcr.io": "镜像仓库（CI 推送目标）",
    "github.com": "仓库/文档链接",
    "users.noreply.github.com": "发布流水线的 bot 提交身份（公开域名）",
    "example.com": "占位邮箱域（RFC 2606 保留域名）",
    "x.cn": "测试里的假邮箱域（a@x.cn / b@x.cn）",
}

#: RFC 2606/6761 保留 TLD —— 占位符专用，永不可能是真基础设施（`.example` / `.local` / …）
RESERVED_TLDS = ("example", "local", "invalid", "test")

#: 可能被判为域名的 TLD 集合。**故意收窄**：`logging.info` / `pytest.raises` 这类属性链
#: 的"TLD"不在集合里 ⇒ 不会被误报成主机名（`.` 后的段就是最好的过滤器）。
KNOWN_TLDS = ("com", "cn", "net", "org", "io", "ai", "dev", "co", "me")

#: 允许的 IP（本机绑定用）；其余任何 IP 都要人工过一遍
ALLOWED_IPS = ("0.0.0.0", "127.0.0.1")

#: 凭据样态（命中即红；测试里的假值不会被这些形状匹配 —— 已本地核过 0 误报）
CREDENTIAL_PATTERNS: dict[str, str] = {
    "JWT 原文": r"eyJ[A-Za-z0-9_-]{10,}\.[A-Za-z0-9_-]{10,}",
    "Bearer + 长串": r"Bearer\s+[A-Za-z0-9_\-\.]{24,}",
    # 裸的长 token 值也算（别只认 `Cookie: token=` 前缀 —— 变异自证时就是从这个缝里漏过去的）
    "token= 长值": r"token=[A-Za-z0-9_\-\.]{20,}",
    "私钥块": r"BEGIN [A-Z ]*PRIVATE KEY",
    "常见密钥前缀": r"(ghp_|gho_|sk-[A-Za-z0-9]{20,}|AKIA[0-9A-Z]{12,}|xox[baprs]-)",
}

_SKIP_SUFFIX = (".png", ".jpg", ".jpeg", ".gif", ".mp4", ".ico", ".pyc")


def tracked_files() -> list[Path]:
    out = subprocess.run(["git", "ls-files"], cwd=REPO, capture_output=True, text=True).stdout
    files = [REPO / line for line in out.split("\n") if line.strip()]
    assert len(files) >= 40, f"只扫到 {len(files)} 个跟踪文件 ⇒ 命令/范围写错，门禁在空转"
    return [p for p in files if not p.name.endswith(_SKIP_SUFFIX)]


def _read(path: Path) -> str:
    try:
        return path.read_text(encoding="utf-8", errors="ignore")
    except OSError:
        return ""


def _host_re() -> re.Pattern[str]:
    return re.compile(
        r"(?<![\w:.\-/])((?:[a-z0-9](?:[a-z0-9\-]*[a-z0-9])?\.)+"
        r"(?:" + "|".join(KNOWN_TLDS + RESERVED_TLDS) + r"))(?![\w\-])")


def scan_hosts() -> dict[str, list[str]]:
    """→ {主机名: [文件…]}（只报未登记的）。"""
    found: dict[str, list[str]] = {}
    for path in tracked_files():
        for m in _host_re().finditer(_read(path)):
            host = m.group(1)
            if host in ALLOWED_HOSTS or host.rsplit(".", 1)[-1] in RESERVED_TLDS:
                continue
            found.setdefault(host, []).append(str(path.relative_to(REPO)))
    return found


def scan_ips() -> dict[str, list[str]]:
    ip_re = re.compile(r"\b(?:10\.\d{1,3}\.\d{1,3}\.\d{1,3}"
                       r"|172\.(?:1[6-9]|2\d|3[01])\.\d{1,3}\.\d{1,3}"
                       r"|192\.168\.\d{1,3}\.\d{1,3})\b")
    found: dict[str, list[str]] = {}
    for path in tracked_files():
        for m in ip_re.finditer(_read(path)):
            found.setdefault(m.group(0), []).append(str(path.relative_to(REPO)))
    return found


def scan_credentials() -> dict[str, list[str]]:
    found: dict[str, list[str]] = {}
    for label, pattern in CREDENTIAL_PATTERNS.items():
        rx = re.compile(pattern)
        for path in tracked_files():
            if rx.search(_read(path)):
                found.setdefault(label, []).append(str(path.relative_to(REPO)))
    return found


# ---------------------------------------------------------------- 门禁


def test_no_unregistered_hosts():
    """公开仓里的主机名必须都在白名单（或 RFC 保留 TLD）里 —— 新域名要显式登记 + 写理由。"""
    assert scan_hosts() == {}, f"有未登记的主机名：{scan_hosts()}"


def test_scanner_is_not_idle():
    """防空转：把白名单清空后应当能扫出一堆主机名 —— 否则是正则失效（静默全绿的假门禁）。"""
    seen: set[str] = set()
    for path in tracked_files():
        seen.update(_host_re().findall(_read(path)))
    assert len(seen) >= 8, f"扫描器只认出 {len(seen)} 个主机名 ⇒ 正则失效，门禁在空转"
    assert len(tracked_files()) >= 40, "跟踪文件数下界"


def test_no_private_ips_in_tracked_files():
    """内网地址（10/172.16-31/192.168）不得出现在公开仓。"""
    assert scan_ips() == {}, f"发现私网地址：{scan_ips()}"


def test_no_credential_shaped_strings():
    """凭据样态不得入库（哪怕"看起来是假的" —— 判据是形状，不是意图）。"""
    assert scan_credentials() == {}, f"发现凭据样态：{scan_credentials()}"


def test_allowed_hosts_are_documented():
    """白名单每条都要有理由 —— 它是"让门禁闭嘴"的唯一通道，理由就是 review 的抓手。"""
    for host, reason in ALLOWED_HOSTS.items():
        assert reason and len(reason) >= 6, f"{host} 的理由太短，等于没写"
    assert len(ALLOWED_HOSTS) >= 5, "白名单被清空了？那说明改用宽正则绕过了门禁"
