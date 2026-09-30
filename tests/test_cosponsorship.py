import pytest
from project_ensemble.orchestration.cosponsorship import SealedCosponsorshipLedger
from project_ensemble.errors import BallotNotClosedError


def test_cosponsorship_hidden_until_ballot_close():
    c = SealedCosponsorshipLedger({"R1", "R2"}, {"A1", "A2"})
    c.add("R1", "A1")
    c.add("R1", "A2")  # conflicting support is not blocked at this layer
    c.freeze()
    with pytest.raises(BallotNotClosedError):
        c.reveal(ballot_closed=False)
    assert c.reveal(ballot_closed=True)["A1"] == {"R1"}
