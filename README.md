# 钉钉消息助手

把单聊、@我和多个关注群的新消息汇集到本地面板，由 Codex 生成摘要、回复草稿和待办建议。**回复和待办需本人确认后执行**

Python 标准库 + 原生网页，无第三方 Python 运行依赖，无需构建前端。支持 macOS/Linux；macOS 可安装登录启动项。

## 启动

首次使用先按 [安装配置与启动](docs/安装配置与启动.md) 完成 CLI 安装、本人账号登录和身份查询。项目核对版本为 DWS 1.0.65、Codex 0.163.0，Python 要求 3.7+；新部署建议使用维护中的 Python 与 Node LTS。

```sh
# 仅首次创建配置；已有配置直接复用
python3 -m backend init --output config.local.json
```

填写 `profile`、本人 `self_id`（openDingTalkId）和 `display_name`；关注群可在本地面板中管理，再执行：

```sh
python3 -m backend --config config.local.json doctor
python3 -m backend --config config.local.json run
```

打开 [本地面板](http://127.0.0.1:8766/)（默认端口），前台按 Ctrl+C 停止。下次直接执行 `run`；已有后台实例时直接打开面板。交给 Agent 启动或维护时先读 [AGENTS.md](AGENTS.md)。

在“关注群”区域点击“添加关注群”，可按名称搜索并多选群；选中的群以标签显示，可单独移除。下拉列表最高显示 300px，超过时在列表内滚动；确认保存后会写入本机配置，重启服务后生效。

macOS 持续后台运行：先退出前台实例，再执行：

```sh
python3 -m backend --config config.local.json service --install
```

启动项、停止/重启、登录排错和旧数据迁移见 [安装文档](docs/安装配置与启动.md)。

## 工作方式

```mermaid
flowchart LR
    D[钉钉新消息] --> L[DWS 三路监听]
    L --> Q[本地队列与收件箱]
    Q --> A[Codex 整理]
    A --> P[面板审查]
    P --> U{本人确认}
    U --> W[DWS 发送回复或创建待办]
```

默认单聊/@我约 30 秒、普通群约 5 分钟后整理，达到 20 条提前触发。首次运行自动创建 SQLite，只接收启动后的新事件，不保证补发历史消息。订阅 `ready` 只表示订阅就绪，实际投递需看接收计数；`degraded` 表示连接异常。

每人使用自己的配置、数据和登录态。消息交给所登录的 Codex 云端模型处理；图片和附件不会自动识别。发送结果不明时保留为 `unknown`，不自动重试。

详细模块职责、进程边界、通讯协议及确认链路见 [架构与通讯链路](docs/架构说明.md)。

## 开发与分享

所有命令在项目根目录执行。开发检查使用 Python 3.8+ 的独立环境和 Node >=18.18：

```sh
python3 -m venv .venv-dev
. .venv-dev/bin/activate
python -m pip install -r requirements-dev.txt
npm ci
python scripts/check.py
```

统一检查包含 Ruff、Black、Stylelint、ESLint、Prettier 和 unittest；自动格式化用 `python scripts/check.py --fix`。运行兼容检查：`python3 -m unittest discover -s tests -v`。测试使用临时数据和本机端口，不发送真实消息或创建待办。

```sh
python3 -m backend share --output ../dingtalk-ears-share.tar.gz
```

分享包按白名单收录源码和通用文档，排除个人配置、凭据、数据库、日志、摘要和截图。不要直接分享整个工作目录。

## 例图
![样图1](./docs/images/app-overview.png)
![增加钉钉待办](./docs/images/dingtalk-todo.png)
![消息回复](./docs/images/message-reply.png)
