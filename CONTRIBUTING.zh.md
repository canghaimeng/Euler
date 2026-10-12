# 参与 Euler 开发

<!-- language-nav -->
[English](CONTRIBUTING.md) · [简体中文](CONTRIBUTING.zh.md) · [返回 Euler](README.zh-CN.md)
<!-- /language-nav -->

提交问题时，请说明实际行为、预期行为和最小复现步骤。使用合成示例，并移除凭据和私人研究记录。

运行 `python -m pip install -e '.[dev]'` 安装开发依赖，再运行 `python -m pytest euler/ -q`。各子系统的测试与模块放在一起。修改时保留类型注解、许可声明和周围代码的风格。

Pull Request 应注明受影响模块、验证命令及结果；界面改动请附截图。保持改动聚焦，提交标题可使用 `fix: ...`、`docs: ...`、`feat: ...`。

数学结果必须经过 Fact 提交与验证流程。基础设施错误记录为运行故障。运行状态和账号配置保存在被 Git 忽略的本地文件中，并保留代码来源署名。

Euler 原创贡献采用[许可说明](LICENSING.zh.md)中的条款。请保留上游和第三方材料的许可声明。
