# Nikoghosyan's Hamiltonicity conjecture

**A 13-year-old open problem in Hamiltonian graph theory (2013–2026), completing the single-forbidden-subgraph classification for 1-tough Hamiltonicity.**

[Read the paper (PDF)](paper.pdf) · [Lean formalization](https://github.com/canghaimeng/Nikoghosyan-conjecture) · [Back to Euler](../../README.md#mathematical-results)

## The question and why it matters

A Hamilton cycle visits every vertex of a graph exactly once. If such a cycle exists, removing a vertex set cannot leave more connected components than removed vertices: this is the necessary condition called **1-toughness**.

This research line reaches back to Chvátal's 1973 toughness framework and Jung's 1978 Hamiltonicity theorem for 1-tough $P_4$-free graphs; see the [historical background](https://arxiv.org/abs/2504.08936). Nikoghosyan conjectured the $P_4 \cup P_1$ case in 2013.

[Li, Broersma and Zhang (2016)](https://ris.utwente.nl/ws/files/253281133/Li2016forbidden.pdf) isolated this as the sole remaining case of a classification. Combining their necessity theorem with the result here gives: for a forbidden graph $H$ of order at least three, every 1-tough $H$-free graph of order at least three is Hamiltonian **if and only if $H$ is an induced subgraph of $P_4 \cup P_1$**.

The completed classification concerns a single forbidden induced subgraph. Chvátal's general toughness conjecture concerns unrestricted graphs and remains a separate question.

## Result

Every finite simple 1-tough graph on at least three vertices with no induced $P_4 \cup P_1$ has a Hamilton cycle. The proof establishes factor existence and its structural prerequisites from the original hypotheses, and constructs a cycle using edges of the original graph.

## Paper

**Qiuyu Chen. _Hamilton cycles in 1-tough graphs with no induced $P_4 \cup P_1$_.**

[paper.pdf](paper.pdf) is the unchanged, reviewed manuscript dated 3 October 2026 in the project archive (32 pages). Its publication status is a locally prepared research manuscript.

Original question: Nikoghosyan, _Disconnected Forbidden Subgraphs, Toughness and Hamilton Cycles_ (2013), Conjecture 7; [preprint](https://arxiv.org/abs/1207.5132).

## Lean formalization

The [separate Lean repository](https://github.com/canghaimeng/Nikoghosyan-conjecture) contains the complete development and build instructions. It currently requires repository access.

The [main declarations](https://github.com/canghaimeng/Nikoghosyan-conjecture/blob/69f3a96df66dd425db33353f2a0bdaa235862050/Nikoghosyan/Main.lean) produce an original-edge spanning cycle and a `SimpleGraph.IsHamiltonian` corollary. The project uses Lean/mathlib 4.34.1; its recorded axiom audit lists only `propext`, `Classical.choice`, and `Quot.sound` for the two exported targets.

[Build record for the linked revision](https://github.com/canghaimeng/Nikoghosyan-conjecture/actions/runs/37029554401).

## What this case illustrates

Understanding proof completeness requires tracing the main theorem back through all its prerequisites and checking that no extra assumptions entered the final statement. This is the kind of dependency and evidence boundary that Euler makes inspectable.
