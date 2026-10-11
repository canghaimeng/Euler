# SPDX-License-Identifier: Apache-2.0
# Adapted from Danus/Rethlas; modified for Euler Agent by canghaimeng.
from euler.strategy.sol_policy import (
    HARD_CAP,
    SOFT_CAP,
    TAKEOVER_CAP,
    ResearchState,
    TheoremBudget,
    decide,
)


def test_initial_and_closure_always_consult():
    b = TheoremBudget("T")
    assert decide(ResearchState.INITIAL, b).consult
    assert decide(ResearchState.CLOSURE, b).consult
    assert decide(ResearchState.CONTRADICTION, b).consult
    assert decide(ResearchState.STAGNATED, b).consult


def test_normal_self_manages():
    d = decide(ResearchState.NORMAL, TheoremBudget("T"))
    assert d.consult is False
    assert d.reason == "grok_main_self_manage"


def test_checkpoint_requires_progress_and_gap():
    b = TheoremBudget("T", rounds_since_sol=2)
    assert decide(ResearchState.MILESTONE, b, major_progress=False).consult is False
    assert decide(ResearchState.MILESTONE, b, major_progress=True).consult is True


def test_caps():
    hard = TheoremBudget("T", calls=HARD_CAP)
    assert decide(ResearchState.CLOSURE, hard).consult is False
    soft = TheoremBudget("T", calls=SOFT_CAP)
    assert decide(ResearchState.STAGNATED, soft).consult is False
    assert decide(ResearchState.CLOSURE, soft).consult is True
    take = TheoremBudget("T", takeovers=TAKEOVER_CAP)
    assert decide(ResearchState.NORMAL, take, takeover=True).consult is False
