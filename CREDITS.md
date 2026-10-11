# Credits

Euler Agent is by **canghaimeng**. The software author is **Qiuyu Chen**,
Shanghai Jiao Tong University.

## Danus

Euler builds on the architecture and software foundations of
[Danus](https://github.com/frenzymath/Danus), released by FrenzyMath under the
Apache License 2.0.

Danus is described in *Danus: Orchestrating Mathematical Reasoning Agents with
Fact-Graph Memory* ([arXiv:2607.06447](https://arxiv.org/abs/2607.06447)) by
Jihao Liu, Guoxiong Gao, Zeming Sun, Bin Wu, Shurui Liu, Jiedong Jiang,
Haocheng Ju, Leheng Chen, Ronnie Cheng, Xiping Zhang, and Bin Dong.

We thank the Danus authors and developers for their work on mathematical-agent
orchestration, verifier-gated facts, memory, and research workflows.

## Rethlas

Euler also builds on ideas and proving workflows from
[Rethlas](https://github.com/frenzymath/Rethlas), released by FrenzyMath under
the Apache License 2.0.

Rethlas is described in *Automated Conjecture Resolution with Formal
Verification* ([arXiv:2604.03789](https://arxiv.org/abs/2604.03789)) by
Haocheng Ju, Guoxiong Gao, Jiedong Jiang, Bin Wu, Zeming Sun, Shurui Liu,
Leheng Chen, Yutong Wang, Yuefeng Wang, Zichen Wang, Wanyi He, Peihao Wu,
Liang Xiao, Ruochuan Liu, Bryan Dai, and Bin Dong.

We thank the Rethlas authors and developers for their work on mathematical
reasoning, proof-generation and verification workflows, and reusable proving
skills.

## Licensing

Danus and Rethlas are distributed under the Apache License 2.0. Euler Agent is
also distributed under the Apache License 2.0. See [LICENSE](LICENSE) and
[NOTICE](NOTICE).

Source files carrying the header below contain code adapted from the upstream
projects and modified for Euler; newly implemented Euler modules do not carry
that adaptation header:

```text
SPDX-License-Identifier: Apache-2.0
Adapted from Danus/Rethlas; modified for Euler Agent by canghaimeng.
```


## Bundled workbench dependencies

The workbench bundles Cytoscape.js, markdown-it and KaTeX (including fonts) for offline
viewing. Their versions, source URLs and original license texts are preserved under
`euler/observability/static/vendor/`. These dependencies retain their own licenses.
