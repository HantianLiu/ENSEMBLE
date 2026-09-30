import pytest
from project_ensemble.runtime.access import AccessPolicy, Principal
from project_ensemble.errors import AccessDeniedError


def test_representative_isolation():
    p = AccessPolicy()
    p.assert_read(Principal.REPRESENTATIVE, "public/docket.md", representative_id="R1")
    p.assert_read(Principal.REPRESENTATIVE, "representatives/R1/state.json", representative_id="R1")
    with pytest.raises(AccessDeniedError):
        p.assert_read(Principal.REPRESENTATIVE, "representatives/R2/state.json", representative_id="R1")
    with pytest.raises(AccessDeniedError):
        p.assert_read(Principal.REPRESENTATIVE, "governance_private/drafting.json", representative_id="R1")
    with pytest.raises(AccessDeniedError):
        p.assert_read(Principal.REPRESENTATIVE, "identity_private/registry.json", representative_id="R1")


def test_chair_cannot_see_identity_private_but_audit_can():
    p = AccessPolicy()
    with pytest.raises(AccessDeniedError):
        p.assert_read(Principal.CHAIR, "identity_private/registry.json")
    p.assert_read(Principal.AUDIT, "identity_private/registry.json")


def test_escalation_contact_is_human_only():
    p = AccessPolicy()
    p.assert_read(Principal.HUMAN, "human_private/escalation_contact.json")
    p.assert_read(Principal.ORCHESTRATOR, "human_private/escalation_contact.json")
    for principal in (Principal.REPRESENTATIVE, Principal.CHAIR, Principal.THINK_TANK, Principal.AUDIT):
        with pytest.raises(AccessDeniedError):
            p.assert_read(principal, "human_private/escalation_contact.json", representative_id="R1")
