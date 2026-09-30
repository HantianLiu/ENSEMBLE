import pytest
from project_ensemble.orchestration.amendments import AmendmentDocket, AmendmentSubmission, AmendmentType, AmendmentConflictGraph
from project_ensemble.errors import PolicyNotConfiguredError


def sample(aid):
    return AmendmentSubmission(amendment_id=aid, proposer_id="R1", text=aid, declared_type=AmendmentType.SUPPLEMENTARY, declared_impact_scope=["C1"])


def test_order_is_not_invented():
    d = AmendmentDocket([sample("A1"), sample("A2")])
    with pytest.raises(PolicyNotConfiguredError):
        d.ordered(None)
    assert [x.amendment_id for x in d.ordered(["A2", "A1"])] == ["A2", "A1"]


def test_conflict_graph_is_explicit():
    g = AmendmentConflictGraph()
    assert not g.conflicts("A1", "A2")
    g.add("A1", "A2", reason="Chair ruling", ruling_id="CR1")
    assert g.conflicts("A1", "A2")


def test_random_order_is_replayable_and_complete():
    docket = AmendmentDocket([sample("A1"), sample("A2"), sample("A3")])
    seed = "42" * 32
    first = docket.randomized(seed=seed)
    second = docket.randomized(seed=seed)
    assert first == second
    assert set(first.order) == {"A1", "A2", "A3"}
    assert [x.amendment_id for x in docket.ordered(first.order)] == first.order


def test_random_order_rejects_duplicate_ids_and_invalid_seed():
    with pytest.raises(ValueError, match="unique"):
        AmendmentDocket([sample("A1"), sample("A1")])
    with pytest.raises(ValueError, match="32 bytes"):
        AmendmentDocket([sample("A1")]).randomized(seed="short")
