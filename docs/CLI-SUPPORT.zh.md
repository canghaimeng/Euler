# CLI 与平台支持

<!-- language-nav -->
[English](CLI-SUPPORT.md) · [简体中文](CLI-SUPPORT.zh.md) · [返回 Euler](../README.zh-CN.md)
<!-- /language-nav -->

Euler 的持久团队支持 `codex`、`claude-code`（别名 `claude`）、`gemini`、`grok` 和 `cursor`（别名 `cursor-agent`）。每个团队有一名负责人，可以配置助手或审阅者；不要求必须包含 Codex。独立 worker 注册表同样支持这五种服务商。

## 在 Linux 或 macOS 上运行

运行智能体的机器需要 Python 3.10+ 和 Git。Mac 用户可直接在本机安装所选 CLI 并打开工作台，无需另备 Linux 服务器。运行 `euler-team doctor` 检查可执行程序及系统隔离能力，此命令不会启动推理。

初始化脚本为 `examples/personal-research/create_workspace.py`；`research.five-clis.json` 提供可编辑的五种 CLI 配置，从中选择需要的成员即可。

Linux 使用 Landlock ABI 3+，macOS 使用 `/usr/bin/sandbox-exec`（Seatbelt）。两者允许写入当前分支所保存的根目录，禁止通过原研究根目录读取内容或写入，包括符号链接访问。每个 CLI 还会获得用于套接字的独立短路径临时目录，退出后删除。隔离后端负责文件系统访问；网络访问遵循宿主机配置。隔离能力不可用时会报错并停止执行。安装脚本根据 Linux/macOS 和 Intel/ARM 选择 Node 安装包。

## 原生会话与恢复

| CLI | 团队通信方式 | 从已保存状态继续 |
| --- | --- | --- |
| Codex | 原生 app-server RPC | 从冻结的 rollout 调用 `thread/fork`，获得新的原生 ID |
| Claude Code | Print 模式 JSON 流 | 使用 `--resume` 和 `--fork-session`，首次明确发送消息时创建分支 |
| Gemini CLI | ACP | 将完整原生会话文件复制到独立的服务商存储，再调用 `session/load` |
| Grok | 原生 ACP | 使用原生会话分支扩展，获得新的原生 ID |
| Cursor Agent | ACP | 将完整原生会话存储复制到独立目录，再调用 `session/load` |

Gemini 和 Cursor 在各自独立存储中保留服务商会话 ID。Euler 的团队实例、文件、消息队列和后续历史彼此独立；界面称其为**隔离的原生恢复**。

有些 CLI 在首次提问时才创建会话文件，此前显示 **CLI ready**。等待首次创建分支的 Claude 快照显示 **saved conversation ready**。

会话归档不包含凭据和服务商配置。macOS 凭据可使用服务商的 Keychain；Linux 凭据仅复制到分支中被 Git 忽略的运行目录。恢复需要原生会话归档。浏览器展示公开回复；原生归档与私有推理保留在执行主机。

Codex、Claude、Grok 支持原生 effort 配置。Gemini、Cursor 使用 `effort: "model-defined"`，通过模型 ID 选择模型与受支持参数。Cursor ACP 接受服务商提供的标准变体 ID，包括方括号参数；`auto` 对应其公布的 Auto 选项。不支持的模型 ID 会产生配置错误。

## 预算与验证范围

新团队使用所选服务商支持的登录方式。账户剩余额度百分比限制是可选项；也可设置 `team.max_turns` 和单个成员的 `team.members[].max_turns`，省略则不设对应上限。这些参数统计对话轮次。Token 或金额上限请通过服务商账户控制设置。

包含 Codex 的团队可在 `euler-team init` 时通过 `quota_policy.codex_stop_percent` 主动启用已有账户额度保护。已经保存的部署策略继续生效，新项目不继承固定百分比暂停线。Grok 不设整轮固定时限，其他适配器遵守 `turn_seconds`。

适配器已针对 Codex 0.159.0、Claude Code 2.1.198、Gemini CLI 0.63.0、Grok 1.0.50 和 Cursor Agent 2026.10.01-e373342 的接口检查。Gemini 依赖 0.63 的 ACP 与项目注册表布局；缺少这些接口的旧版 CLI 需要升级。

`euler/workspace/tests/test_cli_adapters.py` 使用**本地协议模拟程序**，检查真实子进程、系统隔离、公开输出过滤、原生存储保留、分支独立性和恢复。账号访问与模型可用性在部署主机另行检查。

接口参考：[Claude CLI](https://code.claude.com/docs/en/cli-reference)、[Gemini 会话](https://geminicli.com/docs/cli/session-management/)、[Gemini 配置](https://geminicli.com/docs/reference/configuration/)、[Cursor CLI](https://cursor.com/docs/cli/overview)。开发时也检查了本机 CLI 帮助和服务商代码；Cursor 的 ACP 接口可能随版本变化。

## 运行中的研究工具

原生团队获得研究状态查询、分层证明计划、有范围的障碍记录和类型化状态更新工具。助手提交证明时经过真实的 worker MCP 网关及独立验证服务，主 Agent 和 verifier 角色不能提交 Fact。原始目标需要单独的最终审计。研究事件直接驱动现有工作台，无需事后导出图谱。

运行器可以根据团队中的 Grok、Codex 或 Cursor 成员启动回环地址验证服务；显式设置的 `EULER_VERIFY_URL` 优先。Claude Code 与 Gemini 团队也能使用该服务，但这两种 CLI 尚不是现有验证服务的冷启动后端。所选验证者需在本机完成登录。运行器优先复用所选原生成员已刷新的有效登录，凭证仍排除在里程碑之外。连续五次服务商传输重试通知会结束当前轮并报告基础设施错误；这不会给 Grok 增加整轮时间上限。

持续研究由主 Agent 针对研究请求明确启用。每轮编译当前目标的上下文，连续三轮没有研究前沿变化时暂停待检查。恢复后的分支默认不启动持续研究，用户提问与后续探索均留在该分支。
