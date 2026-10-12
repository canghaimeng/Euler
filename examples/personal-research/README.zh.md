# 研究工作区初始化示例

<!-- language-nav -->
[English](README.md) · [简体中文](README.zh.md) · [返回 Euler](../../README.zh-CN.md)
<!-- /language-nav -->

初始化脚本使用 Euler 的公开配置接口，将可编辑的研究定义转成可以连接团队的工作区。配置在本地完成，连接服务商会话是后续独立步骤。

```bash
python examples/personal-research/create_workspace.py --workspace ~/euler-research/euclid
```

复制并修改 `research.json`，通过 `--definition` 指定自己的问题和团队。随附定义使用欧几里得“素数有无穷多个”定理，帮助熟悉工作流。

脚本写入输入文件，并执行以下配置步骤：

```bash
EULER_AGENTS_ROOT=WORKSPACE/projects euler init ID --contract setup/contract.json --policy setup/policy.json
euler-milestones enable --project WORKSPACE/projects/ID --store WORKSPACE/history/ID
euler-team init --project WORKSPACE/projects/ID --definition setup/team.json
```

它会创建精确的问题约定、状态为 OPEN 的主目标、已注册的团队、本地里程碑存储，以及包含项目目录和策略的 `workbench/start-workbench.sh`。

`--workspace` 应指向空目录。先运行 `euler-team doctor` 检查 CLI 和系统隔离能力，登录所选服务商，再用 `euler-team prepare` 连接会话。发送第一条问题后才开始研究。完整步骤见[使用指南](../../docs/DASHBOARD-GUIDE.zh.md)。

工作区支持 macOS 和 Linux。`research.five-clis.json` 提供五种 CLI 的配置，请根据账号实际可用的成员和模型选择。
