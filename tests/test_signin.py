"""signin 铸造 —— 出口只走 HTTP(S) 代理。零网络（注入 `httpx.MockTransport`）。

覆盖三件事：
  ① 请求形状：POST `/api/v2/auths/signin`、body 是 `sha256hex(password)`、带浏览器特征头
     （**不是**明文口令 —— 上游契约如此）；
  ② token **只从 `Set-Cookie` 读**（body 里的账号记录没有 token）；
  ③ 失败分类：WAF 挑战页 → `WallError`；非 200 / 拿不到 token → `MintError`；
     SOCKS 等非 HTTP 形态 → `MintError`（按用户决策**不做兼容**）。
"""
from __future__ import annotations

import hashlib
import json

import httpx
import pytest

from app.upstream.qwen.signin import MintError, WallError, mint_token

SIGNIN = "/api/v2/auths/signin"


def _client(handler) -> httpx.MockTransport:
    return httpx.MockTransport(handler)


def test_sends_sha256_password_and_reads_token_from_set_cookie():
    seen: dict = {}

    def handler(request: httpx.Request) -> httpx.Response:
        if request.url.path.endswith("/auth"):     # 预热：无 body 的 GET，响应无所谓
            return httpx.Response(200, text="<html>warmup</html>")
        seen["method"] = request.method
        seen["path"] = request.url.path
        seen["ua"] = request.headers.get("user-agent", "")
        seen["body"] = json.loads(request.content)
        return httpx.Response(
            200, json={"success": True, "data": {"email": "x"}},
            headers={"set-cookie": "token=JWT-abc.def.ghi; Path=/; HttpOnly"})

    token = mint_token("http://user:pw@pool.local:2086", "a@x.cn", "s3cret",
                       transport=_client(handler))

    assert token == "JWT-abc.def.ghi"
    assert seen["method"] == "POST" and seen["path"] == SIGNIN
    assert seen["body"]["email"] == "a@x.cn"
    assert seen["body"]["password"] == hashlib.sha256(b"s3cret").hexdigest()
    assert "s3cret" not in json.dumps(seen["body"]), "口令绝不明文外发"
    assert "Chrome" in seen["ua"], "必须带浏览器特征头"


def test_warmup_is_best_effort_and_does_not_block():
    """预热（GET /auth）失败不能阻断铸造。"""
    hits: list[str] = []

    def handler(request: httpx.Request) -> httpx.Response:
        hits.append(request.url.path)
        if request.url.path.endswith("/auth"):
            raise httpx.ConnectError("warmup down")
        return httpx.Response(200, json={"success": True},
                              headers={"set-cookie": "token=T2"})

    assert mint_token("http://u:p@pool.local:2086", "a@x.cn", "pw",
                      transport=_client(handler)) == "T2"
    assert hits == ["/auth", SIGNIN]


def test_waf_challenge_page_is_wall_error():
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, text="<html>aliyun_waf_xxx challenge</html>")

    with pytest.raises(WallError):
        mint_token("http://u:p@pool.local:2086", "a@x.cn", "pw",
                   transport=_client(handler))


def test_non_200_and_missing_token_are_mint_errors():
    with pytest.raises(MintError, match="HTTP 403"):
        mint_token("http://u:p@pool.local:2086", "a@x.cn", "pw",
                   transport=_client(lambda r: httpx.Response(403, json={})))

    with pytest.raises(MintError, match="Set-Cookie"):
        mint_token("http://u:p@pool.local:2086", "a@x.cn", "pw",
                   transport=_client(lambda r: httpx.Response(200, json={"success": True})))


def test_socks_form_is_rejected_with_clear_message():
    """SOCKS 分支已删除（用户决策：不做兼容）⇒ 必须响亮失败，而不是静默改成直连。"""
    with pytest.raises(MintError, match="http"):
        mint_token("socks5h://u:p@pool.local:2088", "a@x.cn", "pw",
                   transport=_client(lambda r: httpx.Response(200, json={})))


def test_proxy_transport_failure_is_mint_error():
    def handler(request: httpx.Request) -> httpx.Response:
        raise httpx.ProxyError("proxy auth failed")

    with pytest.raises(MintError, match="传输失败"):
        mint_token("http://u:p@pool.local:2086", "a@x.cn", "pw",
                   transport=_client(handler))


def test_production_path_passes_proxy_and_never_env_proxy(monkeypatch):
    """生产路径必须**显式带代理** + `trust_env=False`。

    为什么单独测它：注入 `transport` 时 httpx 会**跳过代理**（proxy 是 mounts，覆盖 transport），
    所以"请求形状"那几条用例证明不了"真的走了代理" ⇒ 这里用 spy 断言构造 kwargs。
    """
    captured: dict = {}
    real_client = httpx.Client

    def spy(*args, **kwargs):
        captured.update(kwargs)
        return real_client(*args, transport=httpx.MockTransport(
            lambda r: httpx.Response(200, json={"success": True},
                                     headers={"set-cookie": "token=T"})))

    monkeypatch.setattr(httpx, "Client", spy)
    assert mint_token("http://u:p@pool.live:2086", "a@x.cn", "pw") == "T"
    assert captured["proxy"] == "http://u:p@pool.live:2086", "必须走配置的轮换出口"
    assert captured["trust_env"] is False, "别让宿主的 HTTP(S)_PROXY 静默接管"
