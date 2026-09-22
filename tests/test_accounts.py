"""账号池：轮换 / 额度（UTC 日）/ 冷却 / token 分格缓存 / 脱敏。零网络（mint 注入）。"""
from __future__ import annotations

import base64
import json

import pytest

from app.errors import CredentialUnavailableError, RateLimitedError
from app.upstream.qwen.accounts import (
    OPAQUE_TOKEN_TTL,
    TOKEN_TTL_DEFAULT,
    AccountPool,
    jwt_exp,
    mask_email,
    needs_refresh,
    next_utc_midnight,
)


class Clock:
    def __init__(self, t: float = 1_700_000_000.0) -> None:
        self.t = t

    def __call__(self) -> float:
        return self.t


def make_pool(settings, clock, mint=None) -> AccountPool:
    return AccountPool(settings, mint=mint or (lambda account: f"tok-{account.email}"),
                       now=clock)


def test_masks_email():
    assert mask_email("user@example.com") == "use***@example.com"
    assert mask_email("x") == "x***"


def test_rotates_across_accounts_lru(settings):
    clock = Clock()
    pool = make_pool(settings, clock)
    first, wait = pool.acquire()
    assert first is not None and wait == 0.0
    pool.report_submitted(first.email)
    second, _ = pool.acquire()
    assert second is not None and second.email != first.email
    pool.report_submitted(second.email)
    third, _ = pool.acquire()
    assert third is not None and third.email == first.email  # LRU 回到最早的那个


def test_daily_cap_blocks_then_utc_day_rollover_recovers(settings):
    settings.daily_video_cap = 1
    clock = Clock()
    pool = make_pool(settings, clock)
    for _ in range(2):
        account, _ = pool.acquire()
        assert account is not None
        pool.report_submitted(account.email)
    blocked, _ = pool.acquire()
    assert blocked is None
    with pytest.raises(RateLimitedError):
        pool.acquire_with_wait()
    clock.t += 86400  # 跨过 UTC 日
    recovered, _ = pool.acquire()
    assert recovered is not None


def test_quota_failure_cools_until_next_utc_midnight(settings):
    clock = Clock()
    pool = make_pool(settings, clock)
    pool.report_failure("a@x.cn", "quota")
    state = pool.get_state("a@x.cn")
    assert state is not None
    assert state.cooldown_until == pytest.approx(next_utc_midnight(clock.t))
    picked, _ = pool.acquire()
    assert picked is not None and picked.email != "a@x.cn"


def test_risk_cooldown_expires(settings):
    clock = Clock()
    pool = make_pool(settings, clock)
    for email in ("a@x.cn", "b@x.cn"):
        pool.report_failure(email, "risk")
    blocked, _ = pool.acquire()
    assert blocked is None
    clock.t += 301.0
    recovered, _ = pool.acquire()
    assert recovered is not None


def test_token_cache_is_per_account_and_does_not_cross_evict(settings):
    mints: list[str] = []
    clock = Clock()

    def mint(account):
        mints.append(account.email)
        return f"tok-{account.email}"

    pool = AccountPool(settings, mint=mint, now=clock)
    assert pool.token_for("a@x.cn") == "tok-a@x.cn"
    assert pool.token_for("a@x.cn") == "tok-a@x.cn"
    assert pool.token_for("b@x.cn") == "tok-b@x.cn"
    assert pool.token_for("a@x.cn") == "tok-a@x.cn"   # 分格缓存：B 登录不挤掉 A
    assert mints == ["a@x.cn", "b@x.cn"]


def test_token_expires_and_remints(settings):
    counter = {"n": 0}
    clock = Clock()

    def mint(account):
        counter["n"] += 1
        return f"t{counter['n']}"

    pool = AccountPool(settings, mint=mint, now=clock)
    pool.token_for("a@x.cn")
    clock.t += settings.token_ttl + 1
    pool.token_for("a@x.cn")
    assert counter["n"] == 2


def test_invalidate_token_forces_remint(settings):
    counter = {"n": 0}

    def mint(account):
        counter["n"] += 1
        return f"t{counter['n']}"

    pool = AccountPool(settings, mint=mint, now=Clock())
    pool.token_for("a@x.cn")
    pool.invalidate_token("a@x.cn")
    pool.token_for("a@x.cn")
    assert counter["n"] == 2


def test_signin_pacing_blocks_when_wait_exceeds_ceiling(settings):
    settings.signin_min_interval = 100.0
    settings.signin_wait_timeout = 1.0
    pool = make_pool(settings, Clock())
    pool.token_for("a@x.cn")   # 第一次铸造盖下节奏戳
    with pytest.raises(CredentialUnavailableError, match="节流"):
        pool.token_for("b@x.cn")


def test_no_accounts_configured_is_a_config_error(settings):
    settings.accounts = {}
    pool = AccountPool(settings, mint=lambda account: "x")
    with pytest.raises(CredentialUnavailableError, match="账号"):
        pool.acquire_with_wait()


def test_stats_masks_emails_and_never_leaks_tokens(settings):
    pool = make_pool(settings, Clock())
    pool.token_for("a@x.cn")
    stats = pool.stats()
    text = json.dumps(stats, ensure_ascii=False)
    assert "a@x.cn" not in text
    assert "tok-a@x.cn" not in text
    assert any(row["account"] == "a***@x.cn" for row in stats["accounts"])
    assert all("token" not in row or row.get("token_cached") is not None
               for row in stats["accounts"])


# ---------------------------------------------------------------- 池状态耐久化


def test_snapshot_and_restore_carry_quota_and_cooldown(settings):
    """重启不丢额度计数与冷却（快照只含计数/冷却，不含 token）。"""
    clock = Clock()
    pool = make_pool(settings, clock)
    account, _ = pool.acquire()
    pool.report_submitted(account.email)
    pool.report_failure("b@x.cn", "risk")
    snapshot = json.loads(json.dumps(pool.snapshot()))   # 过一遍 JSON = 真实持久化形态

    restored = make_pool(settings, clock)                # “重启”
    restored.restore(snapshot)
    assert restored.get_state(account.email).day_used == 1
    assert restored.get_state("b@x.cn").cooldown_until > clock.t
    assert restored.get_state("b@x.cn").cooldown_reason == "risk"
    # 冷却仍然生效：下一号只能是被冷掉的那个之外的账号
    nxt, _ = restored.acquire()
    assert nxt is not None and nxt.email == account.email


def test_restore_is_lenient_with_unknown_rows_and_garbage(settings):
    """坏数据/未知账号一律忽略，绝不因快照损坏拒绝启动。"""
    pool = make_pool(settings, Clock())
    pool.restore(None)
    pool.restore({"accounts": "nope"})
    pool.restore({"accounts": {"ghost@x.cn": {"day_used": 99}, "a@x.cn": "not-a-dict"}})
    assert pool.get_state("a@x.cn").day_used == 0
    assert pool.get_state("a@x.cn").cooldown_until == 0.0


def test_on_change_callback_fires_and_its_failure_is_contained(settings):
    """状态变更触发回调（装配层用它落 KV）；回调炸了不许影响主流程。"""
    fired: list[int] = []
    pool = make_pool(settings, Clock())
    pool.on_change = lambda: fired.append(1)
    account, _ = pool.acquire()
    pool.report_submitted(account.email)
    pool.report_failure(account.email, "risk")
    assert len(fired) == 2

    def boom() -> None:
        raise RuntimeError("boom")

    pool.on_change = boom
    pool.report_submitted(account.email)     # 不抛异常
    assert pool.get_state(account.email).day_used == 2


# ---------------------------------------------------------------- token 过期续期


def _jwt(exp: float) -> str:
    """造一个只带 `exp` 的 JWT（本仓只解码调度，不验签）。"""
    payload = base64.urlsafe_b64encode(
        json.dumps({"exp": exp, "id": "acct"}).encode()).decode().rstrip("=")
    return f"h.{payload}.s"


def test_jwt_exp_parses_and_is_lenient():
    assert jwt_exp(_jwt(1_792_659_003)) == 1_792_659_003.0
    assert jwt_exp("not-a-jwt") is None            # 不透明 token
    assert jwt_exp("a.!!!.c") is None              # 坏 base64
    assert jwt_exp("a.eyJpZCI6IngifQ.b") is None   # 没有 exp 键
    assert jwt_exp("") is None


def test_self_declared_exp_is_capped_by_ttl():
    """🔴 `exp` 是上游**自称**的（实测 30 天）⇒ 必须有保守上限压住，不能拿自称当真实寿命。"""
    minted = 1_000_000.0
    exp = minted + 30 * 86400
    assert not needs_refresh(minted_at=minted, expires_at=exp, ttl=86400.0, now=minted + 3600)
    assert needs_refresh(minted_at=minted, expires_at=exp, ttl=86400.0, now=minted + 86400)


def test_default_cap_six_days_reserves_headroom_for_suspected_seven():
    """🔴 用户口径（2026-09-22）：`exp` 自称 30 天但**实际可能 7 天失效** ⇒
    按 **6 天**换（`TOKEN_TTL_DEFAULT`），预留 1 天余量，不赌到最后一刻。"""
    minted = 1_000_000.0
    exp = minted + 30 * 86400
    assert TOKEN_TTL_DEFAULT == 6 * 86400
    assert not needs_refresh(minted_at=minted, expires_at=exp,
                             ttl=TOKEN_TTL_DEFAULT, now=minted + TOKEN_TTL_DEFAULT - 1)
    assert needs_refresh(minted_at=minted, expires_at=exp,
                         ttl=TOKEN_TTL_DEFAULT, now=minted + TOKEN_TTL_DEFAULT)
    assert minted + TOKEN_TTL_DEFAULT < minted + 7 * 86400, "换 token 必须早于疑似的 7 天墙"


def test_ttl_le_zero_falls_back_to_default_cap():
    """`ttl<=0` **不再表示"关掉上限"**（那正是 7 天失效会咬人的位置）—— 一律归一到 6 天。"""
    minted = 1_000_000.0
    exp = minted + 30 * 86400
    assert not needs_refresh(minted_at=minted, expires_at=exp,
                             ttl=0.0, now=minted + TOKEN_TTL_DEFAULT - 1)
    assert needs_refresh(minted_at=minted, expires_at=exp,
                         ttl=0.0, now=minted + TOKEN_TTL_DEFAULT)
    assert not needs_refresh(minted_at=minted, expires_at=exp, ttl=-5.0, now=minted + 3600)


def test_short_exp_wins_over_longer_cap():
    """`exp` 比上限**更短**时按 `exp` 走（提前量 6 小时）—— 上限只负责压住"过长的自称"。"""
    minted = 1_000_000.0
    exp = minted + 3 * 86400                        # 自称 3 天 < 6 天上限
    assert not needs_refresh(minted_at=minted, expires_at=exp, ttl=TOKEN_TTL_DEFAULT,
                             now=exp - 6 * 3600 - 1)
    assert needs_refresh(minted_at=minted, expires_at=exp, ttl=TOKEN_TTL_DEFAULT,
                         now=exp - 6 * 3600)


def test_opaque_token_falls_back_to_ttl_then_default():
    """非 JWT（不透明 token，如外部 token 服务）⇒ 按 `ttl`；`ttl` 也给 0 则兜底 6 小时。"""
    minted = 1_000_000.0
    assert not needs_refresh(minted_at=minted, expires_at=0.0, ttl=7200.0, now=minted + 3600)
    assert needs_refresh(minted_at=minted, expires_at=0.0, ttl=7200.0, now=minted + 7200)
    assert not needs_refresh(minted_at=minted, expires_at=0.0, ttl=0.0, now=minted + OPAQUE_TOKEN_TTL - 1)
    assert needs_refresh(minted_at=minted, expires_at=0.0, ttl=0.0, now=minted + OPAQUE_TOKEN_TTL)


def test_pool_remints_at_default_cap_not_at_self_declared_exp(settings):
    """池的真实行为：默认 6 天上限 ⇒ **第 6 天就换**（不等自称的 30 天）。"""
    settings.signin_min_interval = 0.0
    clock = Clock()
    mints: list[str] = []
    pool = AccountPool(settings, mint=lambda a: (mints.append(a.email),
                                                 _jwt(clock.t + 30 * 86400))[1], now=clock)
    pool.token_for("a@x.cn")
    clock.t += TOKEN_TTL_DEFAULT - 1
    pool.token_for("a@x.cn")
    assert len(mints) == 1                          # 还没到 6 天 ⇒ 复用
    assert pool.stats()["accounts"][0]["token_cached"] is True
    clock.t += 2                                    # 越过 6 天
    pool.token_for("a@x.cn")
    assert len(mints) == 2


def test_pool_caps_cache_at_token_ttl(settings):
    """默认口径（`ttl=1 天`）：即便 token 自称 30 天，也诚实按 1 天换 —— 上游可能提前失效。"""
    settings.token_ttl = 86400.0
    settings.signin_min_interval = 0.0
    clock = Clock()
    mints: list[str] = []
    pool = AccountPool(settings, mint=lambda a: (mints.append(a.email),
                                                 _jwt(clock.t + 30 * 86400))[1], now=clock)
    pool.token_for("a@x.cn")
    clock.t += 86399
    pool.token_for("a@x.cn")
    assert len(mints) == 1
    clock.t += 2                                # 越过 1 天上限
    pool.token_for("a@x.cn")
    assert len(mints) == 2
    assert pool.get_state("a@x.cn").expires_at == pytest.approx(clock.t - 2 + 30 * 86400)


def test_invalidate_and_auth_failure_clear_expiry(settings):
    """被动路径：清缓存时必须连 `expires_at` 一起清，否则"复用"判定会拿旧到期时间放行。"""
    pool = make_pool(settings, Clock(), mint=lambda a: _jwt(1_792_659_003))
    pool.token_for("a@x.cn")
    assert pool.get_state("a@x.cn").expires_at > 0
    pool.invalidate_token("a@x.cn")
    assert pool.get_state("a@x.cn").expires_at == 0.0

    pool.token_for("a@x.cn")
    pool.report_failure("a@x.cn", "auth")
    assert pool.get_state("a@x.cn").expires_at == 0.0
