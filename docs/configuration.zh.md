# Euler 配置参考

<!-- language-nav -->
[English](configuration.md) · [简体中文](configuration.zh.md) · [返回 Euler](../README.zh-CN.md)
<!-- /language-nav -->

主机与账号专用配置保存在 Git 忽略的 `config/*.env` 中。`scripts/env.sh` 按下面的链加载并补齐默认值：

```text
config/providers.env → config/euler.env → runtime/runtime.env → 内置默认值
服务商后端             主机／账号          自动生成的主机路径     scripts/env.sh
```

仓库只提交 `*.env.example` 模板，复制成实际文件后编辑。`bin/` 封装会自动加载 `env.sh`。本页默认值对应 `scripts/env.sh` 和 `config/euler.env.example`。

## 前沿检查与审计

| 变量 | 用途 |
| --- | --- |
| `EULER_KERNEL_LAKE_BIN` / `EULER_KERNEL_LEAN_BIN` | 主机配置的检查程序；公开内核请求不能覆盖可执行路径 |
| `EULER_FINAL_AUDIT_RESOURCES` | 真实证明／反例审计的 JSON ResourceVector 预约；若项目限制金额或 Token，需提供正数的明确额度 |
| `EULER_VERIFY_TIMEOUT` | HTTP 超时秒数，也决定最终审计的时长预约 |
| `EULER_VERIFY_CONFIG_ID` | 同一端点更换验证部署时，由操作者指定的版本 ID |
| `EULER_GROK_AUTH_FILE` | 链接到隔离 Grok home 的认证来源文件 |

控制层与检查边界见[架构](../ARCHITECTURE.zh.md)。

## Codex 后端

| 变量 | 默认值 | 含义 |
| --- | --- | --- |
| `CODEX_BACKEND` | `api` | 自备 OpenAI 兼容 Key，或选择 `chatgpt` 使用登录账号 |
| `CODEX_HOME` | `runtime/codex-home` | 被 Git 忽略的认证与配置目录 |
| `CODEX_API_BASE_URL` | — | OpenAI 兼容 Responses 端点 |
| `CODEX_API_MODEL` | `gpt-5.5` | API 后端模型 |
| `EULER_CODEX_API_KEY` | — | 运行时读取的 Key，不写入生成的服务商配置 |

配置位于 `config/providers.env`。步骤见[部署入门](getting-started.zh.md)及 `scripts/setup-codex.sh`。

## 策略咨询

| 变量 | 默认值 | 含义 |
| --- | --- | --- |
| `EULER_CONSULT_TRANSPORT` | `gpt_pro` | `gpt_pro`、`claude_api`、`claude_code` 或 `off` |
| `EULER_CONSULT_API_KEY` | — | gpt_pro 使用的 OpenAI 兼容 API Key |
| `EULER_CONSULT_BASE_URL` | `https://api.openai.com/v1` | gpt_pro 端点 |
| `EULER_CONSULT_MODEL` | `gpt-5.5-pro` | gpt_pro 模型 |
| `EULER_CONSULT_BACKGROUND` | `1` | 发送 `background=true`；不支持时设为 0，单次调用可用 `--background off` |
| `EULER_CONSULT_STORE` | `0` | 发送 `store=false`；要求保存响应时设为 1，单次可用 `--store on` |
| `EULER_CONSULT_CLAUDE_CODE_MODEL` | `claude-sonnet-4-5` | Claude CLI 模型 |
| `EULER_CONSULT_CLAUDE_CODE_BIN` | `claude` | CLI 路径 |
| `EULER_CONSULT_CLAUDE_CODE_MAX_WALL` | `1800` | 单次咨询的实际时间上限，秒 |
| `EULER_CONSULT_CLAUDE_CODE_PRICE_IN` | `10.0` | 每百万输入 Token 的账本估计美元单价 |
| `EULER_CONSULT_CLAUDE_CODE_PRICE_OUT` | `50.0` | 每百万输出 Token 的账本估计美元单价 |
| `EULER_CONSULT_CLAUDE_API_KEY` | 回退到 `ANTHROPIC_API_KEY` | 自备 Anthropic API Key |
| `EULER_CONSULT_CLAUDE_API_BASE_URL` | Anthropic 默认 | 使用代理时指定 |
| `EULER_CONSULT_CLAUDE_API_MODEL` | `claude-sonnet-4-5` | API 模型 |
| `EULER_CONSULT_CLAUDE_API_FALLBACK` | `claude-opus-4-1` | 遭拒时的后备模型，设为 `off` 可关闭 |
| `EULER_CONSULT_CLAUDE_API_PRICE_IN` | `10.0` | 按实际用量记账的每百万输入 Token 美元单价 |
| `EULER_CONSULT_CLAUDE_API_PRICE_OUT` | `50.0` | 按实际用量记账的每百万输出 Token 美元单价 |

以上单价是配置的记账参数。`gpt_pro` 使用付费兼容 API；`claude_api` 通过原生 SDK 按实际用量统计；`claude_code` 通过 `claude -p` 使用订阅；`off` 不发起外部咨询。

Claude Code 咨询在临时工作目录隔离运行，使用 `--setting-sources "" --strict-mcp-config`，不加载其他设置或 MCP，只开放网页工具。提示通过 stdin 传入，不放入共享主机可见的 argv；其上下文为整理后的研究摘要与公开网页。

每次咨询可用 `--effort` 选择 `minimal/low/medium/high/xhigh/max`。所有传输支持到 `max`；gpt_pro 的 max 请求不会在不告知的情况下去掉 effort 重试。

## 原生 worker 服务商

Worker 通过 `euler.providers` 启动，角色文件用 `PROVIDER=` 明确选择服务商。

| PROVIDER | 程序环境变量 | 默认程序 | 说明 |
| --- | --- | --- | --- |
| `codex` | `EULER_CODEX_BIN` | `codex` | `codex exec` |
| `claude-code` | `EULER_CLAUDE_CODE_BIN` | `claude` | `--print/--resume`，可使用 `claf` 等封装 |
| `grok` | `EULER_GROK_BIN` | `grok` | 默认 `--single`，由 `GROK_RESUME_SESSION/EULER_GROK_PERSISTENT` 启用恢复 |
| `cursor` | `EULER_CURSOR_BIN` | `cursor-agent` | `-p`、工作区／add-dir、MCP 批准与关闭 CLI 沙箱 |

一种服务商的 API Key 不应进入另一服务商的子进程环境。持久团队对五种 CLI 的配置与会话支持见 [CLI 与平台支持](CLI-SUPPORT.zh.md)。

## 模型与推理力度

验证后端由 `EULER_VERIFY_BACKEND=grok|codex|cursor` 明确指定，默认 Grok Build，不静默切换。

| 变量 | 默认值 | 作用范围 |
| --- | --- | --- |
| `EULER_CODEX_BIN` | `<repo>/bin/codex`，否则 PATH 的 codex | 所有 Codex 调用 |
| `EULER_CODEX_MODEL` | `gpt-5.5` | 通用默认模型 |
| `EULER_CODEX_EFFORT` | `xhigh` | 通用默认推理力度 |
| `EULER_VERIFY_BACKEND` | `grok` | 验证后端，cursor／codex 需明确选择 |
| `EULER_GROK_BIN` | `grok` | Grok Build 验证者与 worker CLI |
| `EULER_VERIFY_GROK_MODEL` / `_EFFORT` | `grok-4.6` / `high` | Grok 验证者 |
| `EULER_VERIFY_GROK_HOME` | `<EULER_ROOT>/runtime/grok-verifier-home` | 隔离验证运行与认证目录 |
| `EULER_CURSOR_BIN` / `EULER_VERIFY_CURSOR_MODEL` | `cursor-agent` / `cursor-grok-4.6-xhigh` | 旧 Cursor 回退配置 |
| `EULER_VERIFY_MODEL` / `_EFFORT` | 通用默认 | Codex 验证回退配置 |
| `EULER_WRITE_PAPER_MODEL` / `_EFFORT` | 通用默认 | 论文渲染 |
| `EULER_HUMAN_SUMMARY_MODEL` / `_EFFORT` | 通用默认 | 进展报告渲染 |

## 端口

| 变量 | 默认值 | 服务 |
| --- | --- | --- |
| `VERIFY_PORT` | `8091` | 本机验证服务 |
| `DASHBOARD_PORT` | `8099` | 本机工作台 |
| `EULER_VERIFY_URL` | `http://127.0.0.1:8091/verify` | fact_submit 请求地址 |
| `VERIFY_HOST` | `127.0.0.1` | 验证服务绑定地址 |

## 运行数据位置

均默认位于被忽略的 `runtime/` 下。

| 变量 | 默认值 | 内容 |
| --- | --- | --- |
| `EULER_RUNTIME` | `<repo>/runtime` | 自包含运行环境 |
| `EULER_AGENTS_ROOT` | `runtime/projects` | euler new 创建的项目 |
| `VERIFIER_RESULTS_DIR` | `runtime/verify-runs` | 每次验证日志 |
| `EULER_PY` | `runtime/venv/bin/python`，否则系统 python3 | 引擎 Python |

## Worker 循环节奏

| 变量 | 默认值 | 含义 |
| --- | --- | --- |
| `EULER_ROUND_HARD_TIMEOUT` | `14400`，4 小时 | 轮次实际时间上限 |
| `EULER_MAX_ROUNDS` | `0`，不限 | 轮次数上限 |
| `EULER_MAX_CONSEC_FAILURES` | `5` | 连续失败多少轮后退出 |
| `EULER_ROUND_BEAT` | `5` | 轮次间隔，秒 |

持久团队各 CLI 的轮次策略另见 [CLI 支持](CLI-SUPPORT.zh.md)。

## 渲染与其他配置

| 变量 | 默认值 | 含义 |
| --- | --- | --- |
| `EULER_CHROME_BIN` | 自动检测 | human-summary PDF 使用的无头 Chrome/Chromium |
| `TEX_ENGINE` | `pdflatex` | 可选 xelatex、lualatex、tectonic |
| `EULER_WRITE_PAPER_RUN_LOG` | 开启 | 每次写作调用的诊断日志，0 关闭 |
| `EULER_PAPER_VERIFY_WHOLE_DOC_CAP` | `700000` | 全文数学核验的字符上限，超限返回 too_large，由主 Agent 决定拆分 |

## 可选 LaTeX Git 推送

在被忽略的 `config/latex-git.env` 设置 `LATEX_GIT_URL`、`LATEX_GIT_TOKEN`，以及可选 `LATEX_GIT_AUTHOR_NAME/_EMAIL`。向外推送需要操作者授权。

端口与 verify HTTP 是固定的跨模块接口，调整时要同步两端，见[架构](../ARCHITECTURE.zh.md)。服务启动见[运维手册](operations.zh.md)，使用这些配置的命令见[CLI 参考](cli-and-tools.zh.md)。
