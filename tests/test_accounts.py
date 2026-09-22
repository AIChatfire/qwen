"""账号池：轮换 / 额度（UTC 日）/ 冷却 / token 分格缓存 / 脱敏。零网络（mint 注入）。"""
from __future__ import annotations

import json

import pytest

from app.errors import CredentialUnavailableError, RateLimitedError
from app.upstream.qwen.accounts import AccountPool, mask_email, next_utc_midnight


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
