# 公开案例可视化

<!-- language-nav -->
[English](PUBLIC-CASE-STUDIES.md) · [简体中文](PUBLIC-CASE-STUDIES.zh.md) · [返回 Euler](../README.zh-CN.md)
<!-- /language-nav -->

在 Euler 工作台[阅读三个案例](https://canghaimeng.site/euler/cases/?lang=zh)：

- [Lewis–Overton](https://canghaimeng.site/euler/cases/?project=lewis-overton&lang=zh)
- [Nikoghosyan](https://canghaimeng.site/euler/cases/?project=nikoghosyan&lang=zh)
- [Jankiewicz–Norin–Wise](https://canghaimeng.site/euler/cases/?project=jnw&lang=zh)

公开档案沿用工作台的概览、证明阅读、研究历史和关系图。读者可以切换项目与语言、选择节点、突出依赖，并查看记录中的论证和证据。顶部显示快照发布日期，原始研究日期与证据标签继续保留在对应记录中。

## 发布范围

公开服务只读取三个项目的冻结导出，不包含团队运行环境、服务商凭据、会话恢复归档或模型连接。浏览器资源不包含团队聊天、创建项目、研究干预和恢复操作的客户端。HTTP 服务仅接受 GET 与 HEAD，且只提供清单中列出的观测响应和资源；公开反向代理也拒绝写请求。

个人实时工作台保留原有团队与研究控制。更新公开快照时，重新导出、审阅并替换部署版本。

## 导出已审阅项目

从源码仓库中连接本地观测服务：

```bash
python scripts/public_snapshot.py export \
  --source http://127.0.0.1:8099 \
  --project example-project \
  --prefix /euler/cases \
  --output /tmp/euler-public-case

python /tmp/euler-public-case/server.py serve \
  --root /tmp/euler-public-case --port 8165
```

每个需要发布的项目分别指定一次 `--project`。输出包含当前阅读器资源、导出的 JSON 响应、路由清单、软件声明和独立 Python 服务。运行服务只需要 Python 标准库；导出器拒绝覆盖已有输出目录。

发布前检查导出记录。导出器排除运行账号和会话字段，并替换来源片段中的本地文件路径前缀与本地服务地址；数学命题、依赖、状态和已记录证据保留。作者编写的来源文本仍需进行发布审阅。原始来源哈希标识历史工件，展示片段中可能已移除上述本地位置信息。

在 URL 路径前缀下部署时，将该前缀转发到本机服务。`proxy_pass` 末尾的斜杠用于去除前缀：

```nginx
location = /euler/cases { return 301 /euler/cases/; }
location ^~ /euler/cases/ {
    if ($request_method !~ ^(GET|HEAD)$) { return 405; }
    proxy_pass http://127.0.0.1:8165/;
}
```

使用低权限账号运行服务，快照目录设为只读。仅发布已审阅导出；实时工作台及其凭据保留在私有研究环境。
