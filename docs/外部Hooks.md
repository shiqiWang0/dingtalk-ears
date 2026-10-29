# 外部 Hooks

单聊或 @我 的消息经 Codex 整理出摘要和建议回复后，可以把结果 POST 给外部服务（自动化、通知或另一个 agent）；外部服务也可以回调本机接口，代你发送回复或把消息标记为已处理。默认关闭。

## 配置

在 config.local.json 增加 `hooks`，或在面板左侧「Hooks 设置」里填写后保存（只改写配置文件里的 `hooks` 一节，文件权限保持 600）：

```json
{
  "hooks": {
    "enabled": false,
    "url": "http://127.0.0.1:9000/ears",
    "secret": "至少 16 个字符的随机串",
    "filter": { "sources": ["direct", "at"], "needs_reply_only": false },
    "timeout_seconds": 5,
    "retries": 2
  }
}
```

| 字段 | 规则 |
| --- | --- |
| enabled | 开启时必须有 url、secret 和至少一个来源 |
| url | http 或 https 地址 |
| secret | 16–256 个字符；用于签名出站请求和校验回调。面板不回显，留空保存表示保持原值 |
| filter.sources | `direct` 单聊、`at` @我、`ued` 关注群，任一命中即投递 |
| filter.needs_reply_only | 为 true 时只投递 AI 判断需要回复的消息 |
| timeout_seconds | 单次请求超时，1–30 |
| retries | 失败后的重试次数，0–5，间隔 1s、2s、4s… |

secret 属于凭据，config.local.json 不要分享。面板里的「发送测试」会用表单当前值（未保存也可以）投递一条 `hook.test` 事件，并显示 HTTP 状态。

## 出站请求

每个会话批次整理完成、生成草稿后投递一次，以批次最新一条消息为主。投递在后台线程进行，超时、重试和失败都不影响监听与整理；失败写入 stderr：`[hooks] … 投递失败 n/N …`，日志不包含 secret。只有 2xx 算成功。

请求头：

| 头 | 含义 |
| --- | --- |
| X-Ears-Event | `message.triaged` 或 `hook.test` |
| X-Ears-Delivery | 本次投递 ID，重试时不变，可用于去重 |
| X-Ears-Timestamp | Unix 秒 |
| X-Ears-Signature | `sha256=` + HMAC-SHA256(secret, `<timestamp>.<原始 body>`) 的十六进制 |

示例 body：

```json
{
  "event": "message.triaged",
  "delivery_id": "3f0c…",
  "timestamp": "2026-10-29 17:03:30 +08:00",
  "message": {
    "id": "fixture-direct:same-message",
    "message_id": "same-message",
    "sender": "测试同事",
    "sender_id": "other-person",
    "conversation_id": "fixture-direct",
    "sources": ["direct", "at"],
    "text": "合成消息，请查收",
    "time": "2026-10-29 17:03:28 +08:00"
  },
  "batch": [],
  "classification": { "sources": ["direct", "at"], "kind": "direct", "needs_reply": true },
  "summary": "收到合成消息",
  "suggested_reply": "收到",
  "draft": { "id": 12, "version": 1 },
  "actions": {
    "reply": "/messages/fixture-direct%3Asame-message/reply",
    "dismiss": "/messages/fixture-direct%3Asame-message/dismiss"
  }
}
```

`batch` 列出同一批次的所有消息，字段与 `message` 相同。接收方校验签名示例（Python）：

```python
expected = "sha256=" + hmac.new(secret.encode(), ts.encode() + b"." + body, hashlib.sha256).hexdigest()
ok = hmac.compare_digest(expected, request.headers["X-Ears-Signature"]) and abs(time.time() - int(ts)) <= 300
```

## 回调接口

面板只监听 127.0.0.1，回调方需在本机运行。请求必须开启 hooks，并用同一 secret、同样的签名方式签名请求 body（时间戳误差 300 秒内），不能带 Origin 头；面板自身则使用面板令牌。

- `POST /messages/<id>/reply`，body `{"text": "回复内容"}`：复用面板的发送链路（`dws chat +messages-send`，幂等键防重复）。消息已处理、已有发送记录（发送中、结果不明或已送达）、对方已有更新消息时返回 409，不会重复发送。成功受理返回 202，实际发送结果在面板中查看。
- `POST /messages/<id>/dismiss`，body `{}`：把消息标记为已处理，返回 202。

`<id>` 使用出站 payload 里 `actions` 给出的路径即可。
