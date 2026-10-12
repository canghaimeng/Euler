# Euler 运维手册

<!-- language-nav -->
[English](operations.md) · [简体中文](operations.zh.md) · [返回 Euler](../README.zh-CN.md)
<!-- /language-nav -->

本页介绍持久服务、健康检查、主机重启恢复和无人值守运行。主 Agent 可以代为执行这些操作；本手册也供直接管理部署和排查故障使用。

## 持久服务

使用 `scripts/services.sh` 管理服务。它通过 `setsid` 创建脱离终端的进程，使服务在 shell 或 SSH 会话结束后继续运行。

```bash
bash scripts/services.sh up verify
bash scripts/services.sh up dashboard <p>
bash scripts/services.sh status
bash scripts/services.sh logs <svc> -f
bash scripts/services.sh down <svc>
```

- **verify**：`127.0.0.1:8091`，Fact 入库的验证门禁，启动 worker 前应先运行。
- **dashboard**：`127.0.0.1:8099`，显示项目记录和已配置的研究控制；远程访问使用 SSH 转发。

端口按主机分配。如果另一份 Euler 部署已占用 8091，启动时会出现 `address already in use`。`/health` 提供服务进程 PID；诊断与服务状态命令将它和自己的 `runtime/run/verify.pid` 比对，将外来服务报告为 `FOREIGN`。共享主机上的各部署应在 `config/euler.env` 配置各自的 `VERIFY_PORT/DASHBOARD_PORT`。

`services.sh` 在 `runtime/run/` 保存 PID 注册表和 `autostart` 清单，供重启时恢复服务。

## 健康检查

```bash
euler verify-health
euler doctor
bash scripts/doctor.sh
bash scripts/check-codex.sh
```

- `verify-health` 只读检查服务身份：`ours/foreign/stale/down`。
- `euler doctor` 检查证明所需 Python／MCP、worker 工具和 verify 身份。
- `doctor.sh` 检查配置、Python 与依赖、Node、Codex 封装和后端、实时 `/health`，并检查可选 TeX 与 Chrome。结果分为 `ok/FAIL/warn`。例如 `ok verify service up :8091 (ours)` 表示正确实例在线；`warn no pdflatex on PATH` 表示缺少仅写作需要的依赖。
- `check-codex.sh` 检查所选后端；API 模式会发送实际小型请求，ChatGPT 模式检查登录。成功时退出码 0，历史记录在 `runtime/logs/codex-health.jsonl`。Worker 或 verify 出现 API 错误时可使用它。

## 主机重启后的恢复

```bash
bash scripts/recover.sh
```

脚本可重复执行：重新运行 `bootstrap.sh`，修复失效虚拟环境与 Codex 服务商配置，清理过期 PID 文件，重放 `runtime/run/autostart`，再报告 Codex 和服务健康状态。

`recover.sh` 恢复服务，不自动恢复 worker 循环。使用 `euler start <project>` 从持久记忆重新启动 worker。

## Worker 生命周期

```bash
euler status <project>
euler start <project>
euler stop <project>
euler stop <project> --force
```

Worker 在独立进程组中脱离终端运行。普通 stop 等待当前轮次结束；`--force` 立即终止进程组及其 Codex 子进程。`status` 在运行轮次超过硬时限约 1.5 倍时显示 `stuck?`，这是需要排查后端并决定是否重启的运行信号。

## 无人值守运行

旧运维流程使用参数化的 `strategy-loop.sh` 和 `watchdog.sh` 示例：前者在已有 elaboration 时按 `EULER_STRATEGY_BEAT` 节奏咨询策略，后者检查 verify 和 worker 状态，通过 `EULER_NOTIFY` 报告阻塞、退出或错误。这些属于部署辅助脚本，当前精简发行包未包含 `examples/ops/`。需要无人值守服务时，请在自己的部署中配置服务管理与通知。

## 常见问题

| 现象 | 检查方法 |
| --- | --- |
| 没有 Fact 入库 | 检查 verify 是否在线，运行 `services.sh status`、`doctor.sh` |
| Worker 轮次报错 | 检查 `check-codex.sh`、`runtime/logs/codex-health.jsonl`、worker 的 `logs/round_*.log` |
| `paper_*` 工具未返回 `ok` | 阅读返回的 `log_path`，通常在 `<project>/paper/.runs/<utc>-<tool>/log.md` |
| 工作台空白 | 检查 8099 转发，以及该项目的工作台服务是否运行 |
| 重启后没有运行任务 | 先 `recover.sh`，再 `euler start <project>` |

参数见[配置参考](configuration.zh.md)，隔离与验证要求见[验证与访问控制](security-and-trust.zh.md)。
