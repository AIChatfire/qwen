# qwen-service

chat.qwen.ai 视频生成（**t2v / i2v**）的**火山方舟 Seedance 契约出口**：
多账号池（登录态最小凭据）+ 完整浏览器指纹请求头 + 惰性轮询 + **轻量排队重试（重启不丢任务）**，
一条 `cgt-…` 任务贯穿创建与查询。调用方**只换 Base URL + Key** 即可接入。

```
POST /api/v3/contents/generations/tasks        → 200 {"id": "cgt-…"}   创建（只回 id）
GET  /api/v3/contents/generations/tasks/{id}   → 200 方舟任务对象        查询（六态）
GET  /v1/models                                → 200 能力清单            模型发现（OpenAI 形态）
GET  /healthz · /readyz · /stats                                      运维面（无凭据原文）
```

- **对外契约全文**：**[`docs/INTERFACE.md`](docs/INTERFACE.md)**（冻结）
- **上游契约**：**[`docs/UPSTREAM.md`](docs/UPSTREAM.md)**（qwen 网页端唯一真源）

---

## 0. 我要做什么 → 看哪个文件

| 我想… | 看这里 |
|---|---|
| 接这个服务（方舟 SDK 客户端） | `docs/INTERFACE.md` |
| 改 qwen 请求头 / 请求体 / 响应判读 | `app/upstream/qwen/client.py` |
| 改"多账号怎么轮换、冷却、计额度" | `app/upstream/qwen/accounts.py` |
| 改登录铸造（signin / 轮换出口） | `app/upstream/qwen/signin.py` |
| 改方舟 ↔ qwen 的翻译与降级口径 | `app/ark.py`（纯函数，测试主战场） |
| **改"排队 / 重试 / 出队"策略** | `app/service.py`（编排）+ `app/store.py`（`queued` 记录、`attempts`/`next_attempt_at`）+ `app/coordinator.py`（出队与推进） |
| 改任务存取 | `app/store.py`（SQLModel；SQLite 默认 / PostgreSQL 可选） |
| 改对外鉴权与路由 | `app/main.py` |
| 改上游有没有这个能力 / 未证实项 | `docs/UPSTREAM.md` |

---

## 1. 跑起来

```bash
cp .env.example .env
# 填：QWEN_ACCOUNTS（多账号）、QWEN_ACCOUNT_PASSWORD、QWEN_SIGNIN_PROXY（轮换 HTTP 代理出口，必填）
docker compose up -d --build
curl -s localhost:8400/readyz
```

本机直接跑（用仓内 venv）：

```bash
export QWEN_ACCOUNTS='a@x.cn,b@x.cn' QWEN_ACCOUNT_PASSWORD='…'
export QWEN_SIGNIN_PROXY='http://user:pass@pool.example:2086'   # signin 必须走轮换出口（HTTP 代理）
/Users/betterme/.workbuddy/binaries/python/envs/qwen/bin/gunicorn \
  -c gunicorn_conf.py "app.main:create_app()"
```

🔴 末尾那对**括号不能省**：目标是**工厂**而不是模块级 `app` 对象。
写成 `app.main:app` 会得到 `App failed to load.`（`tests/test_wiring.py` 钉住这条）。

### 零成本自检（不提交、不落库）

```bash
curl -s localhost:8400/api/v3/contents/generations/tasks \
  -H 'Authorization: Bearer <key>' -H 'X-Avm-Dry-Run: 1' \
  -d '{"model":"qwen/video","content":[{"type":"text","text":"一只猫"}],"ratio":"16:9"}'
# → 返回"将要发出的请求"（上游 URL / 完整头（Cookie 打码）/ body）
```

### 测试

```bash
/Users/betterme/.workbuddy/binaries/python/envs/qwen/bin/python -m pytest   # 133 项，零网络
/Users/betterme/.workbuddy/binaries/python/envs/qwen/bin/ruff check .
/Users/betterme/.workbuddy/binaries/python/envs/qwen/bin/python scripts/env_sync_check.py  # .env ⇄ 模板 一致性
```

> 🔒 **公开仓纪律**：真实基础设施标识**一律不入库**（代理池地址 / 内网 IP / 账号上游 id /
> 上游 task id / 任何凭据样态）—— 池地址只在 gitignored 的 `.env` 里，库内一律占位符
> （`pool.example` / `token-service.example`）。这条有门禁：`tests/test_public_surface.py`
> （主机名白名单 + 私网 IP + 凭据样态，白名单每条都要写理由）。

> **配置两份文件的分工**：`.env.example` = 字段说明的权威来源（37 键，含代码默认与坑，进库）；
> `.env` = 本机/部署机的真实取值（gitignored）。模板侧门禁在 `tests/test_env_contract.py`；
> 生效文件侧只能在有 `.env` 的机器上跑脚本（`.env` 不入库 ⇒ 写成测试就只能是假绿灯）。

---

## 2. 冻结决策（2026-09-22）

1. **范围 = 创建 + 查询两个核心端点**；列表 / DELETE **刻意不实现**（路由不存在，
   不返回空列表之类的假数据）。回调 v1 不实现（`callback_url` 进 `degradations`）。
2. **创建响应逐字 `{"id": …}`**；查询响应 = 白名单 ∧ 有真值：
   不出现 `resolution` / `seed` / `usage` 等（不编"看起来合理"的常量）；
   `degradations` 是**加性扩展**（仅非空时出现）。
3. **容量语义 = 3 次/天/账号（UTC 日，t2v+i2v 共用）** ⇒ 账号池按 UTC 日计数 + 冷却 +
   提交节奏（`QWEN_SUBMIT_MIN_INTERVAL`）。真实天花板 = 账号数 × 3。
4. **凭据 = 登录态最小凭据**（`Cookie: token=<JWT>`），signin **必须走轮换出口**；
   每账号 token 分格缓存（互不挤掉），任务记录只存 `credential_id` 指纹。
5. **RGV587 的根因是请求头不全**（不是限速/账号/IP）⇒ 请求头照 `biz-api::build_headers`
   逐字段对齐，`version: 0.2.0` 是写端点硬门槛，两者都有实证（`docs/UPSTREAM.md` §2）。
6. **归属即安全**：跨 Key 读任务 ⇒ **本地 404、不发上游**（有变异自证用例钉住）。
7. **容量不足不再硬 429**：落 `queued` 排队，等窗口自己提交（见 §3）；**只有**队列深度超限
   才回 429 背压。重试**只覆盖"可证明未提交"的失败** —— 建任务是计费动作，"可能已提交"的重试
   等于赌重复计费。

## 3. 排队 / 重试 / 重启不丢（轻量实现）

**没有引入任何独立队列组件**（无 Redis/Celery/独立表）：队列就是任务表里 `status='queued'`
的记录，消费者是既有件 —— 后台协调器（默认开）与调用方的 GET。`queued` 本身是方舟契约里的
合法初始态，所以**对外形态零改动**。

| 关切 | 行为 |
|---|---|
| 容量不足（全冷却 / 额度尽 / 节奏窗） | 落 `queued` + **立即**返回 `cgt-…`（不再 429） |
| 谁把它送上去 | 协调器每 `COORDINATOR_TICK`（默认 5s）出队；调用方 GET 也顺带推进一格 |
| 重试范围 | 风控（RGV587）/ 额度尽 / 鉴权失效 / 铸造失败 / 会话失效 —— **都可证明未受理** |
| 不重试范围 | 上游 5xx / 超时 / 未知业务码 ⇒ 照实回报错误，任务落 `failed`（绝不赌重复计费） |
| 退避与上限 | 指数退避 `QUEUE_RETRY_BASE × 2^n`（上限 600s）；`SUBMIT_MAX_ATTEMPTS`（5）与 `TASK_TIMEOUT`（900s）双闸门封顶 |
| 背压 | 排队深度 ≥ `QUEUE_MAX_DEPTH`（50）⇒ 429 + `Retry-After`（绝不无限囤积） |
| 重启不丢 | `queued`/`running` 记录在任务库（≥7 天）；**账号额度计数与冷却也在库**（KV 快照）⇒ 不会把已用额度算成 0；上游 token 刻意不落盘（重启重铸，免费） |
| 严格模式 | `SUBMIT_QUEUE_ENABLED=0` 回到"容量不足立即 429"（既有调用方口径不变） |

对应用例：`tests/test_queue.py`（7 项）—— 排队→出队→成功、协调器无人查询也推进、
风控换号重试、**含义不明的失败不重试**、背压 429、**跨进程重启后 queued 任务与额度计数都还在**。

## 4. 真实链路状态（2026-09-22 首测 ✅）

经本服务全链路真实跑通（实录：`docs/UPSTREAM.md` §7.1）：

| 步骤 | 结果 |
|---|---|
| signin（经轮换出口 2088） | ✅ `token_len=209` / 4.3s |
| dry-run | ✅ 零成本，头与 body 与抓包同构 |
| **t2v 真跑** | ✅ `cgt-20260922012530-tyoa9`：≈343s，产物 5.50MB / **5.042s** |
| **i2v 真跑**（上游样例图作首帧） | ✅ `cgt-20260922013158-ae0tq`：≈93s，产物 9.59MB / **5.042s** |

- 账号轮换实证：两条任务落在**两个不同账号**（各消耗 1/3 日额度；产物 URL 的 `resource_user_id` 不同）。
- 未证实项变化：**U-7 ✅ 关闭**（token 最小凭据跑通视频写端点）；**U-1 🟡 部分关闭**
  （上游域内图 ✅；第三方外链未验）。
- 冒烟驱动：`scripts/live_smoke.py signin|dryrun|run --kind t2v|i2v`（`run` 会消耗额度）。

### 诚实边界（未完成，不许当已完成）

- 队列**不做请求去重**：同一 `POST` 重发两次 = 两条独立任务（方舟原生同样不保证幂等）。
- 队列吞吐受协调器**单轮串行推进**限制（一轮 = 每个非终态任务一次上游往返）；
  多 worker 需要选主/租约去重（未实现）—— 当前部署模型是 `WORKERS=1`。
- 第三方域名外链图作 i2v 首帧、失败态形态、产物 URL 有效期、额度错误形态：见 `docs/UPSTREAM.md` §9。
- 镜像：CI 构建 + **推送前容器冒烟** + 推 GHCR（`ghcr.io/aicatfire/qwen`，版本/`latest`/`sha-` 三 tag）；
  **尚未部署到任何环境**；本机无 Docker ⇒ 本地构建冒烟未做（由 CI 的推送前冒烟兜住）。
- `rehost`（产物转存）未实现（上游 URL 实测可直下，先透传）。
- 严格 SDK 客户端若对 `degradations` 扩展报错，需要响应裁剪开关（未实现）。

## 5. 相关技能

`seedance-protocol-adapter` · `site-api-to-protocol-adapter` · `multi-account-adapter-pool` ·
`qwen-guest-identity-service`（访客身份，另一个方向）

## 6. 发版（与姊妹仓同一套）

push `main` → 自增 patch（`v0.0.x`）→ 版本号写回 `app/__init__.py` → 构建镜像 →
**容器内冒烟（不过不推）** → 推 GHCR（`ghcr.io/aicatfire/qwen`：版本 / `latest` / `sha-<7位>` 三 tag）
→ 打 annotated tag → 建 Release。人工 tag（minor/major）走 tag 模式（**不改代码**）；
逃生阀：commit message 含 `[skip release]`（仅 push 事件生效，补发用 `workflow_dispatch`）。
