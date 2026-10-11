# Euler 项目工作台

网页集中展示一个 Euler 项目的团队运行状态、Fact Graph、Research Graph 和公开研究记录。
它使用原项目的数据；打开网页不会运行模型。配置好的个人项目可以通过明确的发送、保存、恢复或启动操作继续研究，无需额外的网页控制令牌。

## 启动

新读者可以先看[使用指南](DASHBOARD-GUIDE.zh.md)。
网页中的 **User guide / 使用指南** 提供同样的双语内容；当前在线工作台的概览还有
可展开的阅读起点。引导适用于只有研究目标和初始尝试的新项目，不要求先有论文或 Lean 代码。

启动团队后，在单独终端运行下面的命令即可同步观察其已有公开记录。网页服务与团队调度
是独立进程；通过网页明确发送问题或启动任务时，后台才会发起推理。需要开机或断线后持续运行时，将它纳入部署的服务管理。

```bash
python -m pip install '.[observability]'
euler-dashboard --project /absolute/path/to/runtime/projects/my-project --port 8099
```

打开 `http://127.0.0.1:8099`。也可以使用
`python -m euler.observability --project ...`，或现有
`bash scripts/start-dashboard.sh my-project`。网页库、数学排版和字体随安装包提供，
浏览器无需访问 CDN，也无需单独启动 Node 开发服务器。

## 一个项目，多个团队

通过 `--config /absolute/path/to/dashboard.json` 配置展示名称和团队分组。
参见 [`config/dashboard.example.json`](../config/dashboard.example.json)：
`teams[].workers` 引用该项目 `workers/` 下的真实 worker 名称。
配置仅为展示分组，不会创建模型、进程或研究任务。未分组的真实成员仍会显示。
没有配置时展示一个默认团队，不会把不同模型自动当成不同团队。

可选字段：

- `name`、`description`：项目展示信息。
- `teams`：团队 ID、名称、说明、成员及协调者名称/模型。
- `monitor_file`：明确指定的公开监测 JSON；协调者状态与数据时间独立展示。
- `acceptance_file`：明确指定的结构化形式化验收 JSON。

协调者结束、进程停止、Fact 入图、控制器目标关闭以及形式化验收是不同事件。
页面分别呈现来源；旧控制器状态不会覆盖新的验收报告，验收报告也不会改写控制数据库。
未上报、过期或不完整的数据会明确标记。

## 图与交互

**Fact Graph** 读取已记录的事实及显式前驱，支持搜索、成员/团队筛选、依赖追溯、
节点详情和证明阅读。无前驱表示未记录依赖，不能据此称其为公理。

**Research Graph** 从控制事件展示证明义务、研究路线、证据及明确记录的关系。
没有写入事件的论文推理步骤不会自动变成图中的连边。两张图的状态及节点数量各自计算。

图视图提供依赖/网络布局、缩放、适应画布、上下游聚焦、可访问的列表视图及 PNG 导出。
刷新保留当前视图；团队与公开活动帮助定位停止、阻塞和新交付。

## 从 Mac 等另一台设备使用

网页默认监听研究主机的本机端口。在 Mac 终端建立转发：

```bash
ssh -N -L 8099:127.0.0.1:8099 user@research-host
```

然后在 Mac 打开 `http://127.0.0.1:8099`。模型登录、文件和团队都位于研究主机，
浏览器只是你的操作入口。单用户工作流无需再次填写网页控制令牌。
团队与恢复配置见[团队恢复](TEAM-RECOVERY.md)，完整入门顺序见[使用指南](DASHBOARD-GUIDE.zh.md)。

## 团队交互

这是与概览、证明阅读、研究现场、关系图和运行诊断并列的第六个入口。左侧选择当前项目的分支与成员，右侧查看该成员的公开对话历史并继续发送消息。继承的里程碑历史与本分支的新消息分别标记；恢复后的问题作为草稿保留，不会自动发送。

## 技术选择与验证

### 维护介绍与帮助页

使用指南的源文件是
`docs/DASHBOARD-GUIDE{,.zh}.md`。修改后运行 `python scripts/build-reader-guide.py`，
将内容同步到发行包的静态资源。页面沿用已有的 Markdown 渲染器，无需新的模型调用、
后端写接口或外部前端服务。帮助页保留所选项目与语言，返回时仍进入原项目。

为具体项目撰写介绍时，明确列出原问题、提出者与出处、研究意义、当前结果和检查范围；
未知结果保持未知。不要从 Fact 数量、进程状态或部分 Lean 成功推断整个猜想已经完成。

采用 FastAPI 提供同源数据接口与静态页面，保留 Python 项目的一条启动命令。
图渲染使用 [Cytoscape.js](https://js.cytoscape.org/) 的选择、遍历、布局及触摸交互能力；
依赖固定版本并保留许可证。服务部署遵循
[FastAPI 容器部署文档](https://fastapi.tiangolo.com/deployment/docker/)。

```bash
python -m pytest euler/observability/ -q
npm install --prefix /tmp/euler-dashboard-browser playwright
BASE_URL=http://127.0.0.1:8099 node scripts/check-release-ui.mjs
```

浏览器检查使用本机 Chrome 或 `EULER_BROWSER_EXECUTABLE` 指定的浏览器。
它检查实际导航、筛选、图交互及移动端，并保存截图。API 测试覆盖多团队、状态来源、
损坏/缺失记录、只读访问和可选口令；安装包另需检查静态资源是否齐全。

本次实际检查结果见 [RELEASE-VALIDATION.md](../CONTRIBUTING.md)。
