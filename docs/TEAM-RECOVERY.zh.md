# 恢复整个研究团队

<!-- language-nav -->
[English](TEAM-RECOVERY.md) · [简体中文](TEAM-RECOVERY.zh.md) · [返回 Euler](../README.zh-CN.md)
<!-- /language-nav -->

团队检查点将两类内容保存在同一个私有 Git 提交中：

- 项目文件、数学状态、依赖关系、任务分配和执行回执。
- 每个已注册成员的原生会话归档、模型与 effort、角色、目标元数据、待处理请求，以及负责人与助手之间的消息关系。

恢复后的项目彼此独立。Codex 从冻结的 rollout 副本执行原生 `thread/fork`；Grok 从已保存的会话目录执行原生会话分支 RPC。这两种服务商生成新会话 ID，并保留父会话 ID。Claude 在首次发送消息时创建分支；Gemini 和 Cursor 在分支私有目录中恢复完整原生存储，保留服务商 ID。详见 [CLI 支持](CLI-SUPPORT.zh.md)。后续提问通过相应服务商的恢复机制继续已保存的原生会话。

## 启动持久团队

需要恢复成员会话的新项目应使用此团队宿主。它支持 Linux 和 macOS 上的 Codex、Claude Code、Gemini CLI、原生 Grok 和 Cursor Agent。会话恢复要求检查点包含原生会话归档；导入外部会话需要对应服务商适配器支持。

准备项目目录，并编写如下团队定义：

```json
{
  "objective": "For every positive integer N, find a prime p > N.",
  "max_turns": 12,
  "turn_seconds": 300,
  "members": [
    {"id":"codex","role":"lead","provider":"codex",
     "model":"gpt-6.1-sol","effort":"high","assignment":"Coordinate the proof and consult Grok."},
    {"id":"grok","role":"assistant","provider":"grok",
     "model":"grok-4.6","effort":"high","assignment":"Check the construction independently."}
  ]
}
```

```sh
euler-team init --project /research/project --definition team.json
euler-team prepare --project /research/project
euler-team ask --project /research/project --member codex \
  --question 'Develop the proof and ask Grok to check its critical step.'
euler-team status --project /research/project
```

`prepare` 连接原生会话，或准备首次提问时的创建／分支步骤，不启动推理。原生会话准备完成以及一批消息处理结束后，在整个团队空闲时自动创建 Git 里程碑。数学图未变化时，检查点仍可保存会话变化；数学进展通过义务和已验收证据记录。

负责人具有成员管理、发消息、委派和提出证明步骤的工具。助手回复返回负责人原有会话，宿主保留因果消息 ID。提出的新义务在验证前保持未闭合；讨论与同行审阅为 Fact 提交和最终审计提供候选内容。

`max_turns` 可限制分支中新发生的服务商轮次，每个成员也可单独设置。上限由用户配置，省略则不设限。`turn_seconds` 限制非 Grok 的轮次；**Grok 不设整轮固定截止时间**。账户百分比监控需要主动启用，发布默认配置不启用该限制；已有本地监控策略继续保留。凭据使用当前运行配置。

## 保存与恢复

在团队空闲时使用 Git 里程碑流程。若成员仍在执行，保存会被拒绝。服务商文件作为不解析的原生内容复制，检查并发修改、计算哈希，并与项目一起提交。这些文件不会由浏览器提供下载，也不包含在公开源码包中。

```sh
euler-milestones enable --project /research/project
euler-milestones capture --project /research/project --label 'Draft and peer review'
euler-milestones restore --project /research/project --commit FULL_COMMIT_HASH
euler-team prepare --project /path/printed/by/restore
```

恢复保留成员名单与历史消息，暂停队列中的任务，将原有轮次用量与新分支额度分开记录。所有继承会话必须成功恢复后，才能开始新问题。新加入成员在首次任务时连接原生会话；缺少归档会产生恢复错误。检查点保留原生目标，研究在发送问题或明确恢复队列任务后继续。

## 浏览器中的操作

通过**研究里程碑**选择时间点并恢复工作区。新分支会打开**团队交互**：先恢复成员会话，再在左侧选择负责人或助手，在右侧继续对话。直接使用第六个导航入口，也能进入当前项目分支，无需先恢复或选择节点。

证明节点的**从这一步继续研究**区域提供**向当前团队追问**和**从里程碑追问**。前者把所选节点身份带入当前团队对话；后者从已保存状态准备独立工作区。入口显示可用状态，并说明何时缺少会话归档。成员列表与可展开的会话详情显示服务商、模型、effort、恢复状态、来源／分支 ID 和目标元数据。继承的检查点历史与当前分支新消息分别标记。切换分支会同时改变展示的历史和后续消息的实际接收者。

会话视图显示公开问题与回复，包括委派咨询。工作台通过会话接口连接原生会话，模型内部推理保留在执行主机。**查看对象记录**打开版本、范围和证据详情。启用单项任务控制后，**查看对象／单项任务**提供独立的有界操作接口。没有原生身份的历史记录保持只读，仍可阅读证明与依赖。

个人工作台无需单独的研究控制令牌，可以直接通过按钮发送、保存和恢复。执行主机仍需登录模型账号，已配置的额度与轮次限制继续生效。完整配置和保存／恢复顺序见[使用指南](DASHBOARD-GUIDE.zh.md)。

通过 `EULER_TEAM_CONFIG` 启用已注册团队的控制：

```json
{"projects":{"my-project":{"allow_prepare":true,"allow_launch":true,
  "max_turns":12,"turn_seconds":300}}}
```

只有服务端配置的目录项目可以启动。恢复项目仅在来源项目已明确配置时继承执行策略。Pause 暂停排队任务并中断当前轮次；Stop 还禁止继续提交请求。取消记录在任务生命周期中，数学证据与验收状态保留。

## 隔离与证据边界

执行使用 Linux Landlock ABI 3+ 或通过 `sandbox-exec` 使用 macOS Seatbelt。服务商进程可写入新工作区，禁止访问来源文件内容及写入来源路径，包括符号链接。为支持包导入，允许列出祖先目录。此隔离约束文件系统访问；网络策略、当前凭据、可执行程序版本和新进程句柄由宿主机提供。

随附协议测试使用本地 CLI 模拟程序与合成会话数据，验证检查点、分支隔离和原生会话恢复。

## 在浏览器创建项目与添加成员

**研究里程碑**旁的**创建项目**接受用户自定名称、负责人 CLI／模型／effort、目标类型和首条消息。后端创建独立的受管工作区，将首条要求记录为未闭合目标，注册项目并启动持久对话。请求未给出的定义和量词明确保留为未指定。

成员列表下的 **＋ 添加成员**可为当前分支添加具名助手或审阅者。正在工作的负责人也可调用 `euler_team_create_member`，再调用 `euler_team_ask`。工具记录添加者与父消息；普通助手不能注册队友或替换负责人。新成员首次任务时创建新的原生会话；已有成员和恢复成员的会话保留。名单变化纳入检查点。

启动脚本配置 `EULER_PROJECT_CREATION_CONFIG`，指向服务端持有的 JSON 文件，包含 `allow_create`、`root`、`registry` 和可选 CLI `defaults`。团队与里程碑策略继续通过各自环境变量配置。浏览器请求不能选择文件系统路径、可执行程序或凭据。
