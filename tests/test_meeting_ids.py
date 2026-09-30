import json

import pytest

from project_ensemble.domain import DeliverableType, MeetingType
from project_ensemble.ids import meeting_id, meeting_id_prefix
from project_ensemble.storage.meeting_index import discover_local_meetings


@pytest.mark.parametrize(
    ("meeting_type", "deliverable_type", "prefix"),
    [
        (MeetingType.AUDIT, DeliverableType.NORMATIVE_INSTRUMENT, "AU"),
        (MeetingType.RESEARCH, DeliverableType.NORMATIVE_INSTRUMENT, "LR"),
        (MeetingType.DELIBERATION, DeliverableType.LITERATURE_REVIEW, "LR"),
        (MeetingType.DELIBERATION, DeliverableType.NORMATIVE_INSTRUMENT, "DL"),
        (MeetingType.SCHOLARLY_RENDERING, DeliverableType.SCHOLARLY_RENDERING, "SR"),
    ],
)
def test_new_meeting_id_prefixes(meeting_type, deliverable_type, prefix):
    assert meeting_id(prefix=meeting_id_prefix(meeting_type, deliverable_type)).startswith(
        f"{prefix}-"
    )


def test_verification_prefix_is_reserved_without_inventing_a_meeting_type():
    assert meeting_id(prefix="VR").startswith("VR-")


def test_local_discovery_finds_legacy_and_new_prefixes(tmp_path):
    for mid in ("M-OLD", "AU-NEW", "LR-NEW", "VR-NEW", "DL-NEW", "SR-NEW"):
        public = tmp_path / mid / "public"
        public.mkdir(parents=True)
        (public / "meeting_manifest.json").write_text(
            json.dumps({"meeting_id": mid}), encoding="utf-8"
        )
    (tmp_path / "unrelated" / "public").mkdir(parents=True)
    assert {path.name for path in discover_local_meetings(tmp_path)} == {
        "M-OLD", "AU-NEW", "LR-NEW", "VR-NEW", "DL-NEW", "SR-NEW"
    }
