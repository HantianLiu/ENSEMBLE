"""Human-authorized AI rulings for fast scientific revisions."""

from __future__ import annotations

import json
import re
from datetime import datetime, timezone
from pathlib import Path
from typing import Literal

from pydantic import BaseModel, ConfigDict, Field

from project_ensemble.orchestration.consultations import HumanConsultationResolution, HumanConsultationService
from project_ensemble.orchestration.literature_report_execution import (
    _effective_chapter_citation_catalog_path,
)
from project_ensemble.orchestration.literature_writing_v071 import (
    WriterChapter, _science_review_evidence,
)
from project_ensemble.runtime.fast_science_consultation import _safe_json, _review_objections


AI_SCIENCE_MENU_ACTION = "DELEGATE_THIS_SCIENCE_CONSULTATION_TO_AI"
AI_SCIENCE_DECISIONS = frozenset({
    "RETRY_WRITER_LOCAL_REPAIR", "REWRITE_WHOLE_MODULE", "RETRY_WRITER_REVISION", "KEEP_PAUSED",
})
MAX_STANDING_AI_RETRIES_PER_MODULE = 2
AUTOMATIC_SCIENCE_RETRY_LIMIT_EFFECT = "HUMAN_RULING_REQUIRED_AFTER_AUTOMATIC_RETRY_LIMIT"
_WRITER_RETRY_DECISIONS = frozenset({
    "RETRY_WRITER_LOCAL_REPAIR", "REWRITE_WHOLE_MODULE", "RETRY_WRITER_REVISION",
})


class AIScienceObjectionDisposition(BaseModel):
    model_config = ConfigDict(extra="forbid", str_strip_whitespace=True)
    objection_number: int = Field(ge=1)
    treatment: Literal["REVISE", "NOT_ADOPT", "NEEDS_EVIDENCE"]
    rationale: str = Field(min_length=1)


class AIScienceDecision(BaseModel):
    model_config = ConfigDict(extra="forbid", str_strip_whitespace=True)
    decision: Literal[
        "RETRY_WRITER_LOCAL_REPAIR", "REWRITE_WHOLE_MODULE", "RETRY_WRITER_REVISION", "KEEP_PAUSED",
    ]
    rationale: str = Field(min_length=1)
    objections: list[AIScienceObjectionDisposition] = Field(min_length=1)


def can_delegate_fast_science(issue) -> bool:
    return (
        issue.stage == "FAST_SCIENCE_REVIEW"
        and issue.reason_code == "FAST_SCIENCE_REVIEW_HUMAN_REQUIRED"
        and not issue.context.get("last_problem")
        and bool((AI_SCIENCE_DECISIONS - {"KEEP_PAUSED"}).intersection(issue.options))
        and bool(re.fullmatch(r"RM-\d+", str(issue.context.get("module_id", ""))))
        and isinstance(issue.context.get("recheck_path"), str)
    )


def science_rulings_for_review(repo, module_id: str, review_path: Path) -> list[dict]:
    """Read only completed AI rulings tied to this exact frozen review."""
    review_relative = str(review_path.relative_to(repo.root))
    service = HumanConsultationService(repo)
    rulings = []
    for path in sorted((repo.root / "public/procedural_consultations").glob("*.ai_science_decision.json")):
        issue_id = path.name.removesuffix(".ai_science_decision.json")
        issue = _safe_json(repo.root, f"human_private/consultations/{issue_id}.issue.json")
        resolution = service.resolution(issue_id)
        if issue is None or resolution is None or resolution.authority != "DELEGATED_AI":
            continue
        context = issue.get("context", {})
        if context.get("module_id") == module_id and context.get("recheck_path") == review_relative:
            rulings.append(json.loads(path.read_text(encoding="utf-8")))
    return rulings


def _science_reviewer(repo) -> str:
    registry = _safe_json(repo.root, "identity_private/meeting_manifest.json") or {}
    path = repo.root / "identity_private/representative_registry.json"
    if not path.is_file():
        raise ValueError("本会议没有可用于 AI 代裁的科学审阅员")
    records = json.loads(path.read_text(encoding="utf-8"))
    from project_ensemble.runtime.model_replacements import current_runtime_for

    writer = current_runtime_for(repo, "WRITER") if registry.get("writer_model") else None
    candidates = [record for record in records
                  if record.get("status", "ACTIVE") == "ACTIVE"
                  and record.get("runtime", {}).get("persona") == "librarian"]
    if not candidates:
        raise ValueError("本会议没有可用于 AI 代裁的在岗科学审阅员")
    # Prefer a different base model from the Writer. Registry metadata never
    # enters the adjudicator's prompt; the selected call starts a fresh context.
    return next((record["representative_id"] for record in candidates
                 if current_runtime_for(repo, record["representative_id"]) != writer),
                candidates[0]["representative_id"])


def _decision_context(repo, issue) -> dict:
    module_id = str(issue.context.get("module_id", ""))
    if not re.fullmatch(r"RM-\d+", module_id):
        raise ValueError("科学咨询缺少有效的研究模块编号")
    recheck_relative = issue.context.get("recheck_path")
    review = _safe_json(repo.root, recheck_relative)
    if review is None:
        raise ValueError("当前科学异议记录无法读取")
    problems = _review_objections(review)
    if not problems:
        raise ValueError("当前记录没有可供代裁的具体科学异议")
    match = re.search(r"_v(\d+)\.json$", str(recheck_relative))
    if match is None:
        raise ValueError("无法确定本次科学异议对应的稿件版本")
    version = int(match.group(1))
    base = f"public/literature_report/modules/{module_id}"
    chapter_record = _safe_json(repo.root, f"{base}/writing_v071/writer_v{version}_validated.json")
    if chapter_record is None:
        raise ValueError("科学异议对应的已校验稿件无法读取")
    chapter = WriterChapter.model_validate(chapter_record)
    catalog_path = _effective_chapter_citation_catalog_path(repo.root, module_id)
    catalog = _safe_json(repo.root, str(catalog_path.relative_to(repo.root))) or {"sources": []}
    prose = chapter.draft.body_markdown + "\n" + chapter.draft.short_summary
    cited = set(re.findall(r"C\d+-\d+", prose))
    for term in chapter.glossary_additions:
        cited.update(term.source_citation_ids)
    legacy = set(re.findall(r"RP-[A-Z0-9]+", prose))
    relevant = [source for source in catalog.get("sources", [])
                if source.get("citation_id") in cited
                or legacy.intersection(source.get("packet_ids", []))]
    dossier = _safe_json(repo.root, f"{base}/research/evidence_dossier.json") or {}
    source_ids = {str(source["source_id"]) for source in relevant if source.get("source_id")}
    return {
        "module_id": module_id,
        "current_chapter": chapter.model_dump(mode="json"),
        "numbered_objections": [{"number": index, "objection": problem}
                                for index, problem in enumerate(problems, 1)],
        "cited_source_catalog": relevant,
        "bounded_evidence": _science_review_evidence(dossier, chapter, cited_source_ids=source_ids),
        "local_revision_application": _safe_json(
            repo.root, f"{base}/writing_v071/writer_v{version}_local_patch_applied.json",
        ) or {},
        "review_date_utc": datetime.now(timezone.utc).date().isoformat(),
        "allowed_decisions": [option for option in issue.options if option in AI_SCIENCE_DECISIONS],
    }


def delegate_fast_science(repo, issue, *, engine, max_output_tokens=None,
                          standing_authorization_path=None):
    """A single explicit choice or a still-active meeting authorization is required."""
    if not can_delegate_fast_science(issue):
        raise ValueError("此咨询不支持科学异议 AI 代裁")
    service = HumanConsultationService(repo)
    existing = service.resolution(issue.issue_id)
    if existing is not None:
        return existing
    if standing_authorization_path is not None:
        from project_ensemble.runtime.ai_delegation_settings import delegation_setting
        enabled, source = delegation_setting(repo, "fast_science")
        if not enabled or source != standing_authorization_path:
            raise ValueError("本会议的科学异议 AI 代裁授权已关闭或变更")
    context = _decision_context(repo, issue)
    participant = _science_reviewer(repo)
    authorization_path = Path("human_private/consultations") / f"{issue.issue_id}.ai_authorization.json"
    authorization = {
        "meeting_id": repo.meeting_id, "issue_id": issue.issue_id,
        "authorized_by": "HUMAN", "scope": "THIS_CONSULTATION_ONLY",
        "allowed_decisions": context["allowed_decisions"],
        "science_recheck_required": True,
        **({"standing_authorization_record_path": standing_authorization_path}
           if standing_authorization_path is not None else {}),
    }
    if (repo.root / authorization_path).is_file():
        previous = json.loads((repo.root / authorization_path).read_text(encoding="utf-8"))
        if previous.get("standing_authorization_record_path") != standing_authorization_path:
            # A revoked standing grant is not silently reused as a one-time grant.
            # Record the Human's new choice without overwriting the original.
            folder = repo.root / "human_private/consultations"
            changes = sorted(folder.glob(f"{issue.issue_id}.ai_authorization-*.json"))
            sequence = int(changes[-1].stem.rsplit("-", 1)[1]) + 1 if changes else 1
            authorization_path = Path("human_private/consultations") / (
                f"{issue.issue_id}.ai_authorization-{sequence:06d}.json"
            )
    if not (repo.root / authorization_path).is_file():
        repo.docs.write_once(authorization_path, json.dumps(authorization, ensure_ascii=False, indent=2))
        repo.events.append("FAST_SCIENCE_AI_DELEGATION_AUTHORIZED", authorization,
                           actor="orchestrator" if standing_authorization_path else "HUMAN")
    public_authorization_path = Path("public/procedural_consultations") / authorization_path.name
    if not (repo.root / public_authorization_path).is_file():
        repo.docs.write_once(public_authorization_path, (repo.root / authorization_path).read_text(encoding="utf-8"))
    decision_path = Path("public/procedural_consultations") / f"{issue.issue_id}.ai_science_decision.json"
    system = (
        "人类已选择由 AI 代裁当前这一次科学异议咨询。你在独立上下文中审查当前稿、"
        "每条原异议和已核查证据，逐条说明采纳修订、不采纳的证据理由，或仍需补证。"
        "优先选择只修改相关段落和术语；仅在确有全章结构问题时选择整章重写。"
        "没有证据时不得凭模型记忆把异议判为不存在；有界证据遗漏也不等于事实不存在。"
        "只选择 allowed_decisions 中的下一步处理方式，不能宣布科学复核通过或替人类附限制放行。"
        "所有原异议仍交独立科学复核，主笔可据你的理由修订或作有依据的回应。"
        "逐条覆盖全部 numbered_objections，每条恰好一次。理由用报告的语言，清楚指出证据与判断。"
        "只返回符合所给 JSON schema 的对象。"
    )
    user = json.dumps(context, ensure_ascii=False) + "\nTARGET JSON SCHEMA:\n" + json.dumps(
        AIScienceDecision.model_json_schema(), ensure_ascii=False,
    )
    stage = f"fast_science_ai_decision_{issue.issue_id}"
    if (repo.root / decision_path).is_file():
        decision = AIScienceDecision.model_validate_json((repo.root / decision_path).read_text(encoding="utf-8"))
    else:
        response = engine.find_recorded_response(
            participant, stage=stage, system_text=system, user_text=user,
        ) or engine.invoke_participant(
            participant, stage=stage, system_text=system, user_text=user,
            max_output_tokens=max_output_tokens,
        )
        decision = engine.validate_structured_response(
            participant, response=response, schema_model=AIScienceDecision, stage=stage,
            max_output_tokens=max_output_tokens, fresh_attempts_remaining=0,
            nonblocking_quality_failure_code="FAST_SCIENCE_AI_DECISION_UNUSABLE",
        )
    if decision.decision not in context["allowed_decisions"]:
        raise ValueError("AI 返回了未获授权的处理决定")
    numbers = [item.objection_number for item in decision.objections]
    if sorted(numbers) != list(range(1, len(context["numbered_objections"]) + 1)):
        raise ValueError("AI 没有逐条完整处理本次科学异议；原咨询仍未解决")
    if not (repo.root / decision_path).is_file():
        repo.docs.write_once(decision_path, decision.model_dump_json(indent=2))
    treatment_labels = {"REVISE": "建议修订", "NOT_ADOPT": "有据不采纳", "NEEDS_EVIDENCE": "仍需补证"}
    rationale = decision.rationale + "\n" + "\n".join(
        f"异议 {item.objection_number}（{treatment_labels[item.treatment]}）：{item.rationale}"
        for item in decision.objections
    )
    if decision.decision == "KEEP_PAUSED":
        # A pause is not a final disposition: keep the menu available on resume.
        return HumanConsultationResolution(
            issue_id=issue.issue_id, meeting_id=repo.meeting_id, decision=decision.decision,
            rationale=rationale, scope="THIS_CONSULTATION_ONLY", thresholds=issue.thresholds,
            authority="DELEGATED_AI", authorization_record_path=str(authorization_path),
        )
    # An in-flight model cannot commit under a standing grant revoked from settings.
    if standing_authorization_path is not None:
        enabled, source = delegation_setting(repo, "fast_science")
        if not enabled or source != standing_authorization_path:
            raise ValueError("代裁过程中会议授权已变更；原咨询保留供人工处理")
    return service._resolve(
        issue_id=issue.issue_id, decision=decision.decision, rationale=rationale,
        scope="THIS_CONSULTATION_ONLY", authority="DELEGATED_AI",
        authorization_record_path=str(authorization_path),
    )


def _standing_ai_writer_retries_for_module(repo, module_id: str) -> int:
    """Count durable meeting-wide AI rulings that sent this module back to its Writer."""
    root = repo.root / "human_private/consultations"
    if not root.is_dir():
        return 0
    service = HumanConsultationService(repo)
    count = 0
    pattern = f"HC-FAST-SCIENCE-{module_id}-LOCAL-V*.issue.json"
    for issue_path in root.glob(pattern):
        issue_id = issue_path.name.removesuffix(".issue.json")
        try:
            previous_issue = json.loads(issue_path.read_text(encoding="utf-8"))
            resolution = service.resolution(issue_id)
        except (OSError, json.JSONDecodeError, ValueError):
            continue
        if (previous_issue.get("context", {}).get("module_id") != module_id
                or resolution is None
                or resolution.authority != "DELEGATED_AI"
                or resolution.decision not in _WRITER_RETRY_DECISIONS
                or not resolution.authorization_record_path):
            continue
        authorization = _safe_json(repo.root, resolution.authorization_record_path)
        # This guard limits the meeting-wide standing grant. A Human may still
        # explicitly delegate a single consultation after the guard trips.
        if authorization and authorization.get("standing_authorization_record_path"):
            count += 1
    return count


def record_automatic_science_fallback(repo, issue, source, error, *, effect=None):
    fallback = Path("public/procedural_consultations") / f"{issue.issue_id}.ai_automatic_fallback.json"
    if (repo.root / fallback).is_file():
        return
    payload = {"meeting_id": repo.meeting_id, "issue_id": issue.issue_id,
               "standing_authorization_record_path": source, "reason": str(error),
               "effect": effect or "MANUAL_RECOVERY;NO_AUTOMATIC_RETRY_OF_THIS_ISSUE"}
    repo.docs.write_once(fallback, json.dumps(payload, ensure_ascii=False, indent=2))
    repo.events.append("FAST_SCIENCE_AI_AUTOMATIC_FALLBACK", payload, actor="orchestrator")


def try_automatic_fast_science(repo, issue, *, engine, max_output_tokens=None):
    """At most one automatic attempt per issue; failure/pause leaves manual recovery."""
    from project_ensemble.runtime.ai_delegation_settings import delegation_setting, delegation_model
    from project_ensemble.errors import (
        ProviderError, RepresentativeUnavailableError, InputContextLimitError,
        OutputLimitReachedError, EmptyModelOutputError, ResearchQualityControlError,
    )
    if engine is None or not can_delegate_fast_science(issue):
        return None
    enabled, source = delegation_setting(repo, "fast_science")
    fallback = Path("public/procedural_consultations") / f"{issue.issue_id}.ai_automatic_fallback.json"
    if not enabled or (repo.root / fallback).is_file():
        return None
    progress = getattr(engine, "progress", None)
    module_id = str(issue.context.get("module_id", ""))
    automatic_retries = _standing_ai_writer_retries_for_module(repo, module_id)
    if automatic_retries >= MAX_STANDING_AI_RETRIES_PER_MODULE:
        error = (
            f"该模块已达到会议级 AI 自动返修上限（{MAX_STANDING_AI_RETRIES_PER_MODULE} 次退回主笔）；"
            "原科学异议与当前稿均保留，本条改由人工菜单决定下一步"
        )
        record_automatic_science_fallback(
            repo, issue, source, error,
            effect=AUTOMATIC_SCIENCE_RETRY_LIMIT_EFFECT,
        )
        if progress is not None:
            progress.info(error)
        return None
    try:
        if progress is not None:
            progress.info(f"科学异议 AI 代裁 · 当前模型：{delegation_model(repo, 'fast_science')}")
        result = delegate_fast_science(
            repo, issue, engine=engine, max_output_tokens=max_output_tokens,
            standing_authorization_path=source,
        )
        if result.decision != "KEEP_PAUSED":
            return result
        error = "AI 建议保持暂停；原咨询返回人工选项"
    except (ValueError, OSError, ProviderError, RepresentativeUnavailableError,
            InputContextLimitError, OutputLimitReachedError, EmptyModelOutputError,
            ResearchQualityControlError) as exc:
        error = str(exc)
    record_automatic_science_fallback(repo, issue, source, error)
    if progress is not None:
        progress.info(f"AI 代裁未完成：{error}；原异议保留，改由人工选择，不重复自动重试")
    return None
