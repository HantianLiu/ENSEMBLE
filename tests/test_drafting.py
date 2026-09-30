from project_ensemble.governance_private.drafting import AtomicDraftItem, calculate_drafting_alignment, choose_consultative, primary_drafter_candidates


def test_drafting_alignment_authorship_and_cosponsor():
    items = [
        AtomicDraftItem(item_id="I1", adopted=True, author="R1", co_sponsors={"R2"}, source_type="initial_draft"),
        AtomicDraftItem(item_id="I2", adopted=True, author="R2", co_sponsors={"R1"}, source_type="amendment"),
        AtomicDraftItem(item_id="I3", adopted=False, author="R1", co_sponsors={"R2"}, source_type="amendment"),
    ]
    r = calculate_drafting_alignment(items, ["R1", "R2", "R3"])
    assert r.scores == {"R1": 1.5, "R2": 1.5, "R3": 0.0}
    assert primary_drafter_candidates(r.scores) == ["R1", "R2"]


def test_consultative_floor_and_ties_retained():
    # N=10 => k=floor(3). The third and fourth scores tie at the cutoff, so the
    # whole tied group stays ACTIVE and only the strictly lower R0 is converted.
    scores = {"R0": 0, "R1": 1, "R2": 1, "R3": 1, "R4": 3, "R5": 4, "R6": 5, "R7": 6, "R8": 7, "R9": 8}
    assert choose_consultative(scores, 0.30) == {"R0"}


def test_consultative_never_exceeds_floor_fraction():
    scores = {f"R{i}": float(i) for i in range(12)}
    selected = choose_consultative(scores, 0.30)
    assert len(selected) <= 3


def test_consultative_converts_the_full_quota_without_a_cutoff_tie():
    scores = {f"R{i}": float(i) for i in range(10)}
    assert choose_consultative(scores, 0.30) == {"R0", "R1", "R2"}


def test_coauthored_item_credits_each_direct_author_without_self_cosponsorship():
    item = AtomicDraftItem(
        item_id="FRAGMENT-COMMON",
        adopted=True,
        author="R1",
        co_authors={"R2"},
        co_sponsors={"R1", "R2", "R3"},
        source_type="conflict_fragment_ballot_unit",
    )
    result = calculate_drafting_alignment([item], ["R1", "R2", "R3"])
    assert result.scores == {"R1": 1.0, "R2": 1.0, "R3": 0.5}
