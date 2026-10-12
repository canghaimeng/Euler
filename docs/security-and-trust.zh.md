# Euler 的验证与访问控制

<!-- language-nav -->
[English](security-and-trust.md) · [简体中文](security-and-trust.zh.md) · [返回 Euler](../README.zh-CN.md)
<!-- /language-nav -->

本指南介绍 Euler 如何检查数学结果、控制共享记录的访问，以及运行服务商进程。

## 1. 验证证据

验证服务使用独立 LLM 会话审阅命题和证明。`correct` 表示该审阅者的判断。服务检查回复格式，以及结论、问题列表和缺口之间的一致性。LLM 审阅可能出错，准备发表结果时应进行专家审阅。

形式化流程可提供 Lean 内核检查和命题对齐证据。项目约定决定单个步骤和最终验收所需的证据；工作台显示记录的证据类型及来源。

切换验证后端会保留有效 Facts 及原始来源；需要新证据时应重新请求审阅。具体缺陷使结果失效时，使用 `fact_revoke` 及其依赖级联撤销。

## 2. Gateway 权限

MCP gateway 根据 `euler/gateway/roles.py` 分配工具：

| 角色 | 可用工具 |
| --- | --- |
| Worker | `gm_add`、`gm_search`、`fact_submit`、`fact_search`、`search_arxiv_theorems` |
| Main | `gm_add`、`gm_search`、`fact_search`、`fact_revoke`、`search_arxiv_theorems` |
| Verifier | `search_arxiv_theorems` |

Worker 通过验证门禁提交候选 Fact；主 Agent 协调研究并可撤销已接受 Fact；验证者的 gateway 权限只读。未知或未指定角色采用权限受限的 verifier 工具集；完整开发工具集需设置 `EULER_ROLE=all`。

## 3. Fact 入库

生产流程如下：

1. Worker 调用 `fact_submit`，提交命题与证明。
2. Gateway 请求验证，在收到有效的 `correct` 结论后接纳 Fact。
3. Gateway 将结果写入全局记忆，包括拒绝和写入失败。

验证服务不可用或回复格式错误时返回错误，Fact 不入库。Worker 通过 gateway 写入共享数学记录。MCP 工具缺失或 worker 配置损坏会产生 `PRECHECK_FAILED`。`/health` 标识验证服务进程，数学判定来自具体的验证请求。

## 4. 服务商进程与隔离

Agent 约定、技能和服务商配置应作为可信的可执行输入管理。自主验证应在专用主机或隔离环境中运行，限制为所需研究数据的访问权限。

Codex 验证启动器绕过其 CLI 沙箱；Cursor 验证启动器使用强制工具批准和独立 agent home。子进程环境按允许列表构建，home 中放置所选认证与验证者配置。验证者 gateway 使用 `EULER_ROLE=verifier`。

原生研究团队轮次使用 Linux Landlock 或 macOS Seatbelt 隔离文件系统。服务商进程获得配置分支工作区的访问权限，网络策略由宿主机管理。对应执行路径见 [CLI 支持](CLI-SUPPORT.zh.md)和[团队恢复](TEAM-RECOVERY.zh.md)。

## 5. 网络与凭据

验证服务默认地址为 `127.0.0.1:8091`，工作台为 `127.0.0.1:8099`。从另一台设备访问时使用 SSH 转发。只在明确配置的可信部署中绑定外部网络接口。

密钥和服务令牌放在被忽略的 `config/*.env` 或服务商登录存储中。生成的服务商配置通过环境变量名引用密钥。提交前检查账号配置和 `runtime/` 没有被跟踪。

## 6. 确定性检查与评估

LLM 审阅前的预检查会拒绝空命题、不支持的条件前提和部分格式异常的证明。适配新领域时需检查这些判定规则；预检查通过后，请求才进入审阅阶段。

软件测试覆盖角色权限、入库状态变化、内容寻址记录、恢复和撤销。数学验收遵循项目证据要求和最终审计。论文写作流程还会审阅组装后的稿件。
