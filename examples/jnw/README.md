# The Jankiewicz–Norin–Wise legal-system problem

<!-- language-nav -->
[English](README.md) · [简体中文](README.zh.md) · [Back to Euler](../../README.md)
<!-- /language-nav -->

**An explicit solution of the four-connected form of Jankiewicz–Norin–Wise Problem 5.2.**

[Explore the visualization](https://canghaimeng.site/euler/cases/?project=jnw&lang=en) · [Read the paper (PDF)](paper.pdf) · [arXiv](https://arxiv.org/abs/2609.13419) · [Lean formalization](https://github.com/canghaimeng/JNWLegalSystemObstruction) · [Back to Euler](../../README.md#mathematical-results)

## The question and why it matters

Kasia Jankiewicz, Sergey Norin, and Daniel T. Wise introduced legal systems as finite combinatorial certificates used to construct virtual algebraic fibrations of right-angled Coxeter groups. This connects concrete graph structure with questions in geometric group theory.

The question appears as **Problem 5.2 in their [2017 preprint](https://arxiv.org/pdf/1711.11505v1#page=7)**, later [published in 2021](https://doi.org/10.1017/S1474748019000422). It asks for a graph with strong connectivity and nonnegative curvature whose four-connected ordinary subgraphs nevertheless admit no legal system. A solution shows that those global conditions alone do not guarantee this certificate method will succeed.

## The researchers behind the question

- **[Kasia Jankiewicz](https://people.ucsc.edu/~kjankiew/)** is an Associate Professor at the University of California, Santa Cruz, working in geometric group theory.
- **[Sergey Norin](https://www.mcgill.ca/mathstat/sergey-norin)** is a Professor and the Peter Redpath Chair of Pure Mathematics at McGill University, working in graph theory and combinatorics.
- **[Daniel Wise](https://royalsociety.org/people/daniel-wise-13854/)** is a James McGill Professor and a Fellow of the Royal Society, known for his work in geometric group theory and low-dimensional topology.

These are their current professional profiles; the problem's date is taken from the 2017 preprint.

## Result

The construction has **33 vertices and 66 edges**. It is 4-regular and four-connected, has girth four and Charney–Davis curvature one, and has no four-connected ordinary subgraph admitting a legal system.

This answers the **four-connected** question. The stronger parenthetical requirement concerning all three-connected ordinary subgraphs is a separate question. An ordinary subgraph may select a subset of both vertices and edges.

## Paper

**Qiuyu Chen. _A Four-Connected Graph without a Legal System_.**

[paper.pdf](paper.pdf) is the unchanged [arXiv:2609.13419v1](https://arxiv.org/abs/2609.13419v1), submitted 11 September 2026 (14 pages). It retains the paper's [CC BY-NC-SA 4.0 license](https://creativecommons.org/licenses/by-nc-sa/4.0/).

## Lean formalization

The [public Lean repository](https://github.com/canghaimeng/JNWLegalSystemObstruction) uses Lean/mathlib 4.33.0. Its [main theorem](https://github.com/canghaimeng/JNWLegalSystemObstruction/blob/e32027f2a95074deeed0605528db22e6b46994d3/JNWLegalSystem/Main.lean) is `JNW.Vertex.jnw_problem5_2_four_connected`.

General obstruction arguments are structural proofs; several finite graph certificates use `native_decide`. The repository explains this computational verification boundary. [Build record for the linked revision](https://github.com/canghaimeng/JNWLegalSystemObstruction/actions/runs/34931664980).

## What this case illustrates

Ruling out individual candidates can motivate a change of construction strategy without ruling out an entire graph class. Euler records these local failures and route changes so that researchers can understand what each unsuccessful attempt actually establishes.

## Interactive visualization

[Open this case in the Euler workbench](https://canghaimeng.site/euler/cases/?project=jnw&lang=en) to explore the main goal, proof dependencies, recorded research routes, and source-linked derivations. Select a graph node to highlight its proof path and open the supporting argument. The interface supports English and Chinese.

This public archive is a snapshot of the historical investigation. It provides reading and graph navigation; team messaging, project creation, milestone restoration, and model calls are disabled.
