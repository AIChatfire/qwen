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

- token 是**无状态 JWT**（实测：`token_len=209`；签发后 27 分钟仍被接受；缓存 TTL 取 6 天，
  对照实测寿命 30 天）。
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

---

## 3. 额度

| 项 | 值 | 证据 |
|---|---|---|
| 视频额度 | **3 次/天**（`t2v` 与 `i2v` **共用**同一池） | ✅（normal 免费档） |
| 窗口 | **UTC 日**（本地日会提前 8 小时"误判恢复"） | ✅（`qwen-chat-api.md` §2.12c） |
| 查询接口 | `POST /api/v2/users/user/entitlement_quota`（只读） | ✅ |
| `times_left` | **有滞后/缓存，不是实时计数** ⇒ 只当参考，熔断以本仓账号池的计数为准 | ✅ |
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

---

## 10. 变更记录

| 日期 | 变更 |
|---|---|
| 2026-09-22 | 首次成文：整合 `reverse-proxy` / `video-adapter` 既有取证 + 用户当日抓包（i2v 完整头版）；确立"token 最小凭据 + 完整头 + 三处同标 + `wanx.task_id` 路径"四条实现依据 |
| 2026-09-22 | **真实链路首测（经本服务）**：signin ✅（token_len=209）/ t2v ✅ / i2v ✅，两条产物下载核验（均 5.042s）；U-7 关闭、U-1 部分关闭；补 §6.4 耗时波动观测与 §7.1 出片实录 |
