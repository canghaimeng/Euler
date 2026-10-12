# Euler 系统架构

<!-- language-nav -->
[English](ARCHITECTURE.md) · [简体中文](ARCHITECTURE.zh.md) · [返回 Euler](README.zh-CN.md)
<!-- /language-nav -->

Euler 的持久数学研究状态由研究者与前沿驱动的 Agent 共同使用。带明确适用范围的对象支持检查、任务选择、有界上下文检索和经过验证的更新。主 Agent 协调工作；worker 使用由验证者把关的 Fact gateway；内核与计算适配器提供各自适用范围内的证据。人类请求通过控制层创建可审计的派生研究，结果遵循已有 Fact 入库和最终验收程序。

这一开发分支加入对象解析器、运行生命周期、干预服务和可选的人类 HTTP 操作边界，详见[研究干预](docs/HUMAN-INTERVENTION.zh.md)。已有观测视图继续作为人类入口；没有精确可写后台身份的历史整理节点保持只读。

主 Agent 的规范操作约定位于 `agents/contracts/main_agent.md`。

## 1. 分层模型

```text
操作者 → ① orchestration（主 Agent 与 euler CLI）—— 协调任务，不直接写 Fact
             ② strategy（elaboration → consult → master_guidance）
             ③ execution（原生 Codex、Claude Code、Grok、Cursor worker）
  gm_* │          │ fact_submit
       ▼          ▼
  ⑤ truth       ④ verification（独立冷启动 LLM 审阅者）
  事实图与记忆      correct ⇔ 无 critical_errors 且无 gaps
       ▲          Fact 经验证接受后才建立
       │ 所有读写经过……
  ⑥ gateway（按角色限制的 MCP；main 无 fact_submit，verifier 只读）

人类入口：⑦ workbench（对象检查、范围明确的请求、运行控制、结果追踪）
          ⑧ ops/runtime（安装、服务、诊断、配置）
输出：write-paper（论文）、human-summary（进展报告），各由隔离 Codex 渲染

⑨ research control（ProblemContract、义务／路线图、重复失败分析、
上下文编译、调度与 lease、有界计算、验收、检查点和评估）
—— 读取已有真值路径，不提供另一条 Fact 入库通道
```

## 2. 目录结构

```text
Euler/
├─ ARCHITECTURE.md              架构、约束与接口约定
├─ README.md  pyproject.toml    介绍与可安装的 euler 包
├─ .gitignore  .github/         仓库管理与可移植 CI
├─ config/                     环境模板，仅提交 *.env.example
├─ euler/                      Python 引擎
│  ├─ core/                    真值层：结构、事实图、全局／局部记忆、BM25、术语表
│  ├─ gateway/                 按角色限制的 MCP 与工具表 roles.py
│  ├─ verify/                  验证 HTTP 服务、预检查、冷启动审阅者
│  ├─ execution/               worker 循环、项目／成员生命周期与文件布局
│  ├─ strategy/                consult gateway 与多种咨询传输方式
│  ├─ orchestration/           euler CLI 命令
│  ├─ control/                 进展、预算、证据、验收和评估控制
│  ├─ integrations/            arXiv 定理检索（Matlas）
│  ├─ observability/           工作台与可选研究控制
│  ├─ authoring/               主 Agent 专用的隔离渲染器与公共工具
│  ├─ write_paper/             论文 MCP、上下文组装与工具
│  └─ human_summary/           报告 MCP、过滤与上下文组装
├─ agents/                     Agent 约定与随附技能，作为数据分发
│  ├─ contracts/               main_agent.md、worker.md、verifier.md
│  └─ skills/
│     ├─ initialize/ consult/ elaboration/
│     ├─ human-summary/        报告流程与渲染资源
│     ├─ worker/               证明与分析技能
│     ├─ verify/               验证技能
│     └─ write-paper/          写作流程、驱动、角色提示、模板与风格
├─ bin/                        euler、euler-mcp 等薄封装入口
├─ scripts/                    安装、诊断、服务、环境、恢复和 TeX 安装
├─ docs/                       入门、概念、操作、验证与访问控制等文档
└─ examples/                   配置与研究案例
```

## 3. 设计不变量

1. **三层记忆与统一正确性边界。** 局部私有记忆 → 全局共享认知 → 事实图。证明使用已入库的 `fact_id` 作为依据；全局记忆本身不构成已验证正确性的来源。
2. **按工具可见性落实权限。** 权限由 gateway 角色表执行。`main` 无法调用 `fact_submit`，`verifier` 只读。
3. **验证者是唯一写入门禁。** 收到有效 `correct` 结论后才建立 Fact；门禁在 `fact_submit` 代码路径中执行。
4. **事实图按内容寻址，支持级联撤销。** `fact_id` 由 `problem_id + predecessors + glossary_introduces + statement + proof` 计算。`external_refs` 不纳入哈希，使论文流程改写引用时不破坏 DAG。
5. **自主运行与可恢复。** Worker 脱离终端运行，在轮次边界从持久记忆恢复已登记进展和上下文。
6. **策略咨询负责方向选择。** 主 Agent 在轮次之间通过 consult 获取推理建议，并写入 `master_guidance`。传输方式包括 `gpt_pro`（默认，可使用 gpt-5.5-pro）、`claude_api`／`claude_code`（可使用 claude-sonnet-4-5）和 `off`。未配置外部咨询时，`off` 由主 Agent 自行推理；每轮之间仍需完成方向决策。
7. **可移植与自备凭据。** 不硬编码绝对路径，不提交秘密；凭据来自 Git 忽略的 `config/*.env`，仓库只提交模板。
8. **范围明确的写作上下文。** 论文和报告作者接收由指定研究记录与读者需求组装的专用上下文，组装时过滤内部标识和运行细节。
9. **服务商隔离。** Codex、Claude Code、Grok、Cursor 使用统一的请求／命令协议，同时保留各自程序、会话恢复、MCP 配置和凭据过滤。数学结论统一通过验证门禁进入事实图。

## 4. 接口与端口约定

涉及跨模块接口的改动必须同时更新两端。端口和约定结构保持固定，一端不能单方面改变另一端依赖的接口。

### 网络端口

均默认绑定本机回环地址。

| 端口 | 服务 | 调用关系 |
| --- | --- | --- |
| 8091 | verify 的 `/verify`、`/health` | `euler.gateway.fact_submit` 通过 `EULER_VERIFY_URL` 调用 `euler.verify` |
| 8099 | 工作台 | 浏览器访问 `euler.observability` 的视图与已配置研究控制 |

### 跨模块约定

| 约定 | 固定语义 | 两端 |
| --- | --- | --- |
| MCP 工具与角色 | 共 6 个工具，按 `roles.py` 的 `ROLE_TOOLS` 分配；main 无 `fact_submit`，verifier 只读 | `euler.gateway` ↔ worker／main／verifier |
| MCP 启动 | `python -m euler.gateway` 与 `EULER_ROLE` 环境变量 | 验证启动器、生成的服务商配置、`bin/euler-mcp` ↔ gateway |
| 验证 HTTP | `POST /verify {statement,proof}` → `{verification_report,verdict,repair_hints}`；correct 要求无严重错误且无缺口 | `fact_submit` ↔ verify |
| `/health` 服务身份 | 返回 `{status,pid,euler_root}`，只证明服务可达。调用者将 PID 与 `runtime/run/verify.pid` 比对，并核实 checkout 路径，区分 `ours/foreign/stale/down` | 执行预检查、`euler verify-health`、环境脚本 ↔ verify |
| 证明预检查 | 启动前及每次 `run_round` 前检查 Python／MCP 导入、worker 工具表、stdio 的 `initialize`／`tools/list`、verify PID 与非空根路径、项目内唯一存储探针。失败记录 `PRECHECK_FAILED {component,reason}`，不启动 `codex exec` | `execution.preflight` ↔ 启动与执行循环 |
| 轮次上下文与记忆 | 证明轮次要求 TASK.md 带 `branch_id/subgoal_id`；MCP 环境绑定不可伪造的项目／worker／运行／分支／子目标。`gm_add` 检查尝试／障碍／死路，提交记录区分传输故障与数学判断，基础设施故障的 `verdict` 为 null。结束时须有成功 Fact 或关联尝试，否则产生 `TASK_CONTEXT_FAILED/MEMORY_CONTRACT_FAILED` | 执行上下文、round_contract ↔ gateway |
| 记忆摘要与回执 | 每轮检索当前分支／子目标的尝试、障碍、死路、反例、验证，再以 BM25 补充，每类最多 5 条并限制总量。不可变的 start → launch → result → end 回执绑定提示、执行后端和新增记录水位。失败产生 `RECEIPT_FAILED`，不启动后端或下一轮 | `memory_digest/launch_receipt` ↔ 执行循环／GlobalMemory |
| Euler 控制层 | 追加式 EventStore 与 CAS，精确 ProblemContract／ResearchView，ContextSeed→ActionSpec→ContextRequest，多维预算预约和 lease，检查过的计算候选，最终对齐与确定性评估。可引用有效 Facts 和入库轨迹，不提供 Fact 写 API | `euler.control` ↔ 只读适配器／gateway 真值边界 |
| 失败聚类 | 每轮成功收尾后，对新增、带标记的受阻尝试、方法签名、连续验证失败和障碍签名进行确定性聚类。基础设施 `verdict=null` 不计入。用项目锁、追加式 `failures/ledger.jsonl` 和不可变 `requests/<cluster_id>.json` 保证每类一个请求，状态为 `requested/resolved/superseded`。仅有侧文件时校验并补记一次新序号事件；仅有账本时重建侧文件；损坏或不匹配则 `FAILURE_CONTROL_FAILED` 并停止循环 | `failure_clusters` ↔ 执行循环／编排 |
| 失败归纳 | 指定 worker 下一轮接收 `FAILURE SYNTHESIS MODE`，仍需完成阶段 1–3，并提交匹配的 `key_failures_summary` 障碍或死路候选，`source_record_ids` 必须恰为请求的 `member_ids`。缺失或伪造 ID 导致 `FAILURE_SYNTHESIS_FAILED`。调度器不确认数学死路、不写 Fact；操作者用 `euler failures <project>` 或 `euler status --json` 查看 | 失败聚类 ↔ worker 记忆协议 |
| Fact ID 输入 | `problem_id + sorted(predecessors) + sorted(glossary) + normalized(statement,proof)`，排除 `external_refs` | `euler.core` ↔ 各模块 |
| 全局记忆类别 | 11 种 `GLOBAL_KINDS`，包括 `master_guidance/elaboration/verification` | core ↔ Agent／策略／咨询 |
| Consult JSON | 统一 `{transport,reply,usage,cost_usd,…}`；咨询不能执行时返回 `status="failed"` 与 `error`，不把 traceback 当回复 | strategy CLI ↔ consult 技能 |
| 写作提示资源 | 从 `agents/skills/write-paper/` 读取角色提示和风格，可由 `EULER_WRITE_PAPER_SKILL_DIR` 指定 | write_paper 组装器 ↔ 写作技能 |
| 环境变量 | `EULER_*/CODEX_*/VERIFY_*/CONSULT_*`；所有 Codex 调用通过共享 `euler.codex` 解析程序、模型、effort、PATH 和 exec 前缀。通用配置为 `EULER_CODEX_BIN/MODEL/EFFORT`，各服务可用 `EULER_{VERIFY,WRITE_PAPER,HUMAN_SUMMARY}_{MODEL,EFFORT}` 覆盖 | `euler.codex`、config、env.sh ↔ 所有 Codex 执行入口 |

## 人类干预模块映射（0.3 开发版）

- `control/human_objects.py`：规范公开身份、范围、依赖和证据视图。
- `control/campaign.py`：统一准入边界、暂停／继续／停止、持久检查点。
- `control/intervention.py`：预览、类型化请求准入、派生约定／路线／任务、明确启动聚焦研究。
- `execution/human_control.py`：按 lease 取消本地进程，以及轮次时限。
- `observability/human_api.py`：服务端项目／worker 策略、同源检查和控制权限。
- `observability/static/human.js`：逐层查看对象、审阅／确认／启动、结果与来源导航。

请求绑定对象与工作区身份、来源范围和原生 worker 策略。研究者输入作为提案处理，原 Facts、验证等级和原约定版本保留。原始目标的最终审计与局部子结果验收分别进行。子系统测试检查这些不变量；私有开发事件导出不包含在发行包中。
