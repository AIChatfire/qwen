"""qwen 网页端（chat.qwen.ai）异步视频接口。

端点（与视频功能相关的全部）：
    POST /api/v2/chats/new                 建会话（返回 data.id 作 chat_id）
    POST /api/v2/chat/completions?chat_id= 提交生成（chat_type 三处同标 t2v/i2v）
    GET  /api/v2/task/status/{task_id}     查询任务（HTTP 恒 200，真码在 x-actual-status-code）

上游契约的唯一真源：`docs/UPSTREAM.md`。
"""
