# Lewis–Overton 的 BFGS 速率猜想

<!-- language-nav -->
[English](README.md) · [简体中文](README.zh.md) · [返回 Euler](../../README.zh-CN.md)
<!-- /language-nav -->

**Lewis 与 Overton 所研究的经典 BFGS 模型中，精确渐近速率对初始化的依赖。**

[查看可视化](https://canghaimeng.site/euler/cases/?project=lewis-overton&lang=zh) · [阅读论文（PDF）](paper.pdf) · [arXiv](https://arxiv.org/abs/2609.09872) · [Lean 形式化](https://github.com/canghaimeng/lewis-overton-bfgs-rate) · [返回 Euler](../../README.zh-CN.md#数学成果)

## 问题与意义

BFGS 是标准的拟牛顿优化方法。它在非光滑问题上的表现，促使康奈尔大学的 [Adrian Lewis](https://www.duffield.cornell.edu/people/adrian-s-lewis/) 与纽约大学计算机科学及数学 Silver Professor [Michael Overton](https://cs.nyu.edu/~overton/) 研究可以精确分析的简单模型。两位学者均为 SIAM Fellow。

他们在 [2008 年预印本](https://optimization-online.org/2008/12/2172/)的猜想 4.1 中提出：收敛速率由倾斜参数决定，与初始化无关。Trial 归一化同时统计失败的线搜索试探与被接受的步骤，因此问题也涉及实际线搜索工作量。

## 结果

对每个 $9/5 < u < 13/6$，倾斜绝对值目标 $f_u(x)=\max\{x,-ux\}$ 在指定的 Lewis–Overton 线搜索约定下，都存在两条不终止的执行轨道，具有不同的精确 trial-normalized 速率。因此，即使在这个一维模型中，初始化也会影响精确渐近速率。

两条轨道表明**精确速率可以依赖初始化**，且两者都收敛。不同的精确速率仍可共同满足同一个非紧上界。论文与形式化精确固定了 Armijo、Wolfe 和 trial 计数约定。

## 论文

**Qiuyu Chen. _Initialization-dependent BFGS trial rates for tilted absolute values_.**

[paper.pdf](paper.pdf) 保留未经修改的 [arXiv:2609.09872v1](https://arxiv.org/abs/2609.09872v1)，提交于 2026 年 9 月 9 日，共 7 页，采用原论文的 [CC BY-NC-SA 4.0 许可](https://creativecommons.org/licenses/by-nc-sa/4.0/)。

## Lean 形式化

[公开 Lean 仓库](https://github.com/canghaimeng/lewis-overton-bfgs-rate)使用 Lean/mathlib 4.33.0，形式化参数化轨道、原始线搜索及不相等的精确速率。

可从 [OpenIntervalFamily.lean](https://github.com/canghaimeng/lewis-overton-bfgs-rate/blob/58a7f9d981e1815ba0b59b5e8325edb49029ebf9/LewisOverton/OpenIntervalFamily.lean) 和仓库源码导览开始。[对应版本的构建记录](https://github.com/canghaimeng/lewis-overton-bfgs-rate/actions/runs/34487552156)。

## 这个案例展示了什么

反例需要对应精确的猜想命题。Euler 的范围与依赖视图帮助读者区分两条执行轨道、各自假设，以及它们实际建立的速率比较。

## 交互式可视化

[在 Euler 工作台打开本案例](https://canghaimeng.site/euler/cases/?project=lewis-overton&lang=zh)，查看主目标、证明依赖、研究路线和关联来源的推导。选择图中节点可突出其证明路径，并展开相应论证。界面支持中英文。

这里展示历史研究快照，可阅读与浏览图谱；团队消息、创建项目、里程碑恢复和模型调用均已关闭。
