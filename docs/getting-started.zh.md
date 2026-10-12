# Euler 部署入门

<!-- language-nav -->
[English](getting-started.md) · [简体中文](getting-started.zh.md) · [返回 Euler](../README.zh-CN.md)
<!-- /language-nav -->

当前个人团队、可视化与状态恢复流程请先看[使用指南](DASHBOARD-GUIDE.zh.md)。本页介绍底层 worker 与验证服务的部署。

本流程将 Euler 部署为自包含运行环境，并完成启动前检查。先阅读[基本概念](concepts.zh.md)，配置后参考[操作指南](operating-guide.zh.md)开展首个项目。

## 主机准备

- 允许自主 Agent 工作的 Linux 主机，建议使用专用虚拟机、容器或 pod；见[验证与访问控制](security-and-trust.zh.md)。Mac 上的个人团队配置见[使用指南](DASHBOARD-GUIDE.zh.md)。
- `git`、Python 3.10+、`curl`、`tar`、`bash`。`bootstrap.sh` 将 Node、虚拟环境和 Codex CLI 安装到 `runtime/`，不作全局安装。
- 登录所选组件使用的 CLI。默认验证后端为 Grok Build；Codex 写作器或旧 worker 需要 OpenAI 兼容 API Key 或支持的 ChatGPT 订阅登录。
- 仅论文 PDF 写作需要 PATH 上的 LaTeX 引擎，例如 `pdflatex`，或通过 `scripts/install-tex.sh` 安装 `tectonic`；证明研究本身不要求 TeX。

## 1. 安装运行环境

```bash
bash scripts/bootstrap.sh
```

脚本可重复执行，内容安装到被 Git 忽略的 `runtime/`：

- Node 22 → `runtime/node22`，默认 `NODE_VERSION=v22.14.0`。
- Python 虚拟环境 → `runtime/venv`，安装 `mcp`、`fastapi`、`uvicorn`、`pydantic`、`openai` 和可编辑 Euler 包。Worker gateway 与 `bin/` 入口须能从任意工作目录导入 `euler`；脚本会检查导入，底层解释器失效时重建环境。
- Codex CLI → `runtime/codex-npm`，使用 npm 包 `@openai/codex`。
- human-summary 的 Node 依赖 `markdown-it/katex`，仅 PDF 渲染需要。
- `runtime/runtime.env`，供 `scripts/env.sh` 读取的主机路径配置。

若 `config/providers.env` 已有有效而非占位的 API Key，安装时还会配置 Codex 的 `model_provider`。

## 2. 配置账号与服务

复制模板并填写自己的配置。实际文件被 Git 忽略，不提交凭据。

```bash
cp config/euler.env.example config/euler.env
cp config/providers.env.example config/providers.env
```

Codex 组件需要配置相应后端。验证服务默认使用已登录的 Grok Build CLI：`EULER_VERIFY_BACKEND=grok`、`EULER_VERIFY_GROK_MODEL=grok-4.6`、`EULER_VERIFY_GROK_EFFORT=high`。Cursor 和 Codex 是需要明确选择的回退后端。

- **API Key**：在 `config/providers.env` 设置 `CODEX_BACKEND=api`、`CODEX_API_BASE_URL`、`CODEX_API_MODEL=gpt-5.5` 与 `EULER_CODEX_API_KEY`。再运行 `bash scripts/setup-codex.sh api`。Key 在运行时从环境变量读取，不写入生成的服务商配置。
- **ChatGPT 订阅**：在 `config/euler.env` 设置 `CODEX_BACKEND=chatgpt`，运行 `bash scripts/setup-codex.sh login` 并完成设备登录。

在 `config/euler.env` 配置 `EULER_CONSULT_TRANSPORT`：`gpt_pro` 使用付费 OpenAI 兼容 API，填写 `EULER_CONSULT_API_KEY/_BASE_URL/_MODEL`；`claude_api` 使用 Anthropic SDK，按实际 Token 计费；`claude_code` 通过 Claude Code CLI 使用订阅，按 `EULER_CONSULT_CLAUDE_CODE_PRICE_*` 估算记账；`off` 由主 Agent 自行进行策略推理。全部变量见[配置参考](configuration.zh.md)。

## 3. 检查 Codex 后端

```bash
bash scripts/check-codex.sh
```

成功时退出码为 0，并追加记录到 `runtime/logs/codex-health.jsonl`：

- `api`：用配置的 Key 向 `CODEX_API_BASE_URL` 发送一次小型实际请求，并检查近期日志，成功显示 `ok codex API reachable`。
- `chatgpt`：检查 CLI 登录状态，成功显示 `ok codex backend: chatgpt login active (Logged in using ChatGPT)`。

## 4. 启动验证服务

```bash
bash scripts/services.sh up verify
```

验证服务不可用时，`fact_submit` 会失败，无法产生新 Fact。`services.sh` 通过 `setsid` 让进程脱离当前 shell，见[运维手册](operations.zh.md)。

## 5. 健康检查

```bash
euler doctor
euler verify-health
bash scripts/doctor.sh
```

分别检查证明运行条件、验证服务身份及完整运行栈。检查涵盖配置、Python 依赖、Node、Codex 封装和后端、verify `/health`，以及可选的 `pdflatex` 和 Chrome/Chromium。

健康部署的输出会包含 `ok python pkg: euler`、`ok verify service up :8091 (ours)` 和所选后端登录／API 可达信息。未安装 TeX 时的 `warn no pdflatex on PATH` 只影响论文 PDF；Chrome 检查对应 human-summary PDF。`FAIL` 表示需处理的运行问题。

## 6. 创建并初始化项目

使用已安装 CLI。`euler new` 在被忽略的运行目录创建各服务商的 worker home 和 MCP 配置，仓库根目录不依赖特定编辑器或 Agent 客户端的指令文件。

```bash
euler new MY_PROJECT
euler init MY_PROJECT --contract contract.json --policy policy.json
```

之后按[操作指南](operating-guide.zh.md)提出任务、调度并启动研究。

## 故障排查

- 后端不稳定：运行 `bash scripts/check-codex.sh`，查看 `runtime/logs/codex-health.jsonl`。
- 主机重启：运行 `bash scripts/recover.sh`，步骤见[运维手册](operations.zh.md)。
- `doctor.sh` 出现失败项：先修复，再启动 worker。
