# Nikoghosyan 的 Hamilton 性猜想

<!-- language-nav -->
[English](README.md) · [简体中文](README.zh.md) · [返回 Euler](../../README.zh-CN.md)
<!-- /language-nav -->

**解决 Hamilton 图论中历时 13 年的开放问题（2013–2026），补齐 1-tough 图的单一禁用诱导子图 Hamilton 性分类。**

[查看可视化](https://canghaimeng.site/euler/cases/?project=nikoghosyan&lang=zh) · [阅读论文（PDF）](paper.pdf) · [Lean 形式化](https://github.com/canghaimeng/Nikoghosyan-conjecture) · [返回 Euler](../../README.zh-CN.md#数学成果)

## 问题与意义

Hamilton 圈恰好经过图中每个顶点一次。若图中存在这样的圈，删除一组顶点后，连通分支数不能超过删除顶点数；这就是 **1-toughness** 所表达的必要条件。

这条研究线可以追溯到 Chvátal 于 1973 年提出的韧性框架，以及 Jung 于 1978 年对 1-tough、$P_4$-free 图建立的 Hamilton 性定理，见[历史背景](https://arxiv.org/abs/2504.08936)。Nikoghosyan 于 2013 年提出 $P_4\cup P_1$ 情形的猜想。

[Li、Broersma 与 Zhang（2016）](https://ris.utwente.nl/ws/files/253281133/Li2016forbidden.pdf)将其确定为一个分类中唯一剩余的情形。结合他们的必要性定理与本结果，可以得到：对至少三个顶点的禁用图 $H$，每个至少三个顶点的 1-tough、$H$-free 图都具有 Hamilton 圈，**当且仅当 $H$ 是 $P_4\cup P_1$ 的诱导子图**。

这里完成的是单一禁用诱导子图分类。Chvátal 的一般韧性猜想讨论不受这种禁用条件限制的图，仍是另一个问题。

## 结果

每个至少三个顶点、无诱导 $P_4\cup P_1$ 的有限简单 1-tough 图都有 Hamilton 圈。证明从原始假设推出因子存在性及相关结构前提，并仅使用原图的边构造圈。

## 论文

**Qiuyu Chen. _Hamilton cycles in 1-tough graphs with no induced $P_4\cup P_1$_.**

[paper.pdf](paper.pdf) 是项目归档中未经修改的审阅稿，日期为 2026 年 10 月 3 日，共 32 页；当前属于本地准备的研究手稿。

原问题：Nikoghosyan，_Disconnected Forbidden Subgraphs, Toughness and Hamilton Cycles_（2013），猜想 7；[预印本](https://arxiv.org/abs/1207.5132)。

## Lean 形式化

[独立 Lean 仓库](https://github.com/canghaimeng/Nikoghosyan-conjecture)已公开，包含完整形式化及构建说明。

[主声明](https://github.com/canghaimeng/Nikoghosyan-conjecture/blob/69f3a96df66dd425db33353f2a0bdaa235862050/Nikoghosyan/Main.lean)构造仅使用原图边的生成圈，并给出 `SimpleGraph.IsHamiltonian` 推论。项目使用 Lean/mathlib 4.34.1；记录中的公理审计表明，两个导出目标仅依赖 `propext`、`Classical.choice` 和 `Quot.sound`。

[对应版本的构建记录](https://github.com/canghaimeng/Nikoghosyan-conjecture/actions/runs/37029554401)。

## 这个案例展示了什么

判断证明完整性，需要从主定理追溯所有前提，并检查最终命题是否引入额外假设。Euler 将这样的依赖与证据边界呈现给读者，支持逐步检查。

## 交互式可视化

[在 Euler 工作台打开本案例](https://canghaimeng.site/euler/cases/?project=nikoghosyan&lang=zh)，查看主目标、证明依赖、研究路线和关联来源的推导。选择图中节点可突出其证明路径，并展开相应论证。界面支持中英文。

这里展示历史研究快照，可阅读与浏览图谱；团队消息、创建项目、里程碑恢复和模型调用均已关闭。
