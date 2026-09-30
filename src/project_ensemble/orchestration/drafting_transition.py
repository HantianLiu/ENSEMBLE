from __future__ import annotations

import hashlib
import json
import re
from pathlib import Path

from pydantic import BaseModel

from project_ensemble.domain import MeetingPhase
from project_ensemble.governance_private.drafting import (
    AtomicDraftItem,
    calculate_drafting_alignment,
    choose_consultative,
    primary_drafter_candidates,
)
from project_ensemble.orchestration.engine import MeetingEngine
from project_ensemble.orchestration.primary_drafter_selection import (
    PrimaryDrafterSelectionRunner,
)
from project_ensemble.storage.meeting import MeetingRepository


TRIAL_POLICY_ID = "ATOMIC_DRAFTING_ITEM_TRIAL_2026-09-18"
TRIAL_REVIEW_STATUS = "PENDING_THINK_TANK_REVIEW"

_ARTICLE_HEADING = re.compile(
    r"^\s*(?:#{1,6}\s*)?(?:\*\*)?"
    r"(?P<label>第[一二三四五六七八九十百零〇两0-9]+条"
    r"(?:之[一二三四五六七八九十百零〇两0-9]+)?)"
)


class DraftingTransitionResult(BaseModel):
    atomic_item_count: int
    retained_atomic_item_count: int
    consultative_count: int
    primary_drafter_id: str | None
    primary_drafter_candidate_count: int
    next_phase: MeetingPhase
    paused_reason: str | None = None
    trial_policy_id: str = TRIAL_POLICY_ID
    trial_review_status: str = TRIAL_REVIEW_STATUS


class DraftingTransitionRunner:
    """Apply the frozen trial atomic-item policy without exposing private scores."""

    def __init__(self, *, repo: MeetingRepository, engine: MeetingEngine):
        self.repo = repo
        self.engine = engine

    def run(self, *, final_draft: Path) -> DraftingTransitionResult:
        self._ensure_trial_activation()
        registry = json.loads(
            self.repo.docs.read_text("identity_private/representative_registry.json")
        )
        representative_ids = [item["representative_id"] for item in registry]
        items = self._ensure_atomic_items(final_draft=final_draft)
        alignment = calculate_drafting_alignment(items, representative_ids)
        scores_relative = Path("governance_private/drafting_alignment/scores.json")
        scores_path = self.repo.root / scores_relative
        if scores_path.exists():
            frozen_alignment = json.loads(scores_path.read_text(encoding="utf-8"))
            if frozen_alignment["result"] != alignment.model_dump(mode="json"):
                raise ValueError("frozen Drafting Alignment does not match the trial-policy item table")
        else:
            self.repo.docs.write_once(
                scores_relative,
                json.dumps(
                    {
                        "meeting_id": self.repo.meeting_id,
                        "policy_id": TRIAL_POLICY_ID,
                        "review_status": TRIAL_REVIEW_STATUS,
                        "formula": "direct_adopted_items + 0.5 * cosponsored_adopted_items",
                        "result": alignment.model_dump(mode="json"),
                    },
                    indent=2,
                    ensure_ascii=False,
                ),
            )
            self.repo.events.append(
                "DRAFTING_ALIGNMENT_FROZEN",
                {
                    "meeting_id": self.repo.meeting_id,
                    "policy_id": TRIAL_POLICY_ID,
                    "atomic_item_count": len(items),
                    "retained_atomic_item_count": sum(item.adopted for item in items),
                    "record_path": str(scores_relative),
                },
                actor="orchestrator",
            )

        candidates = primary_drafter_candidates(alignment.scores)
        consultative = choose_consultative(alignment.scores)
        primary_drafter_id = (
            candidates[0]
            if len(candidates) == 1
            else PrimaryDrafterSelectionRunner(repo=self.repo, engine=self.engine).run(
                candidates=candidates,
                representative_records=registry,
                final_draft=final_draft,
            )
        )
        self._freeze_status_transition(
            representative_ids=representative_ids,
            consultative=consultative,
            primary_drafter_id=primary_drafter_id,
        )
        self.engine.status.phase = MeetingPhase.DETAILED_DRAFTING
        self.engine.status.paused_reason = None
        self.engine.progress.status(
            MeetingPhase.DETAILED_DRAFTING,
            f"试行 atomic-item 计分已冻结；Primary Drafter={primary_drafter_id}；"
            f"CONSULTATIVE={len(consultative)}/{len(representative_ids)}",
        )
        return DraftingTransitionResult(
            atomic_item_count=len(items),
            retained_atomic_item_count=sum(item.adopted for item in items),
            consultative_count=len(consultative),
            primary_drafter_id=primary_drafter_id,
            primary_drafter_candidate_count=len(candidates),
            next_phase=MeetingPhase.DETAILED_DRAFTING,
        )

    def _ensure_trial_activation(self) -> None:
        relative = Path("governance_private/drafting_alignment/trial_policy_activation.json")
        if (self.repo.root / relative).exists():
            return
        record = {
            "meeting_id": self.repo.meeting_id,
            "policy_id": TRIAL_POLICY_ID,
            "status": "TRIAL",
            "review_status": TRIAL_REVIEW_STATUS,
            "authorized_by": "Human",
            "authorized_on": "2026-09-18",
            "supersedes_pause_reason": "ATOMIC_DRAFTING_ITEM_POLICY_NOT_CONFIGURED",
            "retroactive_rewrite_by_review": False,
        }
        self.repo.docs.write_once(relative, json.dumps(record, indent=2, ensure_ascii=False))
        self.repo.events.append(
            "TRIAL_ATOMIC_DRAFTING_ITEM_POLICY_ACTIVATED",
            {**record, "record_path": str(relative)},
            actor="Human",
        )
        self.repo.events.append(
            "MEETING_RESUMED",
            {
                "meeting_id": self.repo.meeting_id,
                "prior_reason_code": "ATOMIC_DRAFTING_ITEM_POLICY_NOT_CONFIGURED",
                "resume_stage": MeetingPhase.STATUS_TRANSITION.value,
                "policy_id": TRIAL_POLICY_ID,
            },
            actor="orchestrator",
        )

    def _ensure_atomic_items(self, *, final_draft: Path) -> list[AtomicDraftItem]:
        relative = Path("governance_private/drafting_alignment/atomic_items.json")
        absolute = self.repo.root / relative
        if absolute.exists():
            frozen = json.loads(absolute.read_text(encoding="utf-8"))
            expected_path = str(final_draft.relative_to(self.repo.root))
            expected_sha = self._sha256(final_draft.read_text(encoding="utf-8"))
            if (
                frozen.get("policy_id") != TRIAL_POLICY_ID
                or frozen.get("final_draft_path") != expected_path
                or frozen.get("final_draft_sha256") != expected_sha
            ):
                raise ValueError("frozen atomic-item table does not match the active trial policy/final draft")
            return [AtomicDraftItem.model_validate(item) for item in frozen["items"]]

        initial_drafter = json.loads(
            self.repo.docs.read_text(
                "governance_private/general_principle/initial_drafter_selection.json"
            )
        )["representative_id"]
        d0_path = self.repo.root / "public/general_principle/D0.md"
        d0_text = d0_path.read_text(encoding="utf-8")
        final_text = final_draft.read_text(encoding="utf-8")
        ledgers = {window: self._load_ledger(window) for window in range(1, 4)}
        dockets = {window: self._load_docket(window) for window in range(1, 4)}
        amendment_index = {
            amendment["amendment_id"]: (window, amendment)
            for window, docket in dockets.items()
            for amendment in docket["amendments"]
        }

        items = self._initial_draft_items(
            initial_drafter=initial_drafter,
            d0_text=d0_text,
            final_text=final_text,
            ledgers=ledgers,
        )
        items.extend(
            self._amendment_items(
                amendment_index=amendment_index,
                ledgers=ledgers,
            )
        )
        record = {
            "meeting_id": self.repo.meeting_id,
            "policy_id": TRIAL_POLICY_ID,
            "policy_status": "TRIAL",
            "review_status": TRIAL_REVIEW_STATUS,
            "final_draft_path": str(final_draft.relative_to(self.repo.root)),
            "final_draft_sha256": self._sha256(final_text),
            "retention_caveat": (
                "Adopted amendment applications in the immutable D3 ancestry are presumed retained "
                "during the trial; Think Tank must review possible later semantic deletion."
            ),
            "items": [item.model_dump(mode="json") for item in items],
        }
        self.repo.docs.write_once(relative, json.dumps(record, indent=2, ensure_ascii=False))
        self.repo.events.append(
            "ATOMIC_DRAFTING_ITEM_TABLE_FROZEN",
            {
                "meeting_id": self.repo.meeting_id,
                "policy_id": TRIAL_POLICY_ID,
                "item_count": len(items),
                "retained_item_count": sum(item.adopted for item in items),
                "record_path": str(relative),
            },
            actor="orchestrator",
        )
        return items

    def _initial_draft_items(
        self,
        *,
        initial_drafter: str,
        d0_text: str,
        final_text: str,
        ledgers: dict[int, dict],
    ) -> list[AtomicDraftItem]:
        labels = self._article_labels(d0_text)
        final_labels = set(self._article_labels(final_text))
        base_cosponsors = set().union(
            *(set(ledgers[window]["support"].get(f"D{window - 1}", [])) for window in range(1, 4))
        )
        if not labels:
            return [
                AtomicDraftItem(
                    item_id="D0:FALLBACK_DOCUMENT",
                    adopted=True,
                    author=initial_drafter,
                    co_sponsors=base_cosponsors,
                    source_type="initial_draft_fallback",
                    source_references=["public/general_principle/D0.md", "public/general_principle/D3.md"],
                    retention_basis="D0 had no numbered articles; ratified D3 activates the one-document fallback.",
                )
            ]
        return [
            AtomicDraftItem(
                item_id=f"D0:{label}",
                adopted=label in final_labels,
                author=initial_drafter,
                co_sponsors=base_cosponsors,
                source_type="initial_draft_numbered_article",
                source_references=["public/general_principle/D0.md", "public/general_principle/D3.md"],
                retention_basis=(
                    f"Numbered article {label} remains present in D3."
                    if label in final_labels
                    else f"Numbered article {label} is absent from D3."
                ),
            )
            for label in labels
        ]

    def _amendment_items(
        self,
        *,
        amendment_index: dict[str, tuple[int, dict]],
        ledgers: dict[int, dict],
    ) -> list[AtomicDraftItem]:
        decisions: dict[str, tuple[int, dict, Path]] = {}
        for window in range(1, 4):
            root = self.repo.root / "public/general_principle/decisions" / f"window_{window:03d}"
            for path in sorted(root.glob("*.json")):
                decision = json.loads(path.read_text(encoding="utf-8"))
                decisions[decision["amendment_id"]] = (window, decision, path)
        missing = set(amendment_index) - set(decisions)
        if missing:
            raise ValueError(f"atomic-item provenance is missing amendment decisions: {sorted(missing)}")

        fragment_index = self._fragment_index()
        fragment_states: dict[str, dict] = {}
        items: list[AtomicDraftItem] = []
        for amendment_id, (window, amendment) in amendment_index.items():
            decision_window, decision, decision_path = decisions[amendment_id]
            if decision_window != window:
                raise ValueError(f"amendment {amendment_id} decision is stored under the wrong window")
            components = decision.get("component_outcomes")
            if components:
                for component in components:
                    state = fragment_states.setdefault(
                        component["fragment_id"],
                        {"adopted": False, "decision_paths": set()},
                    )
                    state["adopted"] = state["adopted"] or bool(component["adopted"])
                    state["decision_paths"].add(str(decision_path.relative_to(self.repo.root)))
                continue
            if decision["outcome"] not in {"ADOPTED", "REJECTED"}:
                continue
            items.append(
                AtomicDraftItem(
                    item_id=f"W{window:03d}:{amendment_id}",
                    adopted=decision["outcome"] == "ADOPTED",
                    author=amendment["proposer_id"],
                    co_sponsors=self._amendment_cosponsors(
                        source_ids={amendment_id},
                        amendment_index=amendment_index,
                        ledgers=ledgers,
                    ),
                    source_type="amendment_ballot_unit",
                    source_references=[
                        f"public/general_principle/amendment_docket_window_{window:03d}.json",
                        str(decision_path.relative_to(self.repo.root)),
                    ],
                    retention_basis=(
                        "Adopted application is in the immutable draft ancestry leading to D3 (trial presumption)."
                        if decision["outcome"] == "ADOPTED"
                        else "The independently balloted amendment was rejected."
                    ),
                )
            )

        for fragment_id, state in sorted(fragment_states.items()):
            if fragment_id not in fragment_index:
                raise ValueError(f"atomic-item provenance is missing conflict fragment {fragment_id}")
            fragment, decomposition_path = fragment_index[fragment_id]
            source_ids = set(fragment["source_amendment_ids"])
            if not source_ids <= set(amendment_index):
                raise ValueError(f"conflict fragment {fragment_id} references an unknown amendment")
            authors = sorted({amendment_index[source_id][1]["proposer_id"] for source_id in source_ids})
            items.append(
                AtomicDraftItem(
                    item_id=f"FRAGMENT:{fragment_id}",
                    adopted=bool(state["adopted"]),
                    author=authors[0],
                    co_authors=set(authors[1:]),
                    co_sponsors=self._amendment_cosponsors(
                        source_ids=source_ids,
                        amendment_index=amendment_index,
                        ledgers=ledgers,
                    ),
                    source_type="conflict_fragment_ballot_unit",
                    source_references=[
                        str(decomposition_path.relative_to(self.repo.root)),
                        *sorted(state["decision_paths"]),
                    ],
                    retention_basis=(
                        "Fragment was independently adopted and applied in the immutable D3 ancestry "
                        "(trial presumption)."
                        if state["adopted"]
                        else "Fragment was independently rejected or lost its exclusive choice set."
                    ),
                )
            )
        return items

    def _fragment_index(self) -> dict[str, tuple[dict, Path]]:
        result: dict[str, tuple[dict, Path]] = {}
        root = self.repo.root / "public/general_principle/conflict_decompositions"
        for path in sorted(root.rglob("*.json")) if root.exists() else []:
            record = json.loads(path.read_text(encoding="utf-8"))
            fragments = list(record.get("compatible_fragments", []))
            fragments.extend(
                option
                for choice_set in record.get("choice_sets", [])
                for option in choice_set.get("options", [])
            )
            for fragment in fragments:
                fragment_id = fragment["fragment_id"]
                if fragment_id in result:
                    raise ValueError(f"duplicate conflict fragment provenance: {fragment_id}")
                result[fragment_id] = (fragment, path)
        return result

    def _amendment_cosponsors(
        self,
        *,
        source_ids: set[str],
        amendment_index: dict[str, tuple[int, dict]],
        ledgers: dict[int, dict],
    ) -> set[str]:
        windows = {amendment_index[source_id][0] for source_id in source_ids}
        if len(windows) != 1:
            raise ValueError("one independently balloted fragment cannot span submission windows")
        window = next(iter(windows))
        support = set().union(
            *(set(ledgers[window]["support"].get(source_id, [])) for source_id in source_ids)
        )
        for later_window in range(window + 1, 4):
            support.update(ledgers[later_window]["support"].get(f"D{later_window - 1}", []))
        return support

    def _freeze_status_transition(
        self,
        *,
        representative_ids: list[str],
        consultative: set[str],
        primary_drafter_id: str,
    ) -> None:
        private_relative = Path("identity_private/general_principle/status_transition.json")
        private_record = {
            "meeting_id": self.repo.meeting_id,
            "policy_id": TRIAL_POLICY_ID,
            "review_status": TRIAL_REVIEW_STATUS,
            "primary_drafter_id": primary_drafter_id,
            "representative_statuses": {
                representative_id: (
                    "CONSULTATIVE" if representative_id in consultative else "ACTIVE"
                )
                for representative_id in representative_ids
            },
        }
        if not (self.repo.root / private_relative).exists():
            self.repo.docs.write_once(
                private_relative,
                json.dumps(private_record, indent=2, ensure_ascii=False),
            )
            self.repo.events.append(
                "REPRESENTATIVE_STATUS_TRANSITION_FROZEN",
                {
                    "meeting_id": self.repo.meeting_id,
                    "active_count": len(representative_ids) - len(consultative),
                    "consultative_count": len(consultative),
                    "record_path": str(private_relative),
                },
                actor="orchestrator",
            )
            for representative_id in representative_ids:
                self.repo.docs.write_once(
                    Path("representatives")
                    / representative_id
                    / "status_transition_001.json",
                    json.dumps(
                        {
                            "representative_id": representative_id,
                            "status": private_record["representative_statuses"][representative_id],
                        },
                        indent=2,
                        ensure_ascii=False,
                    ),
                )
        else:
            existing = json.loads((self.repo.root / private_relative).read_text(encoding="utf-8"))
            if existing != private_record:
                raise ValueError("frozen representative status transition does not match Drafting Alignment")

        public_relative = Path("public/general_principle/status_transition.json")
        if not (self.repo.root / public_relative).exists():
            self.repo.docs.write_once(
                public_relative,
                json.dumps(
                    {
                        "meeting_id": self.repo.meeting_id,
                        "status": "FROZEN",
                        "active_count": len(representative_ids) - len(consultative),
                        "consultative_count": len(consultative),
                        "primary_drafter_id": primary_drafter_id,
                        "atomic_item_policy_status": "TRIAL",
                        "review_status": TRIAL_REVIEW_STATUS,
                    },
                    indent=2,
                    ensure_ascii=False,
                ),
            )
            self.repo.events.append(
                "PRIMARY_DRAFTER_SELECTED",
                {
                    "meeting_id": self.repo.meeting_id,
                    "representative_id": primary_drafter_id,
                    "record_path": str(public_relative),
                },
                actor="orchestrator",
            )

    def _pause_for_primary_tie(self, reason: str, candidates: list[str]) -> None:
        relative = Path("governance_private/drafting_alignment/primary_drafter_tie.json")
        if not (self.repo.root / relative).exists():
            self.repo.docs.write_once(
                relative,
                json.dumps(
                    {
                        "meeting_id": self.repo.meeting_id,
                        "reason_code": reason,
                        "candidate_ids": candidates,
                        "candidate_count": len(candidates),
                    },
                    indent=2,
                ),
            )
            event = self.repo.events.append(
                "MEETING_PAUSED",
                {
                    "meeting_id": self.repo.meeting_id,
                    "reason_code": reason,
                    "candidate_count": len(candidates),
                    "record_path": str(relative),
                },
                actor="orchestrator",
            )
            self.engine.escalation.request(
                reason_code=reason,
                summary=(
                    "Two Primary Drafter candidates require the confirmed all-Representative runoff."
                    if len(candidates) == 2
                    else "Three or more Primary Drafter candidates are tied; the reduction policy remains open."
                ),
                related_event_hash=event["event_hash"],
            )
        self.engine.status.phase = MeetingPhase.PAUSED
        self.engine.status.paused_reason = reason
        self.engine.progress.status(
            MeetingPhase.PAUSED,
            f"{reason} · 并列候选人数={len(candidates)}",
        )

    def _load_ledger(self, window: int) -> dict:
        relative = (
            Path("governance_private/general_principle/cosponsorship/frozen_ledger.json")
            if window == 1
            else Path("governance_private/general_principle/cosponsorship")
            / f"window_{window:03d}"
            / "frozen_ledger.json"
        )
        return json.loads(self.repo.docs.read_text(relative))

    def _load_docket(self, window: int) -> dict:
        return json.loads(
            self.repo.docs.read_text(
                Path("public/general_principle")
                / f"amendment_docket_window_{window:03d}.json"
            )
        )

    @staticmethod
    def _article_labels(text: str) -> list[str]:
        labels: list[str] = []
        for line in text.splitlines():
            match = _ARTICLE_HEADING.match(line)
            if match and match.group("label") not in labels:
                labels.append(match.group("label"))
        return labels

    @staticmethod
    def _sha256(text: str) -> str:
        return hashlib.sha256(text.encode("utf-8")).hexdigest()
