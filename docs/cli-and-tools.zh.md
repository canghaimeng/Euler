# Euler 命令行与工具参考

<!-- language-nav -->
[English](cli-and-tools.md) · [简体中文](cli-and-tools.zh.md) · [返回 Euler](../README.zh-CN.md)
<!-- /language-nav -->

Euler 提供两类控制入口：

1. **`euler` CLI**：由你或主 Agent 执行，管理项目和 worker 生命周期。
2. **MCP 工具**：通过 `bin/euler-mcp`、`bin/euler-control-mcp`、`bin/write-paper-mcp` 和 `bin/human-summary-mcp` 调用；worker 的配置仅包含真值 gateway。

通常直接用自然语言与主 Agent 交流，由它执行命令和调用工具。本页列出可用接口，研究前沿、内核与计算的设计见[架构](../ARCHITECTURE.zh.md)。

## euler CLI

`bin/euler` 自动加载 `scripts/env.sh`。项目级操作必须指定项目名，没有默认项目。

| 命令 | 用法 | 作用 |
| --- | --- | --- |
| `list` | `euler list [--json]` | 列出项目、活动 worker 数量与模型 |
| `new` | `euler new <project> [--profile native\|codex] [--roles codex:1,grok:1,...] [--model M]` | 初始化项目；默认原生名单包含 Codex、Claude Code、Grok、Cursor |
| `init` | `euler init <project> --contract FILE --policy FILE [--obligation FILE]` | 为已有项目创建或确认相同控制配置；省略义务时使用约定中的精确命题 |
| `frontier` | `euler frontier <project>` | 查看活动义务、路线、障碍和证据 |
| `research-update` | `euler research-update <project> --operations FILE` | 原子地应用类型化研究状态变更 |
| `admit-fact` | `euler admit-fact <project> <fact_id> --obligation ID --scope-hash HASH --alignment-note TEXT` | 审查范围后，关联已入库 Fact |
| `kernel-check` | `euler kernel-check <project> --obligation ID --request FILE --request-id ID` | 在资源预约约束下执行真实 Lean 检查 |
| `kernel-align` | `euler kernel-align <project> --obligation ID --artifact HASH --review FILE` | 记录明确的命题对齐审阅 |
| `compute` | `euler compute <project> --request FILE` | 有界多项式搜索与独立检查 |
| `compute-review` | `euler compute-review <project> --review FILE` | 审阅检查所得见证是否实现原始数学对象 |
| `propose` | `euler propose <project>/<worker> --candidate FILE` | 保存该 worker 的 CandidateAction |
| `schedule` | `euler schedule <project> [--dispatch] [--launch]` | 运行调度器；`--launch` 默认关闭，启用时同时包含 `--dispatch` |
| `reconcile` | `euler reconcile <project>/<worker>` | 协调最新已验证的 start／launch／result／end 回执链 |
| `checkpoint` | `euler checkpoint <project> [--retry-infra --retry-reason TEXT]` | 对 DISCHARGED 进行证明审计，对 REFUTED 进行反例审计；支持明确的基础设施重试 |
| `assign` | `euler assign <project>/<worker> (--task "…" \| --file P \| --stdin) [--branch-id ID --subgoal-id ID]` | 用于非受管流程；绑定 lease 的 TASK 由 `euler schedule --dispatch` 写入 |
| `finalize` | `euler finalize <project> [--paper <paper_id>] [<fact_id> …]` | 将批准的目标定理写入 TARGET.md；默认位置 `<project>/TARGET.md`，具名论文为 `<project>/papers/<paper_id>/TARGET.md`。省略 Fact ID 时只打印候选终端事实，不写入 |
| `start` | `euler start <project>[/<worker>]` | 启动自主 worker 循环，证明预检查必须先通过 |
| `status` | `euler status <project>[/<worker>] [--json]` | 查看存活状态、轮次和最近活动；已初始化项目的 JSON 包含 control 状态，verify 离线时仍可用 |
| `failures` | `euler failures <project> [--json]` | 只读失败聚类账本，含类别、状态、触发原因和指定 worker；不读私有记忆。`failures/requests/<cluster_id>.json` 是不可变侧文件，控制错误记录为 `FAILURE_CONTROL_FAILED` |
| `stop` | `euler stop <project>[/<worker>] [--force]` | 等当前轮次结束后退出，或用 `--force` 终止进程组 |
| `verify-health` | `euler verify-health [--json]` | 检查验证服务身份 `ours/foreign/stale/down`，不作数学判断 |
| `doctor` | `euler doctor [--json]` | 只读检查 Python／MCP、worker 工具及 verify 身份；服务离线时给出警告 |

`finalize` 只记录目标，不停止 worker。是否将某个已验证 Fact 视为原问题答案，由你作判断，主 Agent 提供依据。

`start` 将各 worker 放在独立进程组中脱离终端运行，`stop --force` 可终止 worker 及正在运行的 Codex 子进程。启动前和每轮前检查 Python／MCP、worker 工具表、配置角色及 stdio 的 `initialize/tools/list` 握手、verify 的 PID 和非空 `euler_root`，并用唯一原子探针检查项目内各存储可写。

缺少 `/health` 的 `euler_root` 会判为外来服务；PID／根路径匹配但服务未返回 HTTP 200 与 `status=ok` 时，健康命令也以非零状态退出。预检查失败写入 `PRECHECK_FAILED {component,reason}`，不启动模型进程。通过后保存并校验 `receipts/<worker>/round_NNNN.start.json`。受管任务使用编译后的 action packet，旧非受管任务使用有界记忆摘要；回执持久化或校验失败产生 `RECEIPT_FAILED`。轮次结束的 `.end.json` 记录新增记录 ID 和结束水位。

没有 `euler submit` 命令，也没有在生产中绕过 MCP、直接导入 `fact_submit` 或写 JSONL 的后备路径。Fact 只能通过 worker MCP 的 `fact_submit` 写入。`list/status/failures/doctor/verify-health` 不要求 verify 在线。

## euler-control MCP：仅主 Agent

由 `bin/euler-control-mcp` 以 `[mcp_servers.euler-control]` 和 `EULER_ROLE=main` 启动。Worker 和 verifier 看不到控制工具，主 Agent 仍没有 `fact_submit`。

| 工具 | 参数 | 作用 |
| --- | --- | --- |
| `euler_project_status` | `project` | 确定性控制状态 |
| `euler_register_contract` | `project, contract, policy, obligation?` | 初始化或确认相同受管控制 |
| `euler_frontier` | `project` | 读取活动前沿 |
| `euler_update_frontier` | `project, operations` | 事务式研究变更 |
| `euler_admit_fact` | `project, fact_id, obligation_id, confirmed_scope_hash, alignment_note` | 关联已接受证据 |
| `euler_kernel_check` | `project, obligation_id, request_id, request` | 执行本地 Lean 检查 |
| `euler_kernel_align` | `project, obligation_id, kernel_artifact_hash, review` | 登记形式命题对齐审阅 |
| `euler_compute` | `project, request` | 有界多项式搜索与检查 |
| `euler_compute_review` | `project, review` | 登记见证实现审阅 |
| `euler_propose_action` | `project, worker, candidate` | 保存 CandidateAction |
| `euler_schedule` | `project, dispatch?, launch?` | 调度，默认不启动 |
| `euler_record_failure` | `project, failure` | 类型化 FailureRecord 事件 |
| `euler_analyze_recurrence` | `project, tool_hash?` | 只读分析，不自动搁置路线 |
| `euler_checkpoint` | `project, retry_infra?, retry_reason?` | 触发或重放证明／反例最终审计 |

## euler MCP：真值存储的 gateway

可见工具由 `EULER_ROLE` 决定，主 Agent 使用 `main`。`?` 表示可选参数。

| 工具 | 参数 | 作用 |
| --- | --- | --- |
| `gm_add` | `kind, claim, evidence, verifiable?, glossary?, links?, project?` | 向共享全局记忆发布发现 |
| `gm_search` | `query, kinds?, limit_per_kind?, project?` | 搜索全局记忆 |
| `fact_submit` | `statement, proof, predecessors?, glossary_introduces?, intuition?, source_id?, external_refs?` | 先验证，仅 `correct` 时写 Fact，始终记录判定轨迹 |
| `fact_search` | `query, limit?, project?` | 检索已验证事实图 |
| `fact_revoke` | `fact_id, reason, project?` | 级联撤销 Fact 及依赖者 |
| `search_arxiv_theorems` | `query, num_results?` | arXiv 定理命题语义检索 |

| 角色 | 工具 |
| --- | --- |
| Worker | `gm_add`、`gm_search`、`fact_submit`、`fact_search`、`search_arxiv_theorems` |
| Main | `gm_add`、`gm_search`、`fact_search`、`fact_revoke`、`search_arxiv_theorems` |
| Verifier | 仅 `search_arxiv_theorems`，只读 |

主 Agent 无法写 Fact，验证者无写权限。详见[验证与访问控制](security-and-trust.zh.md)。

## write-paper MCP：从事实图生成论文

`euler/write_paper/server.py` 提供六个工具。需要模型的步骤使用隔离 Codex 角色；主 Agent 传入小型结构化参数，风格指南和数学内容在工具内部组装。返回状态、路径、标记和每次调用的 `log_path`，不把完整 `.tex` 返回主 Agent。

| 工具 | 角色 | 作用 |
| --- | --- | --- |
| `paper_subgraph` | 筛选，不调用模型 | 返回目标 Fact 依赖闭包的紧凑命题骨架，不写文件；选出的 `fact_ids` 交给 `paper_write` |
| `paper_write` | 作者 | 根据目标依赖闭包和风格生成完整初稿 `main.tex` |
| `reference_audit` | 引用审计 | 离线标记无法确认的文献，无工具和网络 |
| `reference_verify` | 引用核验 | 在线通过 arXiv 和网页核实标记引用，更新文献账本 |
| `paper_revise` | 修订者 | 按编译错误、操作者意见或引用修正改稿，工具内执行编译重试 |
| `paper_verify_math` | 数学复核 | 交付前由专用验证者核查整篇实际稿件 |

多数工具接受可选 `paper_id`。默认论文目录是 `<project>/paper/`，其他 ID 使用独立的 `<project>/papers/<paper_id>/`。完整 Agent 写作流程位于 `agents/skills/write-paper/README.md`。

## human-summary MCP：面向读者的进展报告

`summary_write` 从事实图生成不含内部 ID 的 PDF，包含精确问题、附证明梗概的局部结果、主要障碍、中立时间线与待补引理。可指定 `language`，否则遵循 `OPERATOR.md` 中的操作者语言。

## 主 Agent 技能

`agents/skills/` 提供 `initialize`、`elaboration`、`consult`、`human-summary` 和 `write-paper` 等会话内技能，用于组织上面的工具和 CLI。其生命周期关系见[操作指南](operating-guide.zh.md)。

## 持久服务

| 服务 | 端口 | 是否必需 |
| --- | --- | --- |
| `verify` | 127.0.0.1:8091 | 必需；不可用时 `fact_submit` 失败 |
| `dashboard` | 127.0.0.1:8099 | 可选；通过端口转发访问工作台 |

```bash
bash scripts/services.sh up verify
bash scripts/services.sh up dashboard <p>
bash scripts/services.sh status
bash scripts/services.sh logs <svc> -f
bash scripts/services.sh down <svc>
```

服务运行见[运维手册](operations.zh.md)，环境变量见[配置参考](configuration.zh.md)。
