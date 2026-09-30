from project_ensemble.audit.discussion import AuditDiscussion


def test_rotating_order_and_pass_counts():
    d = AuditDiscussion(["A", "B", "C"], max_turns_per_member=5, seed=7)
    r0 = d.order_for_round(0)
    r1 = d.order_for_round(1)
    assert r1 == r0[1:] + r0[:1]
    for m in r0:
        d.record(m, 0, "PASS")
    assert d.round_all_pass(0)
    assert all(d.used_turns(m) == 1 for m in r0)


def test_out_of_order_audit_speaker_rejected():
    import pytest
    d = AuditDiscussion(["A", "B", "C"], seed=3)
    order = d.order_for_round(0)
    with pytest.raises(ValueError):
        d.record(order[1], 0, "content")
