# Git 研究里程碑与恢复后的追问

<!-- language-nav -->
[English](RESEARCH-MILESTONES.md) · [简体中文](RESEARCH-MILESTONES.zh.md) · [返回 Euler](../README.zh-CN.md)
<!-- /language-nav -->

正常工作台打开真实项目及其完整图谱。小型 `euler-human-demo` 项目用于检查干预协议，应使用独立项目目录。

## 保存完整团队工作区

新的 `euler new` 工作区会自动启用本地 Git 历史，首个快照记录团队配置。原生 worker 退出时检查数学状态是否变化，并在整个团队空闲时保存。长任务运行中需要保存时，先暂停并等待团队结束当前工作。保存要求工作区处于静止状态；繁忙项目会推迟保存。仅在需要关闭自动配置时设置 `EULER_MILESTONES=0`。

为已有项目启用快照并登记外部研究文件：

```sh
euler-milestones enable --project /research/project \
  --store /research/history/project --attachment proofs=/research/proofs \
  --dashboard /research/dashboard.json
euler-milestones watch --project /research/project --interval 30
euler-milestones capture --project /research/project --label 'Factor lemma checked'
euler-milestones list --project /research/project
```

`watch` 检查公开数学图的变化；手动 `capture` 也能记录只有文件变化的里程碑。历史保存在项目外的独立私有本地 Git 仓库，不配置远端。快照根目录由操作者配置，项目自身的源码仓库独立管理。

每个快照保留所有普通项目文件，包括未跟踪或被忽略的证明文件、所有 worker 的任务与不解析的私有笔记、共享 Facts、研究事件、回执和工件。SQLite 使用备份 API；复制过程中要求文件清单保持不变。空目录和文件权限会记录，已注册根目录之间的依赖链接恢复为相对链接。清单记录哈希、排除项和未解析的外部链接。

服务商凭据、服务商缓存、进程句柄、Git 元数据以及可重建的依赖缓存不纳入快照；缺少的外部依赖记录在清单中。已注册团队使用下面说明的原生会话归档机制。

## 原生团队状态

对已注册的持久团队，每个提交还包含不解析的原生会话归档、成员名单、目标和消息记录。见[团队恢复](TEAM-RECOVERY.zh.md)。只有文件的历史导入可以恢复已保存文件；恢复对话还需要原生会话归档。

## 回到时间点并追问

在工作台选择**研究里程碑**，选定时间与提交，可填写问题后恢复。结果作为独立工作区出现在项目选择器中。原研究保持原样；已保存的图谱和证明阅读仍可使用。历史展示也固定原始来源片段，后续源文件修改不会改变旧视图。历史团队状态描述保存时刻，新分支另行报告当前执行状态。

```sh
euler-milestones restore --project /research/project --commit FULL_COMMIT_HASH \
  --question 'Which assumptions are needed for this lemma?'
```

恢复会验证全部文件哈希，拒绝仍有活动原生 lease 的状态，将恢复后的原生 campaign 置于 PAUSED，并写入 `RESTORE_ORIGIN.json` 与 `FOLLOWUP.md`。此步骤准备本地工作区；明确启动研究后才开始使用模型。

原生对象上的查看／预览／启动操作继续使用恢复工作区中的调度器与验证门禁。新任务上下文和结果保留来源 Git 提交。已完成的旧任务不会自动重放；服务商需要当前有效凭据和新的执行授权。

历史展示节点可把追问绑定到精确快照，但不会因此获得原生控制身份。旧团队的继续执行需要协调者交接。

## 部署与证据

`EULER_MILESTONE_CONFIG` 指向服务端 JSON，其 `projects` 映射包含项目目录 ID、`capture: true` 和 `settings`（`store`、命名 `roots`、`dashboard`）。`EULER_MILESTONE_REGISTRY` 指向保存恢复项目条目的独立文件。

个人工作台使用同源检查和显式项目允许列表，无需额外的浏览器控制令牌。接口不提供 worker 私有文件或 Git blob。来源项目已有原生执行策略时，恢复分支才继承该策略。

历史导入从给定来源包恢复记录；清单标注可用会话归档和缺少的链接文件。

验证命令 `python -m pytest euler/workspace/tests -q` 覆盖 Git 恢复、多 worker、被忽略文件、SQLite WAL、幂等、活动任务拒绝、保存竞态、私有文件边界和图谱保留。
