# The Lewis–Overton BFGS rate conjecture

**A 2008 conjecture on a fundamental optimization method, refuted in its initialization-independent sharp-rate assertion.**

[Read the paper (PDF)](paper.pdf) · [arXiv](https://arxiv.org/abs/2609.09872) · [Lean formalization](https://github.com/canghaimeng/lewis-overton-bfgs-rate) · [Back to Euler](../../README.md#mathematical-results)

## The question and why it matters

BFGS is a standard quasi-Newton optimization method. Its success on nonsmooth problems motivated [Adrian Lewis](https://www.duffield.cornell.edu/people/adrian-s-lewis/) of Cornell and [Michael Overton](https://cs.nyu.edu/~overton/), NYU's Silver Professor of Computer Science and Mathematics, to study simple models in which its behavior could be understood exactly. Both researchers are SIAM Fellows.

Their [2008 preprint](https://optimization-online.org/2008/12/2172/), Conjecture 4.1, predicts a convergence rate determined by the tilt parameter, independent of initialization. Trial normalization counts failed line-search trials as well as accepted steps, so the question concerns actual line-search effort as well as convergence.

## Result

For every $9/5 < u < 13/6$, the tilted absolute-value objective $f_u(x)=\max\{x,-ux\}$ admits two nonterminating executions with distinct sharp trial-normalized rates under the specified Lewis–Overton line-search conventions. Thus initialization affects the exact asymptotic rate, even in this one-dimensional model.

This refutes the initialization-independent **sharp-rate** assertion. It does not assert divergence of BFGS or rule out a shared non-sharp upper bound. The paper and formalization fix the Armijo, Wolfe, and trial-count conventions precisely.

## Paper

**Qiuyu Chen. _Initialization-dependent BFGS trial rates for tilted absolute values_.**

[paper.pdf](paper.pdf) is the unchanged [arXiv:2609.09872v1](https://arxiv.org/abs/2609.09872v1), submitted 9 September 2026 (7 pages). It retains the paper's [CC BY-NC-SA 4.0 license](https://creativecommons.org/licenses/by-nc-sa/4.0/).

## Lean formalization

The [public Lean repository](https://github.com/canghaimeng/lewis-overton-bfgs-rate) formalizes the parameterized orbits, source line search, and unequal exact rates using Lean/mathlib 4.33.0.

Start with [OpenIntervalFamily.lean](https://github.com/canghaimeng/lewis-overton-bfgs-rate/blob/58a7f9d981e1815ba0b59b5e8325edb49029ebf9/LewisOverton/OpenIntervalFamily.lean) and its repository's source guide. [Build record for the linked revision](https://github.com/canghaimeng/lewis-overton-bfgs-rate/actions/runs/34487552156).

## What this case illustrates

A counterexample is meaningful only against the exact conjectured assertion. Euler's scope and dependency views help readers distinguish the two executions, their assumptions, and the precise rate comparison they establish.
