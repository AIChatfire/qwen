# qwen-service 对外契约（火山方舟 Seedance 形态）

> 前门 = `POST|GET /api/v3/contents/generations/tasks`，与火山方舟原生契约**逐字段对齐**
> —— 调用方**只换 Base URL + Key** 即可接入。上游是 chat.qwen.ai 视频链路（t2v / i2v）。
> 上游侧契约见 [`UPSTREAM.md`](UPSTREAM.md)。

---

## 0. 端点总表

| # | 方法 | 路径 | 状态 |
|---|---|---|---|
| 1 | `POST` | `/api/v3/contents/generations/tasks` | 🔴 核心（逐字实现） |
| 2 | `GET` | `/api/v3/contents/generations/tasks/{id}` | 🔴 核心（逐字实现） |
| 3 | `GET` | `/api/v3/contents/generations/tasks`（列表） | ⚪ **刻意不实现**（路由不存在 ⇒ 404/405） |
| 4 | `DELETE` | `/api/v3/contents/generations/tasks/{id}` | ⚪ **刻意不实现**（同上；未终态也绝不伪造 `cancelled`） |
| — | `GET` | `/healthz` `/readyz` `/stats` | 运维面（不含任何凭据原文） |

> 🔴 **范围冻结**：不适配的可选端点**不得返回空列表之类的假数据** —— 调用方会把"空列表"
> 误读成"没有任务"。列表与取消要真做时，**先改本文件**再动代码。

---

## 1. 鉴权

- `Authorization: Bearer <API Key>`；**未配置 `API_KEYS` 时鉴权关闭**（仅限内网部署，显式声明）。
- 任务与 Key **绑定**：任务记录里存 `credential_id = HMAC-SHA256(secret, key)` 指纹
  （永不落明文）。**换一把 Key 读同一任务 ⇒ 本地直接 404，且不发任何上游请求**。
- 缺失/无效 Key ⇒ `401 AuthenticationError`。

---

## 2. 创建任务

```
POST /api/v3/contents/generations/tasks
Content-Type: application/json
Authorization: Bearer <key>
```

### 2.1 请求字段

| 字段 | 类型 | 必填 | 本服务行为 |
|---|---|---|---|
| `model` | string | ✅ | `qwen/<任意名>` 或裸名；**provider 段必须为 `qwen`**，否则 400。模型名不转发上游（上游无视频模型维度，见 UPSTREAM §8 D-14） |
| `content` | object[] | ✅ | 见 §2.2 |
| `ratio` | string | ❌ | 枚举：`1:1` / `3:4` / `4:3` / `16:9` / `9:16`；**枚举外（含 `adaptive`、缺失、写错）一律落 `1:1` + 降级说明**（冻结口径 D-2） |
| `duration` | integer | ❌ | 上游固定 ~5s：`5` 静默；`>5` 向下吸附 + 说明；`-1` 替换成 5 + 说明；**`<5` 直接 400** |
| `callback_url` | string | ❌ | v1 未实现回调 ⇒ 进 `degradations`（请轮询查询接口） |
| `resolution` / `seed` / `watermark` / `camera_fixed` / `generate_audio` / `return_last_frame` / `frames` / `service_tier` / `draft` / `priority` / `safety_identifier` / `tools` / `omni_reference_task_type` / `output_format` / `execution_expires_after` | — | ❌ | **认得但做不到** ⇒ 进 `degradations`（不假装支持、不静默丢弃；`seed: -1` 视为"没给"） |
| `extra_body` | object | ❌ | 未建模字段口袋：**每个键**都会进 `degradations` |

### 2.2 `content[]` 降维矩阵（方舟超集 → qwen 子集）

| 入参 | 处置 |
|---|---|
| `text`（可多个） | 按顺序换行拼接为单条 prompt |
| `image_url` + `role:"first_frame"`（或**无 role**） | ⇒ i2v 首帧；**恰好一张**才合法 |
| `image_url` + `role:"last_frame"` | **400**（上游没有尾帧能力，也不悄悄当首帧） |
| `image_url` + `role:"reference_image"` | **400**（上游不支持参考图） |
| 两张及以上 `image_url` | **400**（不替调用方挑一张） |
| `video_url` / `audio_url` / `draft_task` | **400**（上游无此能力） |
| 认不出的 `type` | **400** |

**图片 URL 形态**：只收 `http(s)` 绝对地址；`data:` URI ⇒ **400**（v1 无上传链路）。
非上游域名（非 `cdn.qwenlm.ai` / `qwen-chat.oss-*`）⇒ **照发 + 降级告警**（外链作首帧未完全验证）。

### 2.3 响应（逐字对齐原生）

```json
{ "id": "cgt-20260922043000-ab12c" }
```

- **只回 `id`**，不含 status（必须轮询或查询）。
- `id` 由本服务生成（`cgt-YYYYMMDDHHMMSS-xxxxx`）；上游 UUID 只存本层记录与观测面。
- 容量不足时**不再回 429**：任务落 `queued` 并**立即**返回 id（`queued` 是方舟契约里的合法初始态），
  详见 §3.3。只有**排队深度**超 `QUEUE_MAX_DEPTH` 才回 429 背压。

---

## 3. 查询任务

```
GET /api/v3/contents/generations/tasks/{id}
Authorization: Bearer <key>
```

### 3.1 响应字段（**键集最小且只给真值**）

| 键 | 出现条件 | 说明 |
|---|---|---|
| `id` | 恒有 | 本服务任务 id |
| `model` | 恒有 | 创建时请求的 `model` 原样回显 |
| `status` | 恒有 | 六态枚举：`queued` / `running` / `succeeded` / `failed` / `expired` / `cancelled`（当前实现只会出现前五个之一，`cancelled` 无触发入口） |
| `error` | 恒有 | 成功时显式 `null`（方舟规定）；失败时 `{code, message}` |
| `created_at` / `updated_at` | 恒有 | **epoch 秒**（整数） |
| `content.video_url` | 仅 `succeeded` | 调用方拿到即可下载（上游 CDN 签名直链，实测可直下） |
| `duration` | 仅 `succeeded` | **5**（上游固定 ~5.042s，实测 n≥5；不是请求值） |
| `ratio` | 有值时 | 实际生效比例（决定输出画幅） |
| `degradations` | 仅非空时 | 🔴 **加性扩展**（原生没有这个键）：本层的降级/吸附/忽略说明清单 |

🔴 **不编造**：`resolution` / `seed` / `usage` / `frames` / `framespersecond` / `draft` 等
**一概不出现**（上游不回传/不支持，编一个"看起来合理"的常量比留空更糟）。
`usage` 尤其：上游没有 token 口径，视频额度是"3 次/天"计数 —— 给 0 等于声称"消耗 0 token"。

> ⚠️ **严格 SDK 客户端注意**：官方 Java/Go SDK 对 unknown field 是**报错**。若你的客户端
> 不接受 `degradations` 扩展，请在网关上做响应字段过滤（或告知本服务关闭该键 —— 待实现开关）。

### 3.2 状态语义

| status | 语义 | 终态 |
|---|---|---|
| `queued` | **已受理但还没递交给上游**（账号全忙/冷却/额度用尽 ⇒ 排队等窗口；详见 §3.3） | 否 |
| `running` | 上游生成中（实测 t2v/i2v ≈105s 出片） | 否 |
| `succeeded` | 成功（`content.video_url` 可用） | 是 |
| `failed` | 失败/上游任务不存在/零产物/排队超时或超次数（`error.message` 会写明"未提交、未消耗额度"） | 是 |
| `expired` | 超过 `TASK_TIMEOUT`（默认 900s）仍未终态 | 是 |

**查询是"惰性回查"**：调用方每 GET 一次，本服务推进该任务一格（`queued` ⇒ 尝试提交；
`running` ⇒ 回查上游一次）；终态后不再打扰上游。
（后台协调器 `COORDINATOR_ENABLED=1`，**默认开**，无人查询时也照常推进。）

### 3.3 排队 / 重试 / 重启耐久（轻量实现）

**没有引入任何独立队列组件** —— 队列就是任务表本身（`status='queued'` 的记录），
消费者是既有件：后台协调器 + 调用方的 GET。

| 关切 | 本服务行为 |
|---|---|
| 容量不足 | 落 `queued`，**立即**返回 id（不再硬 429）；由协调器 / 后续 GET 出队提交 |
| 排队深度 | ≥ `QUEUE_MAX_DEPTH`（默认 50）⇒ **429 + `Retry-After`**（背压保留，绝不无限囤积） |
| 重试范围 | **只重试"可证明上游未受理"的失败**：风控（RGV587）、额度耗尽、鉴权失效、凭据铸造失败、会话失效 |
| 不重试范围 | 含义不明的失败（上游 5xx / 超时 / 未知业务码）—— 建任务是**计费动作**，"可能已提交"的重试等于赌重复计费 ⇒ 照实回报错误，任务落 `failed` |
| 退避 | 指数退避（`QUEUE_RETRY_BASE × 2^n`，上限 600s），并受 `SUBMIT_MAX_ATTEMPTS`（默认 5）与 `TASK_TIMEOUT` 双闸门封顶；`failed` 的 `error.message` 会显式声明"未提交、未消耗额度" |
| 重启不丢 | `queued` / `running` 记录都在任务库（≥`TASK_RETENTION_DAYS`=7 天）；**账号额度计数与冷却也在 KV 里** ⇒ 新进程起来接着推进，不会把已用额度算成 0 |
| 重启重新铸造 | 上游 token **刻意不落盘**（重启重新 signin，免费；避免凭据进持久层） |
| 请求去重 | ⚠️ **不提供**：同一 `POST` 重发两次 = 两条独立任务（方舟原生同样不保证幂等）。需要去重请在调用方做 |

> 关闭队列回到严格模式：`SUBMIT_QUEUE_ENABLED=0`（容量不足 ⇒ 立即 429，与既有调用方行为一致）。

---

## 4. 错误

错误体（方舟形状）：

```json
{"error": {"code": "InvalidParameter", "message": "… Request ID: <rid>", "type": "BadRequest",
           "param": "content[0].role"}}
```

| HTTP | code | 何时 | 调用方动作 |
|---|---|---|---|
| 400 | `InvalidParameter` | 请求写错（含 content 角色/图片数量/duration<5/模型 provider 错） | 改请求 |
| 401 | `AuthenticationError` | Key 缺失/无效 | 检查 Key |
| 404 | `InvalidEndpointOrModel.NotFound` | 任务不存在**或不属于该 Key** | 检查 id / Key |
| 429 | `RateLimitExceeded` | **排队深度超限**（`QUEUE_MAX_DEPTH`）或关闭队列时的容量不足 | 按 `Retry-After` 退避 |
| 429 | `ServerOverloaded` | 上游 x5sec 风控（RGV587）；队列开启时通常不再直通（会排队换号重试，除非超次数） | **退避，勿连打**（重试会加深标记） |
| 429 | `QuotaExceeded` | 账号额度耗尽（3 次/天/账号，UTC 日重置）；队列开启时通常转成排队 | 等跨日或换渠道 |
| 502 | `InternalServiceError` | 上游 5xx / 非 JSON / WAF 页（**不自动重试**，防重复计费） | 退避重试 |
| 503 | `CredentialUnavailable` | 本服务**凭据铸造/续期失败**（部署问题，非调用方错） | 联系运维（检查 `QWEN_SIGNIN_SOCKS` / `QWEN_TOKEN_URL`） |
| 504 | `InternalServiceError` | 上游超时 | 退避重试 |

- 失败响应带 `Retry-After` 头（当本层能给出建议等待时）；所有响应带 `x-request-id` 头
  （与 `message` 末尾的 `Request ID:` 同值，便于对账）。
- 🔴 **凭据铸造失败 = 503**（部署状态），不是 400（参数错误）—— 混淆会让调用方去改请求体。

---

## 5. 调试面：dry-run

```
POST /api/v3/contents/generations/tasks
X-Avm-Dry-Run: 1
```

跑完**完整翻译**后直接返回"将要发出的请求"（含上游 URL / 完整头（`Cookie` 打码）/ body），
**零上游调用、零任务落库**：

```json
{"dry_run": true,
 "upstream": {"method": "POST",
              "url": "https://chat.qwen.ai/api/v2/chat/completions?chat_id=<chat_id>",
              "headers": {..., "Cookie": "token=<redacted>"},
              "body": {...}},
 "degradations": ["…"]}
```

用途：部署前/改配置后**零成本**自检翻译与请求形状；也是排障第一手段。

---

## 6. 部署与环境变量（关键项）

| env | 默认 | 说明 |
|---|---|---|
| `QWEN_BASE_URL` | `https://chat.qwen.ai` | 上游 base |
| `QWEN_CHAT_MODEL` | `qwen3.7-plus` | 会话聊天模型（非"视频模型"） |
| `QWEN_ACCOUNTS` / `QWEN_ACCOUNT_PASSWORD` / `QWEN_ACCOUNTS_FILE` | — | 账号池（多账号轮换；额度 3 次/天/账号） |
| `QWEN_ACCOUNT_COOKIES[_FILE]` | 空 | 可选：每账号附加 cookie（整份 jar 或指纹 cookie）；默认只发 `token` 最小凭据 |
| `QWEN_TRUST_ENV` | `0` | 🔴 **别开**：置 1 会让使用侧读取宿主环境代理变量 ⇒ 出口变成"经代理、可能一请求一 IP"（静默行为改变）。详见 `UPSTREAM.md` §2.5 |
| `QWEN_SIGNIN_SOCKS` | 空 | **轮换 SOCKS5 出口**（signin 必须走它，直连会把出口打进 WAF 墙） |
| `QWEN_TOKEN_URL` | 空 | 或改用外部 token 服务（`GET /token?account=`，同 image-adapter） |
| `QWEN_DAILY_VIDEO_CAP` | `3` | 每账号每日视频额度（**UTC 日**窗口） |
| `QWEN_SUBMIT_MIN_INTERVAL` | `15` | 同账号提交最小间隔（防写请求突发） |
| `QWEN_SIGNIN_MIN_INTERVAL` | `45` | 跨账号共享的 signin 节奏 |
| `SUBMIT_QUEUE_ENABLED` | `1` | 轻量排队重试总开关（`0` = 严格模式：容量不足立即 429） |
| `QUEUE_MAX_DEPTH` | `50` | 排队深度上限（超限 ⇒ 429 背压） |
| `SUBMIT_MAX_ATTEMPTS` | `5` | 单任务最大提交尝试次数（超限 ⇒ `failed`，明示未消耗额度） |
| `QUEUE_RETRY_BASE` | `30` | 排队重试退避基数（秒，指数退避，上限 600s） |
| `API_KEYS` | 空 | 对外 Key（逗号分隔）；空 = 关闭鉴权（仅内网） |
| `TASK_DB` | `sqlite:///<DATA_DIR>/qwen.db` | 任务库（多实例请换 PostgreSQL） |
| `TASK_TIMEOUT` | `900` | 未终态任务的超时阈值（→ `expired`；排队中的任务同样受此闸门） |
| `TASK_RETENTION_DAYS` | `7` | 记录保留期（契约要求 ≥7 天） |
| `COORDINATOR_ENABLED` | `1` | 后台协调器（出队排队任务 + 主动回查 + 过期清理） |
| `COORDINATOR_TICK` | `5` | 协调器轮询间隔（秒） |
| `WORKERS` / `PORT` | `1` / `8400` | 单 worker 是架构约束（节奏是进程内状态） |

**运维面**：`/healthz`（存活）、`/readyz`（账号数/store/coordinator）、`/stats`
（账号池统计：**邮箱半脱敏、无 token 原文**，含冷却与今日用量；`tasks.queued` 给出排队深度）。
