# Euler 操作指南

<!-- language-nav -->
[English](operating-guide.md) · [简体中文](operating-guide.zh.md) · [返回 Euler](../README.zh-CN.md)
<!-- /language-nav -->

本页从操作者角度说明项目的完整流程。你可以用自然语言与主 Agent 交流，由它执行 CLI 和工具。开始前阅读[基本概念](concepts.zh.md)，按[部署入门](getting-started.zh.md)配置。精确接口见[命令行与工具参考](cli-and-tools.zh.md)。

## 一次研究的整体流程

```text
初始化 → 新建项目 → 策略循环 ⇄ worker 研究 → 已验证事实积累
                        │                       │
                   随时生成进展报告        确认答案 → finalize
                                                │
                                             论文写作
```

是否已回答原问题、是否对外发布、是否增加预算等判断由你决定；主 Agent 推进已授权工作并报告进展。

## 0. 首次初始化

新部署中，主 Agent 通过 `initialize` 确认称呼与语言、从 main 创建的工作分支、预算上限、咨询传输方式及 Codex 后端，填写 `OPERATOR.md` 和 `config/euler.env`，启动 verify，并创建 `runtime/.euler-initialized`。之后每次会话重新读取操作者配置和项目 `PROBLEM.md`。

## 1. 启动项目

向主 Agent 提供问题，它会：

1. 确认 worker 配置。默认 `native` 为 Codex×2、Grok×2、Claude Code×1、Cursor×1，分别使用原生适配器和同一验证门禁。
2. 将原始目标写入 `runtime/projects/<p>/PROBLEM.md`。
3. 运行 `euler new <p>` 创建成员、空全局记忆与事实图。用户指定的其他名单使用 `--roles`。

一个项目对应一个问题及其独立记忆和事实图。可同时运行多个项目，每个操作都明确项目名。

## 2. 策略循环

新研究状态出现后，主 Agent 协调下一步：

1. **整理（elaborate）**：把共享记录整理为高信息密度摘要，包含判断、已闭合路线、风险与缺失桥接引理。
2. **咨询（consult）**：向强推理模型咨询；可选 `gpt_pro/claude_api/claude_code/off`。
3. **提出与调度**：将回复记录为 `master_guidance`，用 `euler propose` 保存 worker 操作，再执行 `euler schedule`。可选 `--dispatch`；只有确实要启动时才加 `--launch`。需要 Euler lease 的操作走受管调度路径。
4. **监测**：查看 `euler status` 和工作台；轮次结束后执行 `euler reconcile`，目标闭合时运行 `euler checkpoint`。`NO_VERIFIABLE_PROGRESS/INFRA_FAILED` 等类型化暂停记录运行状态，数学验收单独记录。有新状态后再重复循环。

旧操作流程通常在活跃会话中约每两小时咨询一次策略、约每小时生成进展报告。会话不活跃时仅 worker 持续循环，不会自动启动策略咨询定时任务。无人值守需要另行部署，见[运维手册](operations.zh.md)。

## 3. Worker 研究与事实积累

`euler start <p>` 启动自主循环。成员读取各自 `TASK.md`，按配置的原生后端探索、审阅、形式化或计算，参考共享记忆，并通过 `fact_submit` 提交候选。只有验证结果为 `correct` 才形成 Fact。使用外部专家会话的工作流还需按对应技能配置咨询入口。

```bash
euler status <p>
bash scripts/services.sh up dashboard <p>
```

远程浏览工作台时转发 8099。不要手工修改真值存储，事实图的写入统一经过验证门禁。

## 4. 确认答案与 finalize

主 Agent 在判断所有目标已证且路线可信后，停止继续探索以节省计算，并向你呈报完成依据。若你认为仍需研究，可运行 `euler start`。确认目标结果后执行：

```bash
euler finalize <project> <fact_id> [<fact_id> …]
```

命令将批准的目标定理记录到写作流程读取的 `TARGET.md`。省略 ID 时只列出候选终端事实。`finalize` 本身只记录目标，不修改 worker 状态。

## 5. 生成研究输出

**进展报告**：`human-summary` 的 `summary_write` 生成无内部 ID 的 PDF，说明精确问题、重要局部结果及证明梗概、主要障碍、中立时间线和待补引理。可在研究任意阶段调用，不要求 finalize。

**论文**：`write-paper` 从目标的已验证 Facts 生成独立 `amsart` TeX 与参考文献，编译为 PDF。它要求先确定目标，缺少目标时拒绝写作。

- 开始写论文不会自动停止团队；若整理局部结果时仍需探索其他部分，由主 Agent 与你确认是否停止研究。
- 每个项目可用 `paper_id` 保存多篇论文，默认目录为 `<project>/paper/`。
- 流程依次初稿、编译、引用审计与在线核实，再由专门验证者按实际稿件重新核查整篇数学内容后交付。Agent 工作流见 `agents/skills/write-paper/README.md`。

## 6. 对外操作

向 arXiv 或 LaTeX Git 发布、级联撤销已验证 Fact、超出预算等操作需取得操作者授权，并记录决定。

## 多项目与进度报告

多个项目可并行推进，每项操作均指定项目。主 Agent 根据已观察到的入库记录、退出状态等报告进展；错误、空结果和不确定性直接说明。

进一步查看[命令行与工具](cli-and-tools.zh.md)、[运维与恢复](operations.zh.md)和[验证与访问控制](security-and-trust.zh.md)。
