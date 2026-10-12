# Worker 启动示例

<!-- language-nav -->
[English](workers.md) · [简体中文](workers.zh.md) · [返回 Euler](../README.zh-CN.md)
<!-- /language-nav -->

使用可移植的相对路径。在仓库根目录加载环境：

```bash
source ./scripts/env.sh
# 若当前位于 scripts/ 目录：
source ./env.sh
```

原生 worker 的 `.role` 文件通过 `PROVIDER=` 选择 `codex`、`claude-code`、`grok` 或 `cursor`。

为每种原生服务商各创建一个 worker：

```bash
euler new DEMO --roles codex:1,claude-code:1,grok:1,cursor:1
```

自定义仅含 Codex 的角色配置：

```bash
euler new DEMO --roles high:3,xhigh:6
```

默认 `euler new` 创建六名原生 worker：Codex×2、Grok×2、Claude Code×1、Cursor×1。持久团队对五种 CLI 的支持见 [CLI 与平台支持](CLI-SUPPORT.zh.md)。
