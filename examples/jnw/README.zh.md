# Jankiewicz–Norin–Wise 的 legal-system 问题

<!-- language-nav -->
[English](README.md) · [简体中文](README.zh.md) · [返回 Euler](../../README.zh-CN.md)
<!-- /language-nav -->

**以显式构造解决 Jankiewicz–Norin–Wise 问题 5.2 的四连通形式。**

[查看可视化](https://canghaimeng.site/euler/cases/?project=jnw&lang=zh) · [阅读论文（PDF）](paper.pdf) · [arXiv](https://arxiv.org/abs/2609.13419) · [Lean 形式化](https://github.com/canghaimeng/JNWLegalSystemObstruction) · [返回 Euler](../../README.zh-CN.md#数学成果)

## 问题与意义

Kasia Jankiewicz、Sergey Norin 与 Daniel T. Wise 引入 legal systems，作为构造直角 Coxeter 群的虚代数纤维化的有限组合证书，将具体图结构与几何群论联系起来。

问题见于他们 [2017 年预印本](https://arxiv.org/pdf/1711.11505v1#page=7)的**问题 5.2**，后于 [2021 年发表](https://doi.org/10.1017/S1474748019000422)。它询问是否存在满足强连通性和非负曲率条件、却在所有四连通普通子图中都没有 legal system 的图。这样的构造说明，仅凭这些全局条件不足以保证该证书方法成功。

## 问题提出者

- **[Kasia Jankiewicz](https://people.ucsc.edu/~kjankiew/)**：加州大学圣克鲁兹分校副教授，研究几何群论。
- **[Sergey Norin](https://www.mcgill.ca/mathstat/sergey-norin)**：麦吉尔大学教授、Peter Redpath Chair of Pure Mathematics，研究图论与组合数学。
- **[Daniel Wise](https://royalsociety.org/people/daniel-wise-13854/)**：James McGill Professor、英国皇家学会会士，以几何群论和低维拓扑研究著称。

以上身份依据对应学者资料，问题提出时间取自 2017 年预印本。

## 结果

构造包含 **33 个顶点、66 条边**，是 4-正则、四连通图，围长为四、Charney–Davis 曲率为一，且不存在具有 legal system 的四连通普通子图。

这回答了**四连通**问题。原文括号中要求覆盖所有三连通普通子图的更强变体仍是独立问题。普通子图可同时选择顶点子集和边子集。

## 论文

**Qiuyu Chen. _A Four-Connected Graph without a Legal System_.**

[paper.pdf](paper.pdf) 保留未经修改的 [arXiv:2609.13419v1](https://arxiv.org/abs/2609.13419v1)，提交于 2026 年 9 月 11 日，共 14 页，采用原论文的 [CC BY-NC-SA 4.0 许可](https://creativecommons.org/licenses/by-nc-sa/4.0/)。

## Lean 形式化

[公开 Lean 仓库](https://github.com/canghaimeng/JNWLegalSystemObstruction)使用 Lean/mathlib 4.33.0，[主定理](https://github.com/canghaimeng/JNWLegalSystemObstruction/blob/e32027f2a95074deeed0605528db22e6b46994d3/JNWLegalSystem/Main.lean)为 `JNW.Vertex.jnw_problem5_2_four_connected`。

一般性障碍论证使用结构证明，部分有限图证书使用 `native_decide`；仓库说明了这一计算验证边界。[对应版本的构建记录](https://github.com/canghaimeng/JNWLegalSystemObstruction/actions/runs/34931664980)。

## 这个案例展示了什么

排除某些候选可以推动构造策略变化，但不能由此排除整类图。Euler 保存局部失败和路线变化，让研究者理解每次失败究竟建立了什么。

## 交互式可视化

[在 Euler 工作台打开本案例](https://canghaimeng.site/euler/cases/?project=jnw&lang=zh)，查看主目标、证明依赖、研究路线和关联来源的推导。选择图中节点可突出其证明路径，并展开相应论证。界面支持中英文。

这里展示历史研究快照，可阅读与浏览图谱；团队消息、创建项目、里程碑恢复和模型调用均已关闭。
