# The Jankiewicz–Norin–Wise legal-system problem

**An explicit solution of the four-connected form of JNW Problem 5.2.**

[Read the paper (PDF)](paper.pdf) · [arXiv](https://arxiv.org/abs/2609.13419) · [Lean formalization](https://github.com/canghaimeng/JNWLegalSystemObstruction) · [Back to Euler](../../README.md#mathematical-results)

## The question and why it matters

Kasia Jankiewicz, Sergey Norin, and Daniel T. Wise introduced legal systems as finite combinatorial certificates used to construct virtual algebraic fibrations of right-angled Coxeter groups. This connects concrete graph structure with questions in geometric group theory.

In [Problem 5.2 of their 2021 paper](https://doi.org/10.1017/S1474748019000422), they ask for a graph with strong connectivity and nonnegative curvature whose four-connected ordinary subgraphs nevertheless admit no legal system. A solution shows that those global conditions alone do not guarantee this certificate method will succeed.

## Result

The construction has **33 vertices and 66 edges**. It is 4-regular and four-connected, has girth four and Charney–Davis curvature one, and has no four-connected ordinary subgraph admitting a legal system.

This answers the **four-connected** question. The stronger parenthetical requirement concerning all three-connected ordinary subgraphs is not claimed. Ordinary subgraphs here need not be induced.

## Paper

**Qiuyu Chen. _A Four-Connected Graph without a Legal System_.**

[paper.pdf](paper.pdf) is the unchanged [arXiv:2609.13419v1](https://arxiv.org/abs/2609.13419v1), submitted 11 September 2026 (14 pages). It retains the paper's [CC BY-NC-SA 4.0 license](https://creativecommons.org/licenses/by-nc-sa/4.0/).

## Lean formalization

The [public Lean repository](https://github.com/canghaimeng/JNWLegalSystemObstruction) uses Lean/mathlib 4.33.0. Its [main theorem](https://github.com/canghaimeng/JNWLegalSystemObstruction/blob/e32027f2a95074deeed0605528db22e6b46994d3/JNWLegalSystem/Main.lean) is `JNW.Vertex.jnw_problem5_2_four_connected`.

General obstruction arguments are structural proofs; several finite graph certificates use `native_decide`. The repository explains this computational verification boundary. [Build record for the linked revision](https://github.com/canghaimeng/JNWLegalSystemObstruction/actions/runs/34931664980).

## What this case illustrates

Ruling out individual candidates can motivate a change of construction strategy without ruling out an entire graph class. Euler records these local failures and route changes so that researchers can understand what each unsuccessful attempt actually establishes.
