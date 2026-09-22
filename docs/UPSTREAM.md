# Qwen 网页端视频生成上游契约（t2v / i2v）

> 本文件是本仓**上游侧的唯一真源**：`app/upstream/qwen/*` 里凡与上游形态相关的判读，
> 都能在这里找到依据与证据等级。
>
> 跨项目姊妹资料（取证过程更全，引用时注明出处）：
> - `reverse-proxy/docs/upstream/qwen-chat-api.md`（鉴权 / 反爬 / 错误码 / 身份池 / RGV587 订正）
> - `reverse-proxy/docs/upstream/qwen-async-task-api.md`（异步任务端点全量）
> - `reverse-proxy/docs/upstream/qwen-quota-api.md`（额度口径）
> - `video-adapter/docs/upstreams/qwen-official-api.md`（视频侧差异核对 D-1…D-14）
>
> 证据等级：✅ **实测**（本生态发出真实请求并读到响应）／📖 **静态取证**（前端 bundle）／
> ⚠️ **未证实**（不得在实现里假设）。

---

## 0. 一句话模型

```
POST /api/v2/chats/new                        → data.id（chat_id；可长期复用）
POST /api/v2/chat/completions?chat_id=<id>    → data.messages[0].extra.wanx.task_id（同步返回）
GET  /api/v2/task/status/<task_id>            → data.task_status / data.content（产物 URL）
```

- **创建是两段式**：`completions` 的 `chat_id` 必须是**已存在**的会话（瞎填 UUID ⇒ `CHAT_NOT_FOUND`）。
- **查询 HTTP 状态码恒为 200**，真实业务码在响应头 `x-actual-status-code` 与 body 的 `success`。

---

## 1. 端点

| Method | 路径 | 用途 | 证据 |
|---|---|---|---|
| `POST` | `/api/v2/auths/signin` | 登录取 token（`Set-Cookie: token=…`） | ✅ |
| `POST` | `/api/v2/chats/new` | 建会话，返回 `data.id` | ✅ |
| `POST` | `/api/v2/chat/completions?chat_id=<id>` | 提交生成（`chat_type` 三处同标） | ✅ |
| `GET` | `/api/v2/task/status/<task_id>` | 查询任务 | ✅ |
| — | 无取消端点 | 脚本/适配层不声明取消能力（未终态 DELETE 由本服务拒绝） | ✅（证据是"不存在"） |
| `POST` | `/api/v2/users/user/entitlement_quota` | 额度查询（只读） | ✅（`times_left` 有滞后，见 §3） |

---

## 2. 鉴权与请求头

### 2.1 最小凭据 = `Cookie: token=<JWT>`

✅ 实测（`qwen-chat-api.md` §10.1，单变量）：**只给一个 `Cookie: token=<JWT>`**、其他 cookie 与
`bx-ua`/`bx-umidtoken` 一律不带，完整跑通生成链路，出图 owner 与 token 账号一致。

✅ **2026-09-22 再证（视频写端点）**：本服务用同一份最小凭据（`Cookie: token=<JWT>`，无 bx-*、
无其他 cookie）真实跑通 **t2v 与 i2v** 两条链路（见 §7.1）—— U-7 由此关闭。

- token 是**无状态 JWT**（实测：`token_len=209`；签发后 27 分钟仍被接受）。
  **载荷只有三个键：`{exp, id, last_password_change}`（没有 `iat`）**，`id` 即账号 id，
  实测 `exp` = **铸后 30 天**（2026-09-22 现铸解析：`2592000` 秒）。
- 🔴 **`exp` 只是上游"自称"，不得当真实寿命用**：服务端可能提前失效（自称 30 天、实际 7 天就被判 401
  是有先例的形态）。本仓口径（2026-09-22 起）：
  **主动**续期 = `min(exp − 提前量, 铸后 QWEN_TOKEN_TTL)`，提前量 = 生命的 10%（夹在 1 分钟 ~ 6 小时），
  `QWEN_TOKEN_TTL` 默认 **1 天**（保守上限，`0` = 不设上限）；
  **被动**兜底 = 上游判 401 ⇒ 清缓存 → **立即重铸 → 原请求重试一次**（写端点同样重试：
  401 = 上游未受理，不会重复计费）。两条路都在 `app/upstream/qwen/accounts.py` + `app/service.py::_authed_call`。
- token 由 `POST /api/v2/auths/signin` 铸造：body `{"email", "password": sha256hex(password)}`，
  token **只在 `Set-Cookie`**（body 是账号记录，没有 token）。✅ 2026-09-22 经池复现（4.3s/账号）。
- 🔴 **signin 有 IP 级频率墙**：同出口几秒内连登多个账号 ⇒ `aliyun_waf` 挑战页 ⇒
  **必须走轮换出口**（本仓 `QWEN_SIGNIN_SOCKS` / `QWEN_TOKEN_URL`，见 `.env.example`）。
  签发与使用分离是安全的（token 无状态）：**登录走轮换出口、出片走正常出口**。

### 2.2 请求头（照 `biz-api::build_headers` 逐字段对齐）

```
Accept: application/json
Content-Type: application/json
User-Agent: <Chrome macOS>
Origin: https://chat.qwen.ai          Referer: https://chat.qwen.ai/（或 /c/<chat_id>）
source: web
version: 0.2.0                        ← 🔴 写端点硬门槛，见 §2.3
Accept-Language: zh-CN,zh;q=0.9       Connection: keep-alive
Sec-Fetch-Dest: empty   Sec-Fetch-Mode: cors   Sec-Fetch-Site: same-origin
Timezone: <本地时间串，如 Tue Sep 22 2026 04:13:35 GMT+0800>
X-Request-Id: <uuid4>                 X-Accel-Buffering: no
sec-ch-ua / sec-ch-ua-mobile / sec-ch-ua-platform
Cookie: token=<JWT>
```

✅ 2026-09-22 实测：该头集（不含 `bx-*`）连续完成 signin / chats/new / completions / task/status
全部 200，**零 RGV587**。

### 2.3 `version: 0.2.0` 是写端点的硬门槛

✅ 六格对照实测（`qwen-async-task-api.md` §7.3 / §11）：缺该头 ⇒ HTTP 200 +
`{"success":false,"code":"Bad_Request","details":"…"}`（**文案像"请求体写错了"，极易误诊**）；
补上即正常。`chats/new` **不要求**该头。判据看响应形态，不要读文案。

### 2.4 🔴 RGV587 风控的根因订正（2026-09-19）

`RGV587_ERROR::SM`（x5sec 挑战）曾被认为是"累计写请求门禁"。**订正**：
2026-09-18 下午那批 RGV587 的根因是**探针自身请求头不全**（缺
`Sec-Fetch-*` / `Timezone` / `Connection` / `X-Accel-Buffering`，且 `Accept` 形态不对）——
**与账号、出口 IP、累计写计数无关**；换上 `biz-api::build_headers` 同款头后同一账号同一出口
连续 3 次成功（`qwen-chat-api.md` §10.19）。

⇒ 本仓纪律：**见到 RGV587，第一步把请求头与本文件 §2.2 逐字段对齐**，再谈限速/换号。
处置上仍按 429 + `Retry-After` 退避（不自动重试，避免加深标记）。

### 2.5 出口拓扑：哪个请求走哪个 IP（2026-09-22 代码核对）

两扇门**刻意解耦**——`token` 是**无状态 JWT**，铸造 IP 与使用 IP 不需要一致（09-22 全链路实测已证）。

| 路径 | 出口 | 跳不跳 |
|---|---|---|
| **使用侧**：`chats/new` / `chat/completions` / `task/status` / 产物下载 | **服务宿主机直连**（`QwenClient` 不配 proxy，`trust_env` 默认 `False`） | **零跳转**：同一次任务全程同一 IP；**所有账号共用这一个 IP**（无"每号一 IP"能力） |
| **铸造侧**：signin 铸 token | `QWEN_SIGNIN_SOCKS`（当前 `.env` = `pool.livetest.cn:2088` = **每连接换 IP**） | **会跳**，且刻意如此（直连登录会把出口打进 WAF 墙，`qwen-chat-api.md` §2.6） |
| 兜底：`QWEN_TOKEN_URL` | 外部 token 服务 | 由该服务决定（`accounts.py` 亦显式 `trust_env=False`） |

⚠️ **一处细节（代码层）**：`mint_token` 为一次铸造开**两条连接**（① 预热 `GET /auth`、② 正式 `POST /auths/signin`）
⇒ 用 2088 时**同一次铸造的预热与登录可能落在两个 IP**。要"一次铸造内也不跳"，把出口换成**粘性**那条（2089）即可，**代码零改动**。
⚠️ 跨账号共享 signin 节奏（`QWEN_SIGNIN_MIN_INTERVAL=45s`）是**防连登把出口打进墙**，与 IP 轮换策略无关。

🔴 **危险开关 `QWEN_TRUST_ENV`**：置 `1` 会让使用侧也去读宿主环境的 `HTTP(S)_PROXY`/`ALL_PROXY`
（本机/沙箱环境**确有**这些变量）⇒ 使用侧会变成**经环境代理、可能一请求一 IP**，且是**静默**行为改变。
**默认 `False`（不带此变量）是正确姿势**，别开。

🔴 **"不跳"的代价 = 绑死一个出口**：该 IP 若被整域拦（405 / WAF 挑战页）**不会自愈** ——
只能换机或加 SNAT 固定干净出口（image-adapter 在 node-064 上踩过：默认 IP 405、须挑干净 IP + 策略路由）。
因此**部署前先验宿主出口**：`curl -sS -o /dev/null -w "%{http_code}\n" https://chat.qwen.ai/api/models`（200 = 干净）。

> ⚠️ 尺度提醒：7 账号 × 3 次/天**共用一个出口**已被实测证实可用（09-22 两账号两任务同 IP 连续成功）；
> 但账号数/频率继续上量时，"全账号同 IP"是一个相关性风险（上游收紧时会一起收紧）——届时再考虑 per-account 出口（当前**无此能力**）。

### 2.6 鉴权形态 × 视频矩阵（2026-09-22 一手实测）

四形态构造**照图片侧口径逐字段照抄**（`reverse-proxy/qwen/probe/probe_auth_forms_matrix.py` 的
`http_t2i.headers` / `anon_headers` / `ident_pool.build_headers`）；探针 `scripts/probe_auth_forms_video.py`
（两段式，与图片侧判据结构一致）。

| 鉴权形态 | ① `chats/new`（免费段，`chat_type=t2v`） | ② `chat/completions`（写端点，`stream:false`） | 判决 |
|---|---|---|---|
| **`cookie`**：`Cookie: token=<JWT>` | `200` | **`200`** ⇒ 回 `task_id`，出片 | ✅ **能出视频** |
| **`bearer`**：`Authorization: Bearer <JWT>`（**不带任何 cookie**） | `200` | 🔴 **RGV587 风控 + x5sec** | ❌ 写端点被拒 |
| **`guest`**：设备指纹三件套（无 `token`），`chat_mode=guest` | `200` | 🔴 **`400 internal_error`**（单发实测，见 §9.2） | ❌ 会话能建、**生成被拒** |
| **`anon`**：完全未登录（无 Cookie / Authorization / `bx-*`） | 🔴 **`401 Unauthorized`** | —（会话都建不了） | ❌ |

🔴 **本条最可复用的判读：免费段不能当鉴权判据。** `cookie` / `bearer` / `guest` 三种形态在
`chats/new` 上**全回 200**，差异**全部**出现在写端点 —— 图片侧早已记过该结论
（`qwen-chat-api.md` §2.13 第 2 条的单变量对照），本次在**视频侧一手复现**。
⇒ 任何"某凭据形态能用"的结论，必须打在写端点上；只测 `chats/new` 会得出三种形态都行的错判。

**`cookie` 格的三道自证（全绿）**：
1. **归属**：产物 URL 的 `/output/<uuid>/` 段 = `3bc78ba9…` = JWT 载荷里的账号 id（一致）；
2. **产物**：下载 **5,487,103 bytes**，容器 `mvhd` 时长 **5.042s**（与既有 n≥5 实测一致）；
3. **凭据形态**：仅 `Cookie: token=<JWT>`（无其它 cookie）+ `chat_mode=normal`，全链路零 RGV587。

⚠️ **口径偏差（如实标注）**：图片侧要求「一格一号」（6 格 6 个号）；本次只有**一个账号** ⇒
`cookie` 与 `bearer` 两格落在同一个号上（中间**强制冷却 90s**）。要严格版请给足账号，探针支持 `--forms` 逐格指定。


---

## 3. 额度

| 项 | 值 | 证据 |
|---|---|---|
| 视频额度 | **3 次/天**（`t2v` 与 `i2v` **共用**同一池） | ✅（normal 免费档） |
| 窗口 | **UTC 日**（本地日会提前 8 小时"误判恢复"） | ✅（`qwen-chat-api.md` §2.12c） |
| 查询接口 | `POST /api/v2/users/user/entitlement_quota`（只读） | ✅ |
| `times_left` | **有滞后/缓存，不是实时计数** ⇒ 只当参考，熔断以本仓账号池的计数为准 | ✅（09-22 复证：成功出片后 5 分钟与 10 分钟两次读 `t2v` 仍为 `3`；`t2i` 同为 `3`） |
| 风控拦截时 | **不扣额度**（实测：被 RGV587 拦后 `t2v.times_left` 仍为 3） | ✅ |
| "提交即扣 vs 成功才扣" | ⚠️ 未证实 | — |

---

## 4. 创建请求体（实测形态）

```jsonc
{
  "stream": false, "version": "2.1", "incremental_output": true,
  "chatId": "<chat_id>", "chat_id": "<chat_id>",
  "parentId": "", "parent_id": null, "chat_mode": "normal",
  "model": "qwen3.7-plus",
  "messages": [{
    "id": null, "fid": "<uuid>", "parentId": null, "childrenIds": ["<uuid>"],
    "role": "user", "content": "<prompt>", "user_action": "chat",
    "files": [ /* 仅 i2v，见 §4.2 */ ],
    "timestamp": <epoch 秒>, "models": ["qwen3.7-plus"], "model": "",
    "chat_type": "i2v",
    "feature_config": {"thinking_enabled": false, "output_schema": "phase",
                       "research_mode": "normal", "auto_thinking": false,
                       "thinking_mode": "Fast", "auto_search": true},
    "extra": {"meta": {"subChatType": "i2v", "size": "16:9"}},
    "sub_chat_type": "i2v", "parent_id": null
  }],
  "timestamp": <epoch 秒>, "size": "16:9"
}
```

### 4.1 t2v / i2v 判定：三处必须同时标

| 位置 | t2v | i2v |
|---|---|---|
| `messages[0].chat_type` | `"t2v"` | `"i2v"` |
| `messages[0].sub_chat_type` | `"t2v"` | `"i2v"` |
| `messages[0].extra.meta.subChatType` | `"t2v"` | `"i2v"` |

`size` 出现在**两处**（顶层与 `extra.meta.size`），必须同值。比例枚举（UI 截图确认）：
`1:1` / `3:4` / `4:3` / `16:9` / `9:16`；**上游只吃这 5 个**。

### 4.2 `files[0]`（仅 i2v —— 形状依据 2026-09-22 用户抓包）

```jsonc
{"type": "image", "name": "example.png", "file_type": "image/png",
 "showType": "image", "status": "uploaded", "file_class": "vision",
 "url": "<上游认识的图片 URL>"}
```

- 🔴 **上游的 i2v 是"引用"而非"上传"**：抓包里 `url` 指向上游已有资源。
  ✅ **2026-09-22 实测**：用户抓包里的样例图（`qwen-chat.oss-ap-southeast-1.aliyuncs.com/
  resources/i2v/…png`）作首帧，经本服务**真实出片**（§7.1 i2v 行）。
- ⚠️ **第三方域名的外链图仍未验证** ⇒ 本服务照发 + 降级告警（`app/media.py::host_warning`）。
- 自有图正解（已端到端验证过出片）：`POST /api/v2/files/getstsToken` → OSS V4 PUT → 得 `file_url`
  （⚠️ 签名仅 **300s**，缓存/排队会静默失效）。本仓 v1 **不实现上传链路**，`data:` URI 明确 400。
- 早期抓包版本里曾有 `isQuote: true` 等字段；2026-09-22 抓包**没有**这些键 ⇒ 本仓按**最小集**发。

### 4.3 不接受的字段

`duration` / `resolution` / `seed` / `watermark` / `camera_fixed` / `generate_audio` / `frames`
**在上游请求体里不存在**（发了也无效，且可能触发校验拒绝）—— 见 §8。

---

## 5. 创建响应（`stream:false` ⇒ 同步返回 task_id）

```json
{"success": true, "request_id": "…",
 "data": {"chat_id": "…", "parent_id": "…", "message_id": "…",
          "messages": [{"role": "assistant", "content": "",
                        "extra": {"wanx": {"task_id": "e6b0a76d-…"}},
                        "done": false, "size": "16:9"}]}}
```

🔴 **task_id 路径 = `data.messages[0].extra.wanx.task_id`**（✅ 实测；与前端
`msg?.extra?.wanx?.task_id` 读法逐字一致）。缺这个路径 ⇒ 必须响亮失败，**不得猜别的字段**。

`success: false` 时 `data = {"code": "Bad_Request" | "Not_Found" | "Unauthorized" | …, "details": "…"}`。

---

## 6. 查询与状态

### 6.1 HTTP 恒 200，真码在响应头

| 形态 | HTTP | `x-actual-status-code` | body |
|---|---|---|---|
| 成功 | 200 | `200` | `{"success":true,"data":{…}}` |
| 任务不存在 | 200 | `404` | `{"success":false,"data":{"code":"Not_Found","details":"Task not found"}}` |
| 未鉴权 | 200 | `401` | `{"success":false,"data":{"code":"Unauthorized","details":"…"}}` |

### 6.2 成功态 `data`

```json
{"chat_type": "i2v", "task_status": "success", "message": "",
 "content": "https://cdn.qwenlm.ai/output/<uid>/i2v/<chat>/<task_id>.mp4?key=<签名JWT>",
 "remaining_time": "", "sub_chat_type": null, "duration": null}
```

`content` **仅成功时有值**；带签名 `key` 的 URL 直接可下载（✅ 实测多次）。`duration` **恒为 null**
（上游不回传时长，不要用它）。

### 6.3 状态枚举与映射

| 上游 `task_status` | → 本服务六态 | 证据 |
|---|---|---|
| `running` | `running` | ✅（提交后立即 running，**没有 queued**） |
| `success` | `succeeded`（同时给 `content.video_url`） | ✅ |
| 其它任何值 | `failed` | 📖（前端 `else → handleTaskError`） |

🔴 **未知取值一律 `failed`**，禁止默认成"还在跑"（否则上游改枚举时会静默挂死）。
上游没有 `queued` / `expired` / `cancelled` 三态；`expired` 由本服务看门狗按 `TASK_TIMEOUT` 产生。

### 6.4 轮询节奏（📖 前端 bundle + ✅ 实测）

`i2v` 3s / 其它 10s；单次 setTimeout 递归；网络异常重试上限 i2v 5 次 / 其它 10 次；
**上游没有总超时** ⇒ 由本服务 `TASK_TIMEOUT`（默认 900s）兜底。

**出片耗时实测波动大**：约 93s ～ 343s（同一天内：i2v 93s / t2v 343s；09-17 记录为 105s）
⇒ **不要用固定耗时做超时假设**；本服务默认 900s 留足余量。

---

## 7. 产物

| 维度 | 实测值 | 说明 |
|---|---|---|
| 时长 | **5.042s**（n≥7，`mvhd` 全为 `timescale=1000, duration=5042`） | **上游固定，不可指定** |
| 分辨率 / 比例 | 1920×1080（16:9）/ 1440×1440（1:1）/ … | 比例由 `size` 决定；**像素不可指定** |
| 文件大小 | 3.2 – 14.3 MB | — |
| URL 有效期 | ⚠️ 未证实（签名 JWT；`resource_chat_id` 为 null） | 决定是否需要转存 |

### 7.1 真实出片实录（2026-09-22，经本服务全链路）

| 任务 | 形态 | 账号 | 耗时 | 产物 | 时长 |
|---|---|---|---|---|---|
| `cgt-20260922012530-tyoa9` | t2v（16:9） | `2xx***@…` | ≈343s | 5.50 MB | **5.042s** |
| `cgt-20260922013158-ae0tq` | i2v（16:9，上游样例图作首帧） | `yek***@…` | ≈93s | 9.59 MB | **5.042s** |

- 两条链路的 upstream task id 分别为 `993a8bf4…` / `75a74158…`（存在本层任务记录，不进对外响应）。
- **账号轮换实证**：两条产物 URL 里的 `resource_user_id` 不同（`3bc78ba9…` vs `e4fe05b6…`）
  ⇒ 两次生成落在两个不同账号，各消耗 1/3 日额度。
- 全链路零 RGV587、零告警；signin → `chats/new` → `completions` → `task/status` 全部 200。

---

## 8. 与目标契约（方舟 Seedance）的差异核对

| # | 方舟契约 | 本上游 | 本层处置 |
|---|---|---|---|
| D-1 | `duration` 2–12…/`-1` 自选 | **不存在该参数，固定 ~5s** | 允许集 = {5}；>5 向下吸附 + 告警；<5 **400**；`-1` 替换成 5 + 告警 |
| D-2 | `ratio` 支持 `adaptive` | 只吃 5 个枚举值 | 🔴 枚举外（含 `adaptive` / 缺失 / 写错）**一律落 1:1 + 告警**（用户 2026-09-17 冻结口径，刻意偏离"缺省 16:9"） |
| D-3 | `resolution` 1080p/720p/4k | 不可指定 | 进 `degradations`；**不回填** |
| D-4/D-5/D-6 | `seed` / `watermark` / `generate_audio` | 不支持 | `degradations`（`seed=-1` 视为"没给"，不报） |
| D-7 | `camera_fixed` / `frames` / `service_tier` / `draft` / `return_last_frame` | 不支持 | `degradations`（`return_last_frame` 另注明"连续拼接链路会断"） |
| D-8 | `content[]` 六类 | 只吃 `text` +（i2v）**一张首帧图** | 见 `app/ark.py` 降维矩阵：last_frame / reference_image / video_url / audio_url / draft_task 一律 **400** |
| D-9 | `DELETE` 取消 | **无取消端点** | 本服务不实现 DELETE（路由不存在；未终态也不会伪造 `cancelled`） |
| D-10 | 创建响应 `{id}` + `cgt-` 前缀 | 上游给裸 UUID | **本地任务 id 由本服务生成**（`cgt-…`），上游 id 只存本层记录 |
| D-11 | 查询无参数（凭证即身份） | **查询带路径参数** | 任务归属校验落在本层（`credential_id` 指纹；不符本地 404） |
| D-12 | 回调 | 上游无 webhook | 本服务 v1 不实现回调（`callback_url` 进 `degradations`） |
| D-13 | `execution_expires_after` | 无 | 由本服务 `TASK_TIMEOUT` 兜 |
| D-14 | `model` = provider/model | **上游没有"视频模型名"**：能力由 `chat_type` 决定 | `model` 段只做 provider 校验（必须 `qwen`），**不转发**；聊天模型由配置 `QWEN_CHAT_MODEL` 指定 |

---

## 9. 未证实项（不得在实现里假设）

| # | 项 | 状态 | 说明 |
|---|---|---|---|
| U-1 | 外链图作 i2v 首帧 | 🟡 **部分关闭**（2026-09-22） | **上游域内图（OSS `resources/i2v/…`）✅ 实测出片**；**第三方域名外链仍未验证**（本服务照发 + 告警） |
| U-2 | `task_status` 的失败值形态（`failed`？`failure`？） | ⚠️ 未证实 | 需观测一个真实失败任务 |
| U-3 | `bx-*` 是否任何情况都不必需 | ⚠️ 未证实（当前不发送、连续成功） | 上游收紧时需补 |
| U-4 | 额度耗尽的**真实错误形态**（视频档） | ⚠️ 未证实 | 打满一个账号的 3 次后观察 |
| U-5 | 产物 URL 的**有效期** | ⚠️ 未证实 | 定时 ping 一个产物 URL |
| U-6 | 额度是"提交即扣"还是"成功才扣" | ⚠️ 未证实 | 对照实验：提交后立刻查额度 |
| U-7 | 登录态 token 直接跑**视频**写端点 | ✅ **关闭（2026-09-22）** | `Cookie: token=<JWT>` 最小凭据跑通 t2v + i2v 真实出片（§7.1） |
| U-8 | 同一 `chat_id` 上并发提交多个视频任务 | 🟡 保守规避 | 当前按账号串行 + 提交最小间隔（实践稳定）；并发未验 |
| U-9 | **guest（匿名访客身份）能否提交视频任务** | ✅ **关闭（2026-09-22）：不支持** | 单发实测：免费段 `chats/new`（`chat_mode=guest` + `chat_type=t2v`）→ **200 受理**；真实一发 t2v 提交 → **`x-actual-status-code: 400` + `code=internal_error`**（无 task_id、未扣额度）。既非额度拒绝（会回 `RateLimited`+额度文案）也非凭据问题（会 401/RGV587）⇒ **guest 门接受会话但拒绝视频生成**。过程见 §9.2 |

### 9.1 guest 通路的事实边界（2026-09-22 盘点，来源：既有取证，非新实验）

| 事实 | 依据 |
|---|---|
| guest = **设备指纹身份**（cookie 无 `token`；`bx-ua` / `bx-umidtoken` / `ssxmod_itna` 等），由真浏览器铸造 | `qwen-chat-api.md` §2.6 |
| guest 的**图片/文本**写端点已验证可用（4 鉴权 × 5 形态矩阵中 guest **5/5**，含 2.0/3.0-pro/16:9 与 t2t） | 同上 §2.13 |
| guest **读不到额度视图**（`entitlement_quota` → `x-actual-status-code: 401`）；额度墙文案为「今日**生图**额度已用完，登录后可继续生图。」 | 同上 §2.6/§2.7 |
| guest 额度**绑设备身份**（与出口 IP 无关），单身份约 4~5 张/天，且额度数额随模型不同 | 同上 §2.12 |
| **完全未登录（anon）不可用**：3/3 在 `chats/new` 即 401 ⇒ 匿名必须走 guest 身份池 | 同上 §2.13 |
| 视频写端点要求 `token` **cookie**（仅 `Authorization: Bearer` 会落 x5sec 惩罚流）；视频查询端点无凭据 → 401 | `qwen-async-task-api.md` §2.3 / `qwen-chat-api.md` §2.13 |
| 视频额度项 `t2v`（3/天，t2v+i2v 共用）**只出现在登录态**的额度视图里 | `qwen-quota-api.md` §3 |
| 上游 guest 通路属「抓一次包用一阵」形态（`ssxmod_itna` 无生成器），**不宜作无人值守生产凭据** | `qwen-chat-api.md` §2.6 |

**结论（供决策，不是实现依据）**：guest 能到达**同一个**写端点（图片已证），故"能不能发出 t2v 请求"机械上大概率可以；
但 guest 档位的产品语义是**生图**（额度文案与不可读视图都指向此），视频额度大概率**不在 guest 档位** ⇒
预期结果是同类 `RateLimited` 拒绝。**要定论必须实测**，路径见 §9.2。

### 9.2 guest × 视频：判定实验与结果（2026-09-22 已执行，**单发**）

探针：`scripts/probe_guest_video.py`（单发写死在代码里：不重试、不换身份、不打印凭据；须显式 `--confirm` 才真发）。
身份：用 `reverse-proxy/qwen/tools/make_identities.py 1` **现铸一条全新 guest 身份**
（Playwright + 系统 Chrome，7.4s，`bx-ua` 版本 `234!` 与当前 fireye 对齐）—— 用新身份是为了**排除"身份过期"这个混淆项**。

| 步骤 | 请求 | 结果 |
|---|---|---|
| ① 零成本 · UI 取证 | Playwright 载入 `/c/guest` | 🔴 **直接跳转 `https://chat.qwen.ai/auth`**（登录/注册页）；页面无「视频」「图像生成」等任何生成入口 ⇒ 访客 **UI 入口已不存在**（截图：`var/probe/20260922_guest_redirect_to_auth.png`） |
| ② 免费段 · 会话取证 | `POST /api/v2/chats/new`（`chat_mode=guest`、`chat_type=t2v`） | ✅ **200 + `success=true`**，回 `chat_id` ⇒ 身份有效、指纹通过、无 WAF/RGV587；**API 层的 guest 门接受 t2v 会话** |
| ③ **真实一发** | `POST /api/v2/chat/completions?chat_id=…`（`stream:false`） | 🔴 **`x-actual-status-code: 400`**、`success=false`、`code=internal_error`、`details=Internal Error`；**无 task_id** |

**判决**：guest × 视频 = **不支持**。三项证据互不冲突：API 门收会话（②）但拒生成（③），UI 层则干脆把访客入口撤了（①）。

**边界与注意**：
- 🔴 这不是"额度不够"：额度拒绝的形态是 `code=RateLimited` + 「今日…额度已用完」文案（`qwen-chat-api.md` §2.12）；
  也不是"参数写错"：缺 `version` 头的形态是 `Bad_Request`（`qwen-async-task-api.md` §7.3），而本次该头已带。
  `internal_error` 是上游在"这条路走不通"时给的**无信息量错误码**（同类已知用法：`3.0-pro` 传超大 `size` 也回它）。
- **未扣额度**：无 task_id、无产物，`t2v` 计数不变；单发即停（遵守 `R-2`：写端点勿连打）。
- ⚠️ **未验证（不得推断）**：**图片侧的 guest 通路今天是否仍可用**。历史实证是 09-18/19（矩阵 5/5、批量出图工具），
  而本次 ① 显示访客 UI 已被重定向到登录页 ⇒ 存在"guest 通路整体收紧"的可能。
  要定论只需**一发 t2i**（会消耗一条 guest 身份 1 张图额度）；在那之前，**不得**把"guest 出图仍可用"当现状。
- 本服务**不受影响**：本服务凭据恒为账号 token（`chat_mode="normal"`），与 guest 门无关（§2.1）。



---

## 10. 变更记录

| 日期 | 变更 |
|---|---|
| 2026-09-22 | 首次成文：整合 `reverse-proxy` / `video-adapter` 既有取证 + 用户当日抓包（i2v 完整头版）；确立"token 最小凭据 + 完整头 + 三处同标 + `wanx.task_id` 路径"四条实现依据 |
| 2026-09-22 | **真实链路首测（经本服务）**：signin ✅（token_len=209）/ t2v ✅ / i2v ✅，两条产物下载核验（均 5.042s）；U-7 关闭、U-1 部分关闭；补 §6.4 耗时波动观测与 §7.1 出片实录 |
| 2026-09-22 | 登记 **U-9（guest × 视频）** 并补 §9.1 事实边界 / §9.2 分级判定实验：guest 在图片面已证可用、视频面**零证据**；澄清"本服务无 guest 通路"（凭据恒为账号 token、`chat_mode` 固定 `normal`） |
| 2026-09-22 | **U-9 单发实测关闭**：现铸全新 guest 身份 → `chats/new(guest,t2v)` 200 / 真实一发 t2v 提交 **400 `internal_error`**（未扣额度）⇒ **guest 不支持视频**；同时观测到 `/c/guest` **已重定向 `/auth`**（访客 UI 入口消失）。新增探针 `scripts/probe_guest_video.py`（单发/不重试/不打印凭据）。⚠️ 登记新未决：**图片侧 guest 通路今日是否仍可用**（需一发 t2i 才能定论） |
| 2026-09-22 | 补 **§2.5 出口拓扑**（代码核对）：使用侧**零代理、单出口、零跳转**（所有账号共用宿主机 IP）；铸造侧走 2088 **每连接换 IP**，且一次铸造开两条连接（预热/登录**可能换 IP**，要稳定换 2089）；🔴 记 `QWEN_TRUST_ENV=1` 的危险（会静默把使用侧变成经环境代理）；部署前须验宿主出口（405 判据） |
| 2026-09-22 | 补 **§2.6 鉴权形态 × 视频矩阵**（一手实测，4×2 格）：`cookie` ✅ 出片（含归属/产物/凭据三道自证）、`bearer` 🔴 RGV587、`guest` 🔴 `internal_error`、`anon` 🔴 `401`；**结论：不登录（含访客身份）都出不了视频**。同时固化"**免费段不能当鉴权判据**"（三形态在 `chats/new` 全 200，差异只在写端点）。新增探针 `scripts/probe_auth_forms_video.py`（两段式、单号冷却、命中风控即停账号写）。§3 的 `times_left` 行补 09-22 复证 |
