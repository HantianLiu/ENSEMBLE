import pytest
from project_ensemble.orchestration.ballot import SealedBallot, BallotStatus
from project_ensemble.errors import BallotNotClosedError, InvalidBallotError, PolicyNotConfiguredError


def test_no_partial_tally_and_no_abstention():
    with pytest.raises(ValueError):
        SealedBallot(ballot_id="B", eligible_representatives={"R1"}, options=("YES", "ABSTAIN"))
    b = SealedBallot(ballot_id="B", eligible_representatives={"R1", "R2"}, options=("YES", "NO"))
    b.submit("R1", "YES")
    with pytest.raises(BallotNotClosedError):
        b.tally()
    with pytest.raises(InvalidBallotError):
        b.close()
    b.submit("R2", "NO")
    b.close()
    assert b.tally() == {"YES": 1, "NO": 1}


def test_partial_ballot_pause_fails_closed_without_policy():
    b = SealedBallot(ballot_id="B", eligible_representatives={"R1", "R2"}, options=("A", "B"))
    b.submit("R1", "A")
    b.pause()
    assert b.status == BallotStatus.PAUSED
    with pytest.raises(PolicyNotConfiguredError):
        b.resume()
