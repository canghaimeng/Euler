# Euler 基本概念

<!-- language-nav -->
[English](concepts.md) · [简体中文](concepts.zh.md) · [返回 Euler](../README.zh-CN.md)
<!-- /language-nav -->

本页介绍参与者、记忆分层、验证边界和项目生命周期。系统模块见[架构](../ARCHITECTURE.zh.md)，信任与检查方式见[验证与访问控制](security-and-trust.zh.md)。

## Euler 是什么

Euler 是帮助研究者理解完整推导过程的数学研究智能体系统。团队围绕精确数学问题探索，将命题、假设、候选路线、依赖、障碍和验证证据保存在共享状态中。研究者通过工作台检查这些记录，并通过团队对话、局部任务和保存的里程碑继续研究。

同一表示支持 Agent 规划与协作。候选结果进入研究图，进入 Fact Graph 则遵循配置的验证路径。各项目约定规定闭合目标所需的证据。

## 参与者

- **操作者**：提出问题，判断结果是否回答原问题、是否发布论文，设置预算和运行偏好。通过主 Agent 用自然语言与 Euler 交流。
- **负责人**：协调项目、提出计划、分配工作、监测进展并与你沟通。Gateway 权限支持规划与 Fact 撤销，新 Fact 由 worker 提交验证。
- **团队成员与 worker**：Codex、Claude Code、Gemini、Grok、Cursor 按配置角色探索、检查、形式化和计算。受管 worker 接收有明确范围的任务与执行 lease；原生团队保留各成员会话。
- **验证者**：在独立会话中审阅候选命题与证明，gateway 保存判断与来源。形式化流程提供相应内核和命题对齐证据，详见[验证与访问控制](security-and-trust.zh.md)。
- **策略咨询**：主 Agent 将项目状态整理为 elaboration，咨询强推理模型，并将回复记录为指导后续工作的 `master_guidance`。

## 三层记忆与验证边界

三层记忆按共享范围与证据状态区分：

| 层 | 范围 | 内容 | 能否作为已验收依据 |
| --- | --- | --- | --- |
| 局部记忆 | 单个 worker 私有 | 草稿笔记、动作记录 | 否，属于私有草稿 |
| 全局记忆 | 项目共享 | 类型化发现、主张和依据，包括失败路线 | 否，用于共享研究认知 |
| 事实图 | 项目共享 | 经验证接受的 Facts | 按该项目验证标准接受的依据 |

私有笔记可以形成共享发现，具有可检查依据的发现经 worker 提交并被验证者接受后才成为 Fact。尚未检查的全局记忆不能直接当作已证前提；证明引用已入库 Fact ID。不存在将普通记忆直接升级成 Fact 的快捷通道。

## 事实图

每个 Fact 是一个可读文件节点，包含命题、证明、可选直觉说明、前驱 `predecessors`、引入符号 `glossary_introduces` 和外部引用 `external_refs`。

- **内容寻址**：ID 由问题、前驱、术语、命题和证明的内容哈希确定，相同内容获得相同 ID，便于去重和稳定引用。外部引用不纳入哈希，因此修正文献不会破坏依赖图。
- **有向无环依赖**：`predecessors` 表示已登记的依赖边，内容寻址与前驱检查维护该结构。
- **级联撤销**：撤销 Fact 时，间接依赖它的结论也会撤销；证明不能使用已撤销前驱。
- **可独立阅读**：轻量术语检查要求使用的符号具有定义。

精确存储字段和结构见代码目录中的 `euler/core/DATA_MODEL.md`。

## 策略循环

主 Agent 在产生实质新研究状态时协调后续工作：

1. **整理**：通过 `elaboration` 技能汇总判断、已闭合路线、接口、风险和缺少的桥接引理。
2. **咨询**：通过 `consult` 技能向强推理模型咨询。
3. **记录并派发**：把回复完整保存为 `master_guidance`，分配各 worker 的轮次任务。旧非受管流程使用 `euler assign`；受管操作使用 propose／schedule 和 lease。
4. **监测**：查看进展，有新的实质状态后重复循环。

咨询传输可选 `gpt_pro`（默认，付费 OpenAI 兼容模型）、`claude_api`（自备 Anthropic Key）、`claude_code`（通过 CLI 使用 Claude 订阅）或 `off`（主 Agent 自行推理，不发起额外咨询）。Worker 使用各自声明的后端；验证者默认显式配置为 `grok`，也可明确选用 `cursor/codex`，不静默切换。

## 项目生命周期

```text
初始化 → 新建项目 → 策略循环 ⇄ worker 团队 → 验证 → 事实图
                         │                         │
                    随时生成报告              确认答案／finalize
                                                   │
                                               论文写作
```

- **initialize**：初次配置操作者偏好、Codex 后端、咨询方式、Git 分支和预算，启动 verify。
- **new**：建立 `PROBLEM.md` 与成员名单，一个项目对应一个问题及其独立记忆与事实图。
- **策略与 worker**：循环探索、证明、提交，逐步积累 Facts。
- **verify**：每次提交均经过验证，`correct` 后才写入 Fact。
- **human-summary**：从已验证结果生成私有 PDF 进展报告，任意阶段可用，不依赖 finalize。
- **finalize**：由你判断已验证 Fact 是否为原问题答案。主 Agent 判断目标已证时可以先停止探索以节省计算，再请你确认；若仍需研究可运行 `euler start`。
- **write-paper**：将已确认目标的已验证事实组织为论文，由专用数学验证者按实际全文复核后交付。

## 实现与证据

代码落实 gateway 权限、入库状态变化、内容寻址记录和 worker 恢复。各检查路径提供的证据见[验证与访问控制](security-and-trust.zh.md)，实现边界见[架构](../ARCHITECTURE.zh.md)。

## 接下来读什么

- 配置与运行：[部署入门](getting-started.zh.md)、[操作指南](operating-guide.zh.md)。
- 命令与工具：[CLI 参考](cli-and-tools.zh.md)。
- 结果的证据含义：[验证与访问控制](security-and-trust.zh.md)。
- 设计与模块：[架构](../ARCHITECTURE.zh.md)；存储字段参见源码文档 `euler/core/DATA_MODEL.md`。
