"""配置 —— 全部来自环境变量，`Settings.from_env()` 一处收拢。

约定（沿用 jimeng / hailuo / video-adapter）：
  · 凭据只从 env / env 指定的文件来，**绝不写进源码**；
  · 写错配置要**响亮失败**（缺账号 / 缺 signin 出口 / DSN 非法），不静默退回默认。
"""
from __future__ import annotations

import json
import os
import secrets
from dataclasses import dataclass, field
from pathlib import Path

UA_DEFAULT = ("Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 "
              "(KHTML, like Gecko) Chrome/152.0.0.0 Safari/537.36")

REPO_ROOT = Path(__file__).resolve().parent.parent


def _env(env: dict, name: str, default: str = "") -> str:
    return (env.get(name) or default).strip()


def _bool(env: dict, name: str, default: bool = False) -> bool:
    raw = env.get(name)
    if raw is None or str(raw).strip() == "":
        return default
    return str(raw).strip().lower() in ("1", "true", "yes", "on")


def _num(env: dict, name: str, default: float) -> float:
    raw = env.get(name)
    if raw is None or str(raw).strip() == "":
        return default
    return float(raw)


def _int(env: dict, name: str, default: int) -> int:
    raw = env.get(name)
    if raw is None or str(raw).strip() == "":
        return default
    return int(raw)


def parse_accounts(env: dict | None = None) -> dict[str, str]:
    """账号表：`QWEN_ACCOUNTS=email:pass,email2`（无密码条目取 `QWEN_ACCOUNT_PASSWORD`）
    或 `QWEN_ACCOUNTS_FILE`（每行一条 `email:pass`，`#` 注释）。

    ⚠️ 密码里含逗号时请改用文件形态（逗号是列表分隔符）。
    """
    env = env if env is not None else os.environ
    default_pw = _env(env, "QWEN_ACCOUNT_PASSWORD")
    accounts: dict[str, str] = {}

    def _add(item: str) -> None:
        item = item.strip()
        if not item or item.startswith("#"):
            return
        email, sep, pw = item.partition(":")
        accounts[email.strip()] = (pw if sep else default_pw).strip()

    for item in (_env(env, "QWEN_ACCOUNTS")).split(","):
        _add(item)
    path = _env(env, "QWEN_ACCOUNTS_FILE")
    if path and Path(path).exists():
        for line in Path(path).read_text(encoding="utf-8").splitlines():
            _add(line)
    return accounts


def parse_account_cookies(env: dict | None = None) -> dict[str, str]:
    """可选：每账号附加 cookie 串（整份浏览器 jar 或 `ssxmod_itna` / `acw_tc` 等指纹 cookie）。

    来源 `QWEN_ACCOUNT_COOKIES`（JSON：`{"email": "k=v; k=v"}`）或
    `QWEN_ACCOUNT_COOKIES_FILE`（同格式 JSON 文件）。默认空 ⇒ 只发 `Cookie: token=<JWT>`
    （登录态最小凭据，见 `docs/UPSTREAM.md` §2）。
    """
    env = env if env is not None else os.environ
    raw = _env(env, "QWEN_ACCOUNT_COOKIES")
    path = _env(env, "QWEN_ACCOUNT_COOKIES_FILE")
    data: dict = {}
    if raw:
        data = json.loads(raw)
    elif path and Path(path).exists():
        data = json.loads(Path(path).read_text(encoding="utf-8"))
    if not isinstance(data, dict):
        raise ValueError("QWEN_ACCOUNT_COOKIES 必须是 JSON 对象：{email: cookie_string}")
    return {str(k): str(v) for k, v in data.items()}


@dataclass
class Settings:
    # —— 上游 ——
    upstream_base: str = "https://chat.qwen.ai"
    chat_model: str = "qwen3.7-plus"
    user_agent: str = UA_DEFAULT
    version_header: str = "0.2.0"
    upstream_timeout: float = 60.0
    trust_env: bool = False

    # —— 凭据池（7 账号等） ——
    accounts: dict[str, str] = field(default_factory=dict)
    account_cookies: dict[str, str] = field(default_factory=dict)
    signin_socks: str = ""
    token_url: str = ""
    #: token 缓存**上限**（秒）—— 主动续期取 `min(JWT 的 exp - 提前量, 铸后本值)`。
    #: 🔴 为什么不能只看 `exp`：`exp` 是上游**自称**的（实测 30 天），服务端可能提前失效
    #: （自称 30 天、实际 7 天就判 401 是有先例的形态）⇒ 用一个保守上限把它压住，默认 **1 天**。
    #: `0` = 不设上限（完全按 `exp`，仅在对上游行为有把握时用）；token 无 `exp` 时本值即兜底缓存时长。
    token_ttl: float = 86400.0
    signin_min_interval: float = 45.0
    signin_wait_timeout: float = 45.0
    submit_min_interval: float = 15.0
    daily_video_cap: int = 3
    account_wait_timeout: float = 30.0

    # —— 轻量队列 / 重试（"自己排队、自己重试"；关闭则回到严格 429） ——
    submit_queue_enabled: bool = True
    queue_max_depth: int = 50
    submit_max_attempts: int = 5
    queue_retry_base: float = 30.0

    # —— 对外鉴权 ——
    api_keys: list[str] = field(default_factory=list)
    key_secret: str = ""

    # —— 任务持久化 ——
    task_db: str = ""
    data_dir: str = ""
    poll_interval: float = 3.0
    task_timeout: float = 900.0
    task_retention_days: int = 7
    coordinator_enabled: bool = True
    coordinator_tick: float = 5.0

    # —— 进程 ——
    host: str = "0.0.0.0"
    port: int = 8400
    log_level: str = "INFO"

    @classmethod
    def from_env(cls, environ: dict | None = None) -> Settings:
        env = environ if environ is not None else os.environ
        data_dir = _env(env, "DATA_DIR") or str(REPO_ROOT / "var")
        task_db = _env(env, "TASK_DB") or f"sqlite:///{data_dir}/qwen.db"
        return cls(
            upstream_base=_env(env, "QWEN_BASE_URL") or "https://chat.qwen.ai",
            chat_model=_env(env, "QWEN_CHAT_MODEL") or "qwen3.7-plus",
            user_agent=_env(env, "QWEN_USER_AGENT") or UA_DEFAULT,
            version_header=_env(env, "QWEN_VERSION_HEADER") or "0.2.0",
            upstream_timeout=_num(env, "QWEN_UPSTREAM_TIMEOUT", 60.0),
            trust_env=_bool(env, "QWEN_TRUST_ENV", False),
            accounts=parse_accounts(env),
            account_cookies=parse_account_cookies(env),
            signin_socks=_env(env, "QWEN_SIGNIN_SOCKS"),
            token_url=_env(env, "QWEN_TOKEN_URL"),
            token_ttl=_num(env, "QWEN_TOKEN_TTL", 86400.0),
            signin_min_interval=_num(env, "QWEN_SIGNIN_MIN_INTERVAL", 45.0),
            signin_wait_timeout=_num(env, "QWEN_SIGNIN_WAIT_TIMEOUT", 45.0),
            submit_min_interval=_num(env, "QWEN_SUBMIT_MIN_INTERVAL", 15.0),
            daily_video_cap=_int(env, "QWEN_DAILY_VIDEO_CAP", 3),
            account_wait_timeout=_num(env, "QWEN_ACCOUNT_WAIT_TIMEOUT", 30.0),
            submit_queue_enabled=_bool(env, "SUBMIT_QUEUE_ENABLED", True),
            queue_max_depth=_int(env, "QUEUE_MAX_DEPTH", 50),
            submit_max_attempts=_int(env, "SUBMIT_MAX_ATTEMPTS", 5),
            queue_retry_base=_num(env, "QUEUE_RETRY_BASE", 30.0),
            api_keys=[p.strip() for p in _env(env, "API_KEYS").split(",") if p.strip()],
            key_secret=_env(env, "KEY_SECRET"),
            task_db=task_db,
            data_dir=data_dir,
            poll_interval=_num(env, "POLL_INTERVAL", 3.0),
            task_timeout=_num(env, "TASK_TIMEOUT", 900.0),
            task_retention_days=_int(env, "TASK_RETENTION_DAYS", 7),
            coordinator_enabled=_bool(env, "COORDINATOR_ENABLED", True),
            coordinator_tick=_num(env, "COORDINATOR_TICK", 5.0),
            host=_env(env, "HOST") or "0.0.0.0",
            port=_int(env, "PORT", 8400),
            log_level=_env(env, "LOG_LEVEL") or "INFO",
        )

    # ------------------------------------------------------------------ 派生

    @property
    def base_url(self) -> str:
        return self.upstream_base.rstrip("/")

    @property
    def ready(self) -> bool:
        return bool(self.accounts)

    def resolved_key_secret(self) -> str:
        """HMAC 指纹密钥：env 优先；否则在 data_dir 落一个 600 的随机值（重启不变）。

        任务记录里只存 `credential_id = hmac-sha256(secret, key)` 指纹 —— API Key 是
        低熵可枚举空间，裸 sha256 不够（见 video-adapter `ADR-003`）。
        """
        if self.key_secret:
            return self.key_secret
        path = Path(self.data_dir) / "hmac_secret"
        if path.exists():
            value = path.read_text(encoding="utf-8").strip()
            if value:
                return value
        path.parent.mkdir(parents=True, exist_ok=True)
        value = secrets.token_hex(32)
        path.write_text(value, encoding="utf-8")
        try:
            path.chmod(0o600)
        except OSError:  # pragma: no cover - 平台差异
            pass
        return value
