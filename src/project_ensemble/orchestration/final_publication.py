from __future__ import annotations

import hashlib
import html
import io
import json
import os
import re
import unicodedata
from pathlib import Path
from typing import Literal

from pydantic import BaseModel, ConfigDict, Field, model_validator
import reportlab
from reportlab.lib import colors
from reportlab.lib.enums import TA_CENTER, TA_LEFT
from reportlab.lib.pagesizes import A4
from reportlab.lib.styles import ParagraphStyle, getSampleStyleSheet
from reportlab.lib.units import mm
from reportlab.pdfbase import pdfmetrics
from reportlab.pdfbase.ttfonts import TTFont
from reportlab.pdfgen import canvas
from reportlab.platypus import (
    PageBreak,
    Paragraph,
    SimpleDocTemplate,
    Spacer,
)

from project_ensemble.domain import MeetingPhase
from project_ensemble.orchestration.engine import MeetingEngine
from project_ensemble.orchestration.math_rendering import (
    INLINE_MATH_TOKEN, MathSafeParagraph, PdfMathRenderer, display_math_continue,
    display_math_start, inline_math_content, normalize_fragmented_inline_math,
    normalize_math_operator_commands, repair_json_decoded_math_commands,
)
from project_ensemble.research.models import EvidencePacket, EvidenceSource, EvidenceUseClass
from project_ensemble.storage.human_outputs import ensure_visible_link
from project_ensemble.storage.meeting import MeetingRepository


PUBLICATION_TEMPLATE_VERSION = "FINAL_PUBLICATION_V3"
ACADEMIC_CITATION_STYLE = "NUMBERED_HANGING_INDENT_V2"


class ReadabilityCheck(BaseModel):
    model_config = ConfigDict(extra="forbid", str_strip_whitespace=True)
    name: Literal[
        "MAIN_RESULT_IDENTIFIABLE",
        "ADVISORY_APPENDICES_SEPARATE",
        "NAVIGATION_AND_STRUCTURE",
        "SOURCE_LABELS_CLEAR",
        "SOURCE_TEXT_LEGIBLE",
    ]
    status: Literal["PASS", "FAIL"]
    explanation: str = Field(min_length=1)


class ChairReadabilityCertification(BaseModel):
    model_config = ConfigDict(extra="forbid", str_strip_whitespace=True)
    status: Literal["READABLE", "REVISION_REQUIRED"]
    publication_source_sha256: str = Field(pattern=r"^[0-9a-f]{64}$")
    checks: list[ReadabilityCheck] = Field(min_length=5, max_length=5)
    required_changes: list[str] = Field(default_factory=list)
    summary: str = Field(min_length=1)

    @model_validator(mode="after")
    def status_matches_checks(self) -> "ChairReadabilityCertification":
        names = [item.name for item in self.checks]
        if len(set(names)) != 5:
            raise ValueError("readability certification requires five distinct checks")
        failures = [item for item in self.checks if item.status == "FAIL"]
        if self.status == "READABLE" and (failures or self.required_changes):
            raise ValueError("READABLE requires all checks to pass and no required changes")
        if self.status == "REVISION_REQUIRED" and not (failures and self.required_changes):
            raise ValueError("REVISION_REQUIRED requires a failed check and required change")
        return self


class FinalPublicationResult(BaseModel):
    readability_status: Literal["READABLE", "REVISION_REQUIRED"]
    readability_certification_path: str
    report_markdown_path: str | None = None
    report_pdf_path: str | None = None
    publication_manifest_path: str | None = None


class FinalPublicationRunner:
    """Assemble, readability-check, and render the immutable Human publication."""

    def __init__(
        self,
        *,
        repo: MeetingRepository,
        engine: MeetingEngine,
        governance_docs: str | Path,
        max_output_tokens: int | None = None,
    ):
        self.repo = repo
        self.engine = engine
        self.governance_docs = Path(governance_docs)
        self.max_output_tokens = max_output_tokens

    def run(
        self,
        *,
        certified_resolution_path: Path,
        chair_handoff_path: Path,
        epistemic_reviews_path: Path,
        execution_reviews_path: Path,
        human_review_packet_path: Path,
    ) -> FinalPublicationResult:
        frozen = self._resume_frozen_publication()
        if frozen is not None:
            self._ensure_visible_publication_links(frozen)
            return frozen
        source_text, source_paths, citation_manifest = self._build_source(
            certified_resolution_path=certified_resolution_path,
            chair_handoff_path=chair_handoff_path,
            epistemic_reviews_path=epistemic_reviews_path,
            execution_reviews_path=execution_reviews_path,
        )
        source_sha = self._sha256(source_text)
        draft_relative = (
            Path("governance_private/finalization/final_publication_drafts")
            / f"{source_sha}.md"
        )
        draft_path = self.repo.root / draft_relative
        if not draft_path.exists():
            self.repo.docs.write_once(draft_relative, source_text)

        certification_path, certification = self._chair_readability_check(
            source_text=source_text,
            source_sha=source_sha,
        )
        if certification.status != "READABLE":
            return FinalPublicationResult(
                readability_status=certification.status,
                readability_certification_path=str(
                    certification_path.relative_to(self.repo.root)
                ),
            )

        manifest_relative = Path("public/final/final_publication_manifest.json")
        manifest_path = self.repo.root / manifest_relative
        if manifest_path.exists():
            frozen = json.loads(manifest_path.read_text(encoding="utf-8"))
            markdown_path = self.repo.root / frozen["report_markdown_path"]
            pdf_path = self.repo.root / frozen["report_pdf_path"]
            if (
                frozen.get("status") != "FROZEN"
                or frozen.get("report_markdown_sha256") != source_sha
                or not markdown_path.exists()
                or self._sha256(markdown_path.read_text(encoding="utf-8")) != source_sha
                or not pdf_path.exists()
                or self._sha256_bytes(pdf_path.read_bytes())
                != frozen.get("report_pdf_sha256")
            ):
                raise ValueError("frozen final publication manifest does not match artifacts")
            return FinalPublicationResult(
                readability_status=certification.status,
                readability_certification_path=str(
                    certification_path.relative_to(self.repo.root)
                ),
                report_markdown_path=frozen["report_markdown_path"],
                report_pdf_path=frozen["report_pdf_path"],
                publication_manifest_path=str(manifest_relative),
            )

        markdown_relative = Path("public/final/final_report.md")
        markdown_path = self.repo.root / markdown_relative
        if markdown_path.exists():
            if self._sha256(markdown_path.read_text(encoding="utf-8")) != source_sha:
                raise ValueError("frozen final report Markdown does not match readability source")
        else:
            self.repo.docs.write_once(markdown_relative, source_text)

        pdf_relative = Path("public/final/final_report.pdf")
        pdf_path = self.repo.root / pdf_relative
        pdf_provenance_relative = Path("public/final/final_report.pdf.provenance.json")
        pdf_provenance_path = self.repo.root / pdf_provenance_relative
        if not pdf_path.exists():
            pdf_bytes, font_path = render_publication_pdf(
                source_text,
                meeting_id=self.repo.meeting_id,
            )
            pdf_provenance = {
                "meeting_id": self.repo.meeting_id,
                "template_version": PUBLICATION_TEMPLATE_VERSION,
                "source_sha256": source_sha,
                "pdf_sha256": self._sha256_bytes(pdf_bytes),
                "embedded_font_file": font_path.name,
                "embedded_font_sha256": self._sha256_bytes(font_path.read_bytes()),
                "embedded_fallback_font_file": resolve_symbol_font().name,
                "embedded_fallback_font_sha256": self._sha256_bytes(
                    resolve_symbol_font().read_bytes()
                ),
            }
            if pdf_provenance_path.exists():
                if json.loads(pdf_provenance_path.read_text(encoding="utf-8")) != pdf_provenance:
                    raise ValueError("PDF provenance does not match deterministic render")
            else:
                self.repo.docs.write_once(
                    pdf_provenance_relative,
                    json.dumps(pdf_provenance, indent=2, ensure_ascii=False),
                )
            self.repo.docs.write_once(pdf_relative, pdf_bytes)
        else:
            pdf_bytes = pdf_path.read_bytes()
            validate_pdf(pdf_bytes)
            if not pdf_provenance_path.exists():
                raise ValueError("frozen final report PDF exists without provenance")
            pdf_provenance = json.loads(
                pdf_provenance_path.read_text(encoding="utf-8")
            )
            if (
                pdf_provenance.get("source_sha256") != source_sha
                or pdf_provenance.get("pdf_sha256") != self._sha256_bytes(pdf_bytes)
            ):
                raise ValueError("frozen final report PDF does not match provenance")

        manifest = {
            "meeting_id": self.repo.meeting_id,
            "status": "FROZEN",
            "template_version": PUBLICATION_TEMPLATE_VERSION,
            "body_policy": "CERTIFIED_RESULT_AND_CHAIR_HANDOFF_AS_MAIN_BODY",
            "appendix_policy": "INDEPENDENT_REVIEW_OPINIONS_AS_ADVISORY_APPENDICES",
            "citation_policy": citation_manifest,
            "chair_readability_status": certification.status,
            "chair_readability_certification_path": str(
                certification_path.relative_to(self.repo.root)
            ),
            "source_paths": source_paths,
            "human_review_packet_path": str(
                human_review_packet_path.relative_to(self.repo.root)
            ),
            "report_markdown_path": str(markdown_relative),
            "report_markdown_sha256": source_sha,
            "report_pdf_path": str(pdf_relative),
            "report_pdf_sha256": self._sha256_bytes(pdf_bytes),
            "report_pdf_provenance_path": str(pdf_provenance_relative),
            "embedded_font_file": pdf_provenance["embedded_font_file"],
            "embedded_font_sha256": pdf_provenance["embedded_font_sha256"],
            "embedded_fallback_font_file": pdf_provenance[
                "embedded_fallback_font_file"
            ],
            "embedded_fallback_font_sha256": pdf_provenance[
                "embedded_fallback_font_sha256"
            ],
        }
        self.repo.docs.write_once(
            manifest_relative,
            json.dumps(manifest, indent=2, ensure_ascii=False),
        )
        self.repo.events.append(
            "FINAL_HUMAN_PUBLICATION_FROZEN",
            {
                "meeting_id": self.repo.meeting_id,
                "report_markdown_path": str(markdown_relative),
                "report_pdf_path": str(pdf_relative),
                "report_pdf_sha256": manifest["report_pdf_sha256"],
                "manifest_path": str(manifest_relative),
            },
            actor="orchestrator",
        )
        result = FinalPublicationResult(
            readability_status=certification.status,
            readability_certification_path=str(
                certification_path.relative_to(self.repo.root)
            ),
            report_markdown_path=str(markdown_relative),
            report_pdf_path=str(pdf_relative),
            publication_manifest_path=str(manifest_relative),
        )
        self._ensure_visible_publication_links(result)
        return result

    def _ensure_visible_publication_links(self, result: FinalPublicationResult) -> None:
        if not result.report_pdf_path or not result.report_markdown_path:
            return
        created = []
        for link_name, target in (
            ("FINAL_REPORT.pdf", result.report_pdf_path),
            ("FINAL_REPORT.md", result.report_markdown_path),
        ):
            _path, was_created = ensure_visible_link(
                self.repo.root,
                link_name=link_name,
                target_relative=target,
                replace_symlink=True,
            )
            if was_created:
                created.append(link_name)
        literature = self.repo.root / "public/research/literature_bundle.zip"
        if literature.exists():
            _path, was_created = ensure_visible_link(
                self.repo.root,
                link_name="LITERATURE_BUNDLE.zip",
                target_relative="public/research/literature_bundle.zip",
            )
            if was_created:
                created.append("LITERATURE_BUNDLE.zip")
        if created:
            self.repo.events.append(
                "HUMAN_VISIBLE_PUBLICATION_LINKS_CREATED",
                {
                    "meeting_id": self.repo.meeting_id,
                    "entry_points": created,
                },
                actor="orchestrator",
            )

    def _resume_frozen_publication(self) -> FinalPublicationResult | None:
        manifest_relative = Path("public/final/final_publication_manifest.json")
        manifest_path = self.repo.root / manifest_relative
        if not manifest_path.exists():
            return None
        frozen = json.loads(manifest_path.read_text(encoding="utf-8"))
        certification_path = self.repo.root / frozen["chair_readability_certification_path"]
        markdown_path = self.repo.root / frozen["report_markdown_path"]
        pdf_path = self.repo.root / frozen["report_pdf_path"]
        if not certification_path.exists():
            raise ValueError("frozen final publication is missing Chair readability certification")
        certification = ChairReadabilityCertification.model_validate_json(
            certification_path.read_text(encoding="utf-8")
        )
        pdf_bytes = pdf_path.read_bytes() if pdf_path.exists() else b""
        if (
            frozen.get("status") != "FROZEN"
            or certification.status != "READABLE"
            or not markdown_path.exists()
            or self._sha256(markdown_path.read_text(encoding="utf-8"))
            != frozen.get("report_markdown_sha256")
            or not pdf_path.exists()
            or self._sha256_bytes(pdf_bytes) != frozen.get("report_pdf_sha256")
        ):
            raise ValueError("frozen final publication manifest does not match artifacts")
        validate_pdf(pdf_bytes)
        if frozen.get("template_version") != PUBLICATION_TEMPLATE_VERSION:
            return self._upgrade_frozen_pdf_presentation(
                frozen=frozen,
                certification=certification,
                markdown_path=markdown_path,
            )
        return FinalPublicationResult(
            readability_status=certification.status,
            readability_certification_path=frozen["chair_readability_certification_path"],
            report_markdown_path=frozen["report_markdown_path"],
            report_pdf_path=frozen["report_pdf_path"],
            publication_manifest_path=str(manifest_relative),
        )

    def _upgrade_frozen_pdf_presentation(
        self,
        *,
        frozen: dict,
        certification: ChairReadabilityCertification,
        markdown_path: Path,
    ) -> FinalPublicationResult:
        """Render the current presentation beside an immutable legacy publication."""

        manifest_relative = Path("public/final/final_publication_manifest_v2.json")
        manifest_path = self.repo.root / manifest_relative
        markdown_relative = Path("public/final/final_report_v2.md")
        upgraded_markdown_path = self.repo.root / markdown_relative
        pdf_relative = Path("public/final/final_report_v2.pdf")
        pdf_path = self.repo.root / pdf_relative
        provenance_relative = Path("public/final/final_report_v2.pdf.provenance.json")
        source_paths = frozen.get("source_paths") or {}
        required_source_keys = {
            "certified_resolution",
            "chair_execution_handoff_brief",
            "think_tank_epistemic_reviews",
            "think_tank_execution_reviews",
        }
        if set(source_paths) >= required_source_keys:
            source_text, rebuilt_source_paths, citation_manifest = self._build_source(
                certified_resolution_path=self.repo.root
                / source_paths["certified_resolution"],
                chair_handoff_path=self.repo.root
                / source_paths["chair_execution_handoff_brief"],
                epistemic_reviews_path=self.repo.root
                / source_paths["think_tank_epistemic_reviews"],
                execution_reviews_path=self.repo.root
                / source_paths["think_tank_execution_reviews"],
            )
        else:
            # Very early manifests did not record their component paths.  They
            # can still receive the renderer upgrade, but cannot safely rebuild
            # bibliography metadata from frozen source documents.
            source_text = markdown_path.read_text(encoding="utf-8")
            rebuilt_source_paths = source_paths
            citation_manifest = frozen.get("citation_policy", {})
        source_sha = self._sha256(source_text)

        if manifest_path.exists():
            upgraded = json.loads(manifest_path.read_text(encoding="utf-8"))
            if (
                upgraded.get("status") != "FROZEN"
                or upgraded.get("template_version") != PUBLICATION_TEMPLATE_VERSION
                or upgraded.get("report_markdown_sha256") != source_sha
                or not upgraded_markdown_path.exists()
                or self._sha256(upgraded_markdown_path.read_text(encoding="utf-8"))
                != source_sha
                or not pdf_path.exists()
                or self._sha256_bytes(pdf_path.read_bytes())
                != upgraded.get("report_pdf_sha256")
            ):
                raise ValueError("V2 final publication manifest does not match artifacts")
            return FinalPublicationResult(
                readability_status=certification.status,
                readability_certification_path=frozen[
                    "chair_readability_certification_path"
                ],
                report_markdown_path=str(markdown_relative),
                report_pdf_path=str(pdf_relative),
                publication_manifest_path=str(manifest_relative),
            )

        if upgraded_markdown_path.exists():
            if self._sha256(upgraded_markdown_path.read_text(encoding="utf-8")) != source_sha:
                raise ValueError("existing V2 Markdown does not match deterministic rebuild")
        else:
            self.repo.docs.write_once(markdown_relative, source_text)

        pdf_bytes, font_path = render_publication_pdf(
            source_text,
            meeting_id=self.repo.meeting_id,
        )
        provenance = {
            "meeting_id": self.repo.meeting_id,
            "template_version": PUBLICATION_TEMPLATE_VERSION,
            "source_sha256": source_sha,
            "pdf_sha256": self._sha256_bytes(pdf_bytes),
            "embedded_font_file": font_path.name,
            "embedded_font_sha256": self._sha256_bytes(font_path.read_bytes()),
            "embedded_fallback_font_file": resolve_symbol_font().name,
            "embedded_fallback_font_sha256": self._sha256_bytes(
                resolve_symbol_font().read_bytes()
            ),
            "supersedes_presentation_manifest": "public/final/final_publication_manifest.json",
        }
        if pdf_path.exists():
            if self._sha256_bytes(pdf_path.read_bytes()) != provenance["pdf_sha256"]:
                raise ValueError("existing V2 PDF does not match deterministic render")
        else:
            self.repo.docs.write_once(pdf_relative, pdf_bytes)
        provenance_path = self.repo.root / provenance_relative
        if provenance_path.exists():
            if json.loads(provenance_path.read_text(encoding="utf-8")) != provenance:
                raise ValueError("existing V2 PDF provenance does not match render")
        else:
            self.repo.docs.write_once(
                provenance_relative,
                json.dumps(provenance, indent=2, ensure_ascii=False),
            )
        upgraded = dict(frozen)
        upgraded.update(
            {
                "template_version": PUBLICATION_TEMPLATE_VERSION,
                "report_markdown_path": str(markdown_relative),
                "report_markdown_sha256": source_sha,
                "report_pdf_path": str(pdf_relative),
                "report_pdf_sha256": provenance["pdf_sha256"],
                "report_pdf_provenance_path": str(provenance_relative),
                "embedded_font_file": provenance["embedded_font_file"],
                "embedded_font_sha256": provenance["embedded_font_sha256"],
                "embedded_fallback_font_file": provenance[
                    "embedded_fallback_font_file"
                ],
                "embedded_fallback_font_sha256": provenance[
                    "embedded_fallback_font_sha256"
                ],
                "supersedes_presentation_manifest": "public/final/final_publication_manifest.json",
                "source_paths": rebuilt_source_paths,
                "citation_policy": citation_manifest,
            }
        )
        self.repo.docs.write_once(
            manifest_relative,
            json.dumps(upgraded, indent=2, ensure_ascii=False),
        )
        self.repo.events.append(
            "FINAL_HUMAN_PUBLICATION_PRESENTATION_UPGRADED",
            {
                "meeting_id": self.repo.meeting_id,
                "from_template_version": frozen.get("template_version"),
                "to_template_version": PUBLICATION_TEMPLATE_VERSION,
                "report_pdf_path": str(pdf_relative),
                "report_pdf_sha256": provenance["pdf_sha256"],
                "manifest_path": str(manifest_relative),
            },
            actor="orchestrator",
        )
        return FinalPublicationResult(
            readability_status=certification.status,
            readability_certification_path=frozen[
                "chair_readability_certification_path"
            ],
            report_markdown_path=str(markdown_relative),
            report_pdf_path=str(pdf_relative),
            publication_manifest_path=str(manifest_relative),
        )

    def _chair_readability_check(
        self,
        *,
        source_text: str,
        source_sha: str,
    ) -> tuple[Path, ChairReadabilityCertification]:
        public_relative = Path("public/final/chair_readability_certification.json")
        public_path = self.repo.root / public_relative
        if public_path.exists():
            certification = ChairReadabilityCertification.model_validate_json(
                public_path.read_text(encoding="utf-8")
            )
            if certification.publication_source_sha256 != source_sha:
                raise ValueError("Chair readability certification refers to another source")
            return public_path, certification

        attempt_relative = (
            Path("governance_private/finalization/readability_reviews")
            / f"{source_sha}.json"
        )
        attempt_path = self.repo.root / attempt_relative
        if attempt_path.exists():
            certification = ChairReadabilityCertification.model_validate_json(
                attempt_path.read_text(encoding="utf-8")
            )
        else:
            self.engine.status.phase = MeetingPhase.CHAIR_REVIEW
            self.engine.status.paused_reason = None
            self.engine.progress.status(
                MeetingPhase.CHAIR_REVIEW,
                "Chair 正在检查最终出版稿的正文/附录分隔、导航、编号引文和文本可读性",
            )
            system_text = self._chair_system_text()
            user_text = (
                "Perform a presentation-only readability check of the proposed final publication. "
                "Do not reconsider, summarize, rewrite, strengthen, weaken, or correct the certified "
                "substantive result or any independent review opinion. Check exactly these five items: "
                "MAIN_RESULT_IDENTIFIABLE, ADVISORY_APPENDICES_SEPARATE, NAVIGATION_AND_STRUCTURE, "
                "SOURCE_LABELS_CLEAR, and SOURCE_TEXT_LEGIBLE. SOURCE_LABELS_CLEAR must cover both "
                "section provenance and whether every numeric citation resolves to its evidence packet "
                "and bibliography entry. Return exactly one JSON object with "
                "status READABLE or REVISION_REQUIRED; publication_source_sha256; five distinct checks "
                "with name/status PASS or FAIL/explanation; required_changes; and summary. READABLE is "
                "legal only when all five checks pass and required_changes is empty. Any requested change "
                "must concern presentation only. Do not use Markdown.\n\n"
                f"PUBLICATION SOURCE SHA-256: {source_sha}\n\n"
                "PROPOSED FINAL PUBLICATION:\n"
                + source_text
            )
            response = self.engine.find_recorded_response(
                "CHAIR",
                system_text=system_text,
                user_text=user_text,
                stage="chair_final_publication_readability",
            )
            if response is None:
                response = self.engine.invoke_participant(
                    "CHAIR",
                    system_text=system_text,
                    user_text=user_text,
                    stage="chair_final_publication_readability",
                    max_output_tokens=self.max_output_tokens,
                )
            else:
                self.engine.progress.info("已恢复 Chair 的最终出版稿可读性检查响应")
            certification = self.engine.validate_structured_response(
                "CHAIR",
                response=response,
                schema_model=ChairReadabilityCertification,
                stage="chair_final_publication_readability",
                max_output_tokens=self.max_output_tokens,
                semantic_requirement=(
                    "Presentation-only readability review; never change the certified result or "
                    "independent review opinions."
                ),
            )
            if certification.publication_source_sha256 != source_sha:
                raise ValueError("Chair readability certification echoed the wrong source hash")
            self.repo.docs.write_once(
                attempt_relative,
                certification.model_dump_json(indent=2),
            )
            self.repo.events.append(
                "CHAIR_FINAL_PUBLICATION_READABILITY_REVIEWED",
                {
                    "meeting_id": self.repo.meeting_id,
                    "status": certification.status,
                    "publication_source_sha256": source_sha,
                    "record_path": str(attempt_relative),
                },
                actor="CHAIR",
            )
        if certification.status == "READABLE":
            self.repo.docs.write_once(
                public_relative,
                certification.model_dump_json(indent=2),
            )
            self.repo.events.append(
                "CHAIR_FINAL_PUBLICATION_READABILITY_CERTIFIED",
                {
                    "meeting_id": self.repo.meeting_id,
                    "publication_source_sha256": source_sha,
                    "record_path": str(public_relative),
                },
                actor="CHAIR",
            )
            return public_path, certification
        return attempt_path, certification

    def _build_source(
        self,
        *,
        certified_resolution_path: Path,
        chair_handoff_path: Path,
        epistemic_reviews_path: Path,
        execution_reviews_path: Path,
    ) -> tuple[str, dict[str, str], dict[str, object]]:
        source_paths = {
            "certified_resolution": str(
                certified_resolution_path.relative_to(self.repo.root)
            ),
            "chair_execution_handoff_brief": str(
                chair_handoff_path.relative_to(self.repo.root)
            ),
            "think_tank_epistemic_reviews": str(
                epistemic_reviews_path.relative_to(self.repo.root)
            ),
            "think_tank_execution_reviews": str(
                execution_reviews_path.relative_to(self.repo.root)
            ),
        }
        epistemic = json.loads(epistemic_reviews_path.read_text(encoding="utf-8"))
        execution = json.loads(execution_reviews_path.read_text(encoding="utf-8"))
        citation_appendix, citation_manifest = _build_academic_citation_appendix(
            self.repo
        )
        sections = [
            f"# Project ENSEMBLE 最终成果 · {self.repo.meeting_id}",
            "",
            "> 本文档的正文是程序认证后的正式成果及 Chair 的执行移交说明。"
            "附录中的 Librarian / 智库长（Think Tank）审查是独立咨询意见，不自动修改正文，也不自动重开会议。",
            "",
            "# 正文",
            "",
            "## Chair 执行与移交说明",
            "",
            _demote_headings(
                chair_handoff_path.read_text(encoding="utf-8").strip(), by=2
            ),
            "",
            "## 程序认证后的正式成果",
            "",
            _demote_headings(
                certified_resolution_path.read_text(encoding="utf-8").strip(), by=2
            ),
            "",
            "# 附录 A：独立知识完整性审查",
            "",
            "本附录为咨询性材料，不具有自动改写正文、重新表决或重开会议的效力。",
            "",
            _format_epistemic_reviews(epistemic),
            "",
            "# 附录 B：独立执行移交审查",
            "",
            "本附录检查正文和移交说明的知识与执行风险；各审查相互独立，未由 Chair 综合。",
            "",
            _format_execution_reviews(execution),
            "",
        ]
        if citation_appendix:
            sections.extend(
                [
                    "# 附录 C：会议文献证据索引",
                    "",
                    citation_appendix,
                    "",
                    "# 附录 D：来源与完整性",
                ]
            )
        else:
            sections.append("# 附录 C：来源与完整性")
        sections.extend(
            [
            "",
            f"- 出版模板版本：`{PUBLICATION_TEMPLATE_VERSION}`",
            f"- 学术引文格式：`{ACADEMIC_CITATION_STYLE}`",
            f"- 引用的 evidence packet 数：`{citation_manifest['cited_evidence_packet_count']}`",
            f"- 去重参考文献数：`{citation_manifest['reference_count']}`",
            f"- evidence packet 集合 SHA-256：`{citation_manifest['evidence_packet_set_sha256']}`",
            ]
        )
        for name, relative in source_paths.items():
            source = self.repo.root / relative
            sections.append(
                f"- `{name}`：`{relative}`；SHA-256 `{self._sha256(source.read_text(encoding='utf-8'))}`"
            )
        return "\n".join(sections).rstrip() + "\n", source_paths, citation_manifest

    def _chair_system_text(self) -> str:
        paths = (
            self.governance_docs / "03_roles/chair/chair_role.md",
            self.governance_docs / "07_runtime_memory/other_participants/deliberation_chair.md",
            self.governance_docs / "02_deliberation/finalization_and_handoff.md",
            self.governance_docs / "06_execution_interface/execution_handoff.md",
        )
        return "\n\n".join(path.read_text(encoding="utf-8") for path in paths)

    @staticmethod
    def _sha256(text: str) -> str:
        return hashlib.sha256(text.encode("utf-8")).hexdigest()

    @staticmethod
    def _sha256_bytes(content: bytes) -> str:
        return hashlib.sha256(content).hexdigest()


def _format_epistemic_reviews(bundle: dict) -> str:
    sections: list[str] = []
    for index, item in enumerate(bundle.get("reviews", []), start=1):
        review = item["review"]
        sections.extend(
            [
                f"## A.{index} · {item['reviewer_id']}",
                "",
                f"- 知识状态：`{review['epistemic_status']}`",
                f"- 是否建议 Human 考虑重开：`{str(review['reconvene_worthy']).lower()}`",
                f"- 摘要：{review['summary']}",
            ]
        )
        if review.get("reconvene_reason"):
            sections.append(f"- 重开建议理由：{review['reconvene_reason']}")
        findings = review.get("findings", [])
        if not findings:
            sections.append("- Findings：无")
        for finding_index, finding in enumerate(findings, start=1):
            sections.extend(
                [
                    "",
                    f"### A.{index}.{finding_index} · {finding['severity']} / {finding['category']}",
                    "",
                    finding["finding"],
                    "",
                    f"- Human 注意事项：{finding['human_attention']}",
                    "- 证据引用：" + "; ".join(finding["evidence_refs"]),
                ]
            )
        sections.append("")
    return "\n".join(sections).rstrip()


def _demote_headings(text: str, *, by: int) -> str:
    def replace(match: re.Match[str]) -> str:
        level = min(6, len(match.group(1)) + by)
        return "#" * level + " " + match.group(2)

    return re.sub(r"^(#{1,6})\s+(.+)$", replace, text, flags=re.MULTILINE)


def _format_execution_reviews(bundle: dict) -> str:
    sections: list[str] = []
    for index, item in enumerate(bundle.get("reviews", []), start=1):
        review = item["review"]
        sections.extend(
            [
                f"## B.{index} · {item['reviewer_id']}",
                "",
                f"- 执行就绪判断：`{review['execution_readiness']}`",
                f"- 是否建议 Human 考虑重开：`{str(review['reconvene_worthy']).lower()}`",
                f"- 摘要：{review['summary']}",
            ]
        )
        if review.get("reconvene_reason"):
            sections.append(f"- 重开建议理由：{review['reconvene_reason']}")
        for decision in review.get("human_decision_points", []):
            sections.append(f"- Human 决策点：{decision}")
        findings = review.get("findings", [])
        if not findings:
            sections.append("- Findings：无")
        for finding_index, finding in enumerate(findings, start=1):
            sections.extend(
                [
                    "",
                    f"### B.{index}.{finding_index} · {finding['severity']} / {finding['category']}",
                    "",
                    finding["finding"],
                    "",
                    f"- Human 注意事项：{finding['human_attention']}",
                    "- 证据引用：" + "; ".join(finding["evidence_refs"]),
                ]
            )
        sections.append("")
    return "\n".join(sections).rstrip()


def _build_academic_citation_appendix(
    repo: MeetingRepository,
) -> tuple[str, dict[str, object]]:
    """Build a deterministic, source-validated numeric citation apparatus.

    The final normative text has no reliable statement-to-packet provenance in
    legacy meetings.  We therefore cite only the factual findings that a
    released EvidencePacket itself connects to a source, rather than guessing
    that a source supports a superficially similar resolution clause.
    """

    packet_root = repo.root / "public/research/evidence_packets"
    packet_paths = sorted(packet_root.glob("RP-*.json")) if packet_root.exists() else []
    packet_records: list[tuple[Path, EvidencePacket]] = []
    packet_hashes: list[dict[str, str]] = []
    for path in packet_paths:
        content = path.read_bytes()
        packet = EvidencePacket.model_validate_json(content)
        if path.stem != packet.packet_id:
            raise ValueError("evidence packet filename and packet_id do not match")
        packet_records.append((path, packet))
        packet_hashes.append(
            {
                "packet_id": packet.packet_id,
                "sha256": hashlib.sha256(content).hexdigest(),
            }
        )

    superseded = {
        packet_id
        for _path, packet in packet_records
        for packet_id in packet.supersedes_packet_ids
    }
    active_packets = [
        packet for _path, packet in packet_records if packet.packet_id not in superseded
    ]

    references: list[dict[str, object]] = []
    reference_indexes: dict[str, int] = {}
    cited_packet_count = 0
    evidence_lines: list[str] = []
    direction_fields = (
        ("支持", "supporting_evidence"),
        ("反证", "contradictory_evidence"),
        ("适用限制", "scope_limitations"),
        ("替代解释", "canonical_alternatives"),
    )
    for packet in active_packets:
        sources_by_id = {source.source_id: source for source in packet.sources}
        direction_citations: list[str] = []
        packet_has_citation = False
        for label, field_name in direction_fields:
            numbers: list[int] = []
            for finding in getattr(packet, field_name):
                source = sources_by_id[finding.source_id]
                key = _academic_source_key(source)
                if key not in reference_indexes:
                    reference_indexes[key] = len(references) + 1
                    references.append(
                        {
                            "source": source,
                            "packet_ids": [packet.packet_id],
                        }
                    )
                else:
                    record = references[reference_indexes[key] - 1]
                    packet_ids = record["packet_ids"]
                    if packet.packet_id not in packet_ids:
                        packet_ids.append(packet.packet_id)
                number = reference_indexes[key]
                if number not in numbers:
                    numbers.append(number)
            if numbers:
                packet_has_citation = True
                direction_citations.append(
                    f"{label} " + _format_numeric_citations(numbers)
                )
        if not packet_has_citation:
            continue
        cited_packet_count += 1
        evidence_lines.extend(
            [
                f"### {packet.packet_id}",
                "",
                packet.normalized_claim,
                "",
                "- " + "；".join(direction_citations),
                f"- 知识状态：`{packet.knowledge_status.value}`；文献共识：`{packet.consensus.value}`",
                "",
            ]
        )

    packet_set_sha = hashlib.sha256(
        json.dumps(packet_hashes, sort_keys=True, separators=(",", ":")).encode("utf-8")
    ).hexdigest()
    manifest: dict[str, object] = {
        "style": ACADEMIC_CITATION_STYLE,
        "scope": "ACTIVE_RELEASED_EVIDENCE_PACKET_FINDINGS",
        "loaded_evidence_packet_count": len(packet_records),
        "cited_evidence_packet_count": cited_packet_count,
        "reference_count": len(references),
        "superseded_evidence_packet_count": len(superseded),
        "evidence_packet_set_sha256": packet_set_sha,
        "reference_trace": [
            {
                "reference_number": number,
                "source_id": record["source"].source_id,
                "doi": record["source"].doi,
                "url": record["source"].url,
                "evidence_use_class": record["source"].evidence_use_class.value,
                "packet_ids": record["packet_ids"],
                "archive_status": record["source"].archive_status.value,
                "archived_path": record["source"].archived_path,
                "archived_sha256": record["source"].archived_sha256,
            }
            for number, record in enumerate(references, start=1)
        ],
    }
    if not references:
        return "", manifest

    sections = [
        "本附录采用编号制引文。它只连接 Research Desk 已发布 evidence packet 中的事实性 finding "
        "与其实际来源；不把参考文献自动解释为对每条规范性决议的背书，也不把未采用的调研命题"
        "写入正式成果。",
        "",
        "## C.1 证据包索引",
        "",
        *evidence_lines,
        "# 参考文献 / References",
        "",
    ]
    for number, record in enumerate(references, start=1):
        sections.append(
            _format_academic_reference(
                number,
                record["source"],
                packet_ids=record["packet_ids"],
            )
        )
        sections.append("")
    return "\n".join(sections).rstrip(), manifest


def _academic_source_key(source: EvidenceSource) -> str:
    doi = (source.doi or "").strip().lower()
    doi = re.sub(r"^(?:https?://(?:dx\.)?doi\.org/|doi:\s*)", "", doi)
    if doi:
        return f"doi:{doi}"
    return "url:" + source.url.strip().rstrip("/").lower()


def _format_numeric_citations(numbers: list[int]) -> str:
    """Render sorted citation numbers using conventional lists and ranges."""

    ordered = sorted(set(numbers))
    ranges: list[str] = []
    start = previous = ordered[0]
    for number in ordered[1:]:
        if number == previous + 1:
            previous = number
            continue
        ranges.append(str(start) if start == previous else f"{start}–{previous}")
        start = previous = number
    ranges.append(str(start) if start == previous else f"{start}–{previous}")
    return "[" + ", ".join(ranges) + "]"


def _format_academic_reference(
    number: int,
    source: EvidenceSource,
    *,
    packet_ids: list[str],
) -> str:
    corporate_author = (
        source.venue
        if not source.authors
        and source.evidence_use_class == EvidenceUseClass.AUTHORITATIVE
        and source.venue
        else None
    )
    authors = ", ".join(source.authors) if source.authors else corporate_author
    components = []
    if authors:
        components.append(authors)
    components.append(source.title)
    if source.venue and source.venue != corporate_author:
        components.append(f"*{source.venue}*")
    if source.publication_year is not None:
        components.append(str(source.publication_year))
    doi = (source.doi or "").strip()
    doi = re.sub(r"^(?:https?://(?:dx\.)?doi\.org/|doi:\s*)", "", doi, flags=re.I)
    locator = f"https://doi.org/{doi}" if doi else source.url
    components.append(locator)
    return f"[{number}] " + ". ".join(components) + "."


def resolve_cjk_font() -> Path:
    configured = os.environ.get("ENSEMBLE_CJK_FONT")
    candidates = [
        Path(configured) if configured else None,
        Path(__file__).resolve().parents[1] / "assets/fonts/HarmonyOS_Sans_SC_Regular.ttf",
        Path("/usr/share/fonts/google-droid-sans-fonts/DroidSansFallbackFull.ttf"),
        Path("/usr/share/fonts/opentype/noto/NotoSansCJK-Regular.ttc"),
        Path("/usr/share/fonts/truetype/wqy/wqy-zenhei.ttc"),
    ]
    for candidate in candidates:
        if candidate is not None and candidate.is_file():
            return candidate.resolve()
    raise RuntimeError(
        "no embeddable CJK font found; set ENSEMBLE_CJK_FONT to a readable TTF/TTC file"
    )


def resolve_symbol_font() -> Path:
    configured = os.environ.get("ENSEMBLE_SYMBOL_FONT")
    candidates = [
        Path(configured) if configured else None,
        Path("/usr/share/fonts/dejavu-sans-fonts/DejaVuSans.ttf"),
        Path(reportlab.__file__).resolve().parent / "fonts/Vera.ttf",
    ]
    for candidate in candidates:
        if candidate is not None and candidate.is_file():
            return candidate.resolve()
    raise RuntimeError(
        "no embeddable symbol font found; set ENSEMBLE_SYMBOL_FONT to a readable TTF file"
    )


def render_publication_pdf(markdown_text: str, *, meeting_id: str) -> tuple[bytes, Path]:
    markdown_text = normalize_math_operator_commands(
        normalize_fragmented_inline_math(repair_json_decoded_math_commands(markdown_text))
    )
    font_path = resolve_cjk_font()
    font_name = "ENSEMBLE-CJK-" + hashlib.sha256(str(font_path).encode()).hexdigest()[:10]
    if font_name not in pdfmetrics.getRegisteredFontNames():
        pdfmetrics.registerFont(TTFont(font_name, str(font_path)))
    pdfmetrics.registerFontFamily(
        font_name,
        normal=font_name,
        bold=font_name,
        italic=font_name,
        boldItalic=font_name,
    )
    fallback_path = resolve_symbol_font()
    fallback_name = "ENSEMBLE-SYMBOL-" + hashlib.sha256(
        str(fallback_path).encode()
    ).hexdigest()[:10]
    if fallback_name not in pdfmetrics.getRegisteredFontNames():
        pdfmetrics.registerFont(TTFont(fallback_name, str(fallback_path)))
    pdfmetrics.registerFontFamily(
        fallback_name,
        normal=fallback_name,
        bold=fallback_name,
        italic=fallback_name,
        boldItalic=fallback_name,
    )
    glyph_context = (
        font_name,
        frozenset(pdfmetrics.getFont(font_name).face.charWidths),
        fallback_name,
        frozenset(pdfmetrics.getFont(fallback_name).face.charWidths),
    )

    buffer = io.BytesIO()
    document = SimpleDocTemplate(
        buffer,
        pagesize=A4,
        rightMargin=19 * mm,
        leftMargin=19 * mm,
        topMargin=22 * mm,
        bottomMargin=20 * mm,
        title=f"Project ENSEMBLE 最终成果 · {meeting_id}",
        author="Project ENSEMBLE",
    )
    styles = _pdf_styles(font_name)
    def canvas_maker(filename, **kwargs):
        kwargs["invariant"] = 1
        return canvas.Canvas(filename, **kwargs)

    def decorate_page(pdf_canvas, doc):
        pdf_canvas.saveState()
        pdf_canvas.setFont(font_name, 8)
        pdf_canvas.setFillColor(colors.HexColor("#5B6472"))
        pdf_canvas.drawString(19 * mm, 11 * mm, f"Project ENSEMBLE · {meeting_id}")
        pdf_canvas.drawRightString(
            A4[0] - 19 * mm,
            11 * mm,
            f"第 {doc.page} 页",
        )
        pdf_canvas.restoreState()

    with PdfMathRenderer() as math_renderer:
        story = _markdown_story(
            markdown_text, styles, glyph_context=glyph_context,
            math_renderer=math_renderer,
        )
        document.build(
            story,
            onFirstPage=decorate_page,
            onLaterPages=decorate_page,
            canvasmaker=canvas_maker,
        )
    content = buffer.getvalue()
    validate_pdf(content)
    return content, font_path


def validate_pdf(content: bytes) -> None:
    if len(content) < 1024 or not content.startswith(b"%PDF-") or b"%%EOF" not in content[-2048:]:
        raise ValueError("generated final publication is not a complete PDF")


def _pdf_styles(font_name: str) -> dict[str, ParagraphStyle]:
    sample = getSampleStyleSheet()
    common = {
        "fontName": font_name,
        "textColor": colors.HexColor("#20242B"),
        "wordWrap": "CJK",
    }
    return {
        "title": ParagraphStyle(
            "EnsembleTitle",
            parent=sample["Title"],
            fontSize=22,
            leading=30,
            alignment=TA_CENTER,
            spaceAfter=14 * mm,
            **common,
        ),
        "h1": ParagraphStyle(
            "EnsembleH1",
            parent=sample["Heading1"],
            fontSize=17,
            leading=23,
            textColor=colors.HexColor("#163A5F"),
            spaceBefore=8 * mm,
            spaceAfter=4 * mm,
            keepWithNext=True,
            **{key: value for key, value in common.items() if key != "textColor"},
        ),
        "h2": ParagraphStyle(
            "EnsembleH2",
            parent=sample["Heading2"],
            fontSize=13,
            leading=19,
            textColor=colors.HexColor("#245D83"),
            spaceBefore=5 * mm,
            spaceAfter=2.5 * mm,
            keepWithNext=True,
            **{key: value for key, value in common.items() if key != "textColor"},
        ),
        "h3": ParagraphStyle(
            "EnsembleH3",
            parent=sample["Heading3"],
            fontSize=11,
            leading=16,
            textColor=colors.HexColor("#3A6C86"),
            spaceBefore=3.5 * mm,
            spaceAfter=2 * mm,
            keepWithNext=True,
            **{key: value for key, value in common.items() if key != "textColor"},
        ),
        "body": ParagraphStyle(
            "EnsembleBody",
            parent=sample["BodyText"],
            fontSize=9.6,
            leading=15,
            # ReportLab's CJK justification expands space between glyphs and
            # made Chinese paragraphs look as if every character were split.
            alignment=TA_LEFT,
            spaceAfter=2.3 * mm,
            **common,
        ),
        "bullet": ParagraphStyle(
            "EnsembleBullet",
            parent=sample["BodyText"],
            fontSize=9.4,
            leading=14,
            leftIndent=5 * mm,
            firstLineIndent=-3.5 * mm,
            alignment=TA_LEFT,
            spaceAfter=1.2 * mm,
            **common,
        ),
        "reference": ParagraphStyle(
            "EnsembleReference",
            parent=sample["BodyText"],
            fontSize=8.7,
            leading=12.6,
            leftIndent=8 * mm,
            firstLineIndent=-8 * mm,
            alignment=TA_LEFT,
            spaceAfter=2.2 * mm,
            allowWidows=0,
            allowOrphans=0,
            **common,
        ),
        "quote": ParagraphStyle(
            "EnsembleQuote",
            parent=sample["BodyText"],
            fontSize=9.4,
            leading=14,
            leftIndent=6 * mm,
            rightIndent=4 * mm,
            borderColor=colors.HexColor("#7A9CB5"),
            borderWidth=0,
            borderPadding=4,
            backColor=colors.HexColor("#EDF4F8"),
            spaceAfter=3 * mm,
            **common,
        ),
        "code": ParagraphStyle(
            "EnsembleCode",
            parent=sample["BodyText"],
            fontSize=8.2,
            leading=12,
            leftIndent=4 * mm,
            rightIndent=3 * mm,
            backColor=colors.HexColor("#F3F5F7"),
            borderPadding=4,
            spaceAfter=2 * mm,
            **common,
        ),
    }


def _markdown_story(
    text: str,
    styles: dict[str, ParagraphStyle],
    *,
    glyph_context: tuple[str, frozenset[int], str, frozenset[int]] | None = None,
    math_renderer: PdfMathRenderer | None = None,
) -> list:
    Paragraph = MathSafeParagraph  # Keep the fallback local to reader-report layout.
    story: list = []
    paragraph_lines: list[str] = []
    code_lines: list[str] = []
    in_code = False
    seen_title = False
    math_closing: str | None = None
    math_lines: list[str] = []
    markup = lambda value: _inline_markup(
        value, glyph_context=glyph_context, math_renderer=math_renderer,
    )

    def flush_paragraph() -> None:
        if paragraph_lines:
            content = _join_markdown_paragraph_lines(paragraph_lines)
            story.append(
                Paragraph(
                    markup(content),
                    styles["body"],
                )
            )
            paragraph_lines.clear()

    def flush_code() -> None:
        if code_lines:
            content = "<br/>".join(
                _safe_pdf_text(line, glyph_context=glyph_context)
                for line in code_lines
            )
            story.append(Paragraph(content, styles["code"]))
            code_lines.clear()

    for raw_line in text.splitlines():
        line = raw_line.rstrip()
        if line.startswith("```"):
            flush_paragraph()
            if in_code:
                flush_code()
            in_code = not in_code
            continue
        if in_code:
            code_lines.append(line)
            continue
        if math_closing is not None:
            content, closed = display_math_continue(line, math_closing)
            if content:
                math_lines.append(content)
            if closed:
                if math_renderer is None:
                    raise ValueError("PDF_MATH_RENDERING_FAILED: math renderer unavailable")
                story.extend(math_renderer.display_flowables(" ".join(math_lines)))
                math_lines.clear()
                math_closing = None
            continue
        math_start = display_math_start(line)
        if math_start is not None:
            flush_paragraph()
            math_closing, content, closed = math_start
            if content:
                math_lines.append(content)
            if closed:
                if math_renderer is None:
                    raise ValueError("PDF_MATH_RENDERING_FAILED: math renderer unavailable")
                story.extend(math_renderer.display_flowables(" ".join(math_lines)))
                math_lines.clear()
                math_closing = None
            continue
        if not line.strip():
            flush_paragraph()
            continue
        heading = re.match(r"^(#{1,6})\s+(.+)$", line)
        if heading:
            flush_paragraph()
            level = len(heading.group(1))
            if level == 1 and seen_title:
                story.append(PageBreak())
            style_name = "title" if level == 1 and not seen_title else f"h{min(level, 3)}"
            story.append(
                Paragraph(
                    markup(heading.group(2)),
                    styles[style_name],
                )
            )
            if level == 1 and not seen_title:
                seen_title = True
            continue
        if re.match(r"^\s*[-*_]{3,}\s*$", line):
            flush_paragraph()
            story.append(Spacer(1, 3 * mm))
            continue
        if re.match(r"^\s*\|?\s*:?-{3,}", line):
            continue
        if line.startswith(">"):
            flush_paragraph()
            story.append(
                Paragraph(
                    markup(line[1:].strip()),
                    styles["quote"],
                )
            )
            continue
        reference = re.match(r"^\[(\d+)\]\s+(.+)$", line)
        if reference:
            flush_paragraph()
            label = _safe_pdf_text(
                f"[{reference.group(1)}] ", glyph_context=glyph_context
            )
            story.append(
                Paragraph(
                    label
                    + markup(reference.group(2)),
                    styles["reference"],
                )
            )
            continue
        bullet = re.match(r"^\s*(?P<label>[-*]|\d+[.)])\s+(?P<body>.+)$", line)
        if bullet:
            flush_paragraph()
            marker = bullet.group("label")
            display_marker = "•" if marker in {"-", "*"} else marker
            story.append(
                Paragraph(
                    _safe_pdf_text(display_marker + " ", glyph_context=glyph_context)
                    + markup(bullet.group("body")),
                    styles["bullet"],
                )
            )
            continue
        if line.strip().startswith("|") and line.strip().endswith("|"):
            flush_paragraph()
            cells = [cell.strip() for cell in line.strip().strip("|").split("|")]
            story.append(
                Paragraph(
                    _safe_pdf_text(" · ", glyph_context=glyph_context).join(
                        markup(cell)
                        for cell in cells
                    ),
                    styles["body"],
                )
            )
            continue
        paragraph_lines.append(line)
    if math_closing is not None:
        raise ValueError("PDF_MATH_RENDERING_FAILED: unclosed display math block")
    flush_paragraph()
    flush_code()
    return story


def _inline_markup(
    value: str,
    *,
    glyph_context: tuple[str, frozenset[int], str, frozenset[int]] | None = None,
    math_renderer: PdfMathRenderer | None = None,
    citation_mode: bool = True,
) -> str:
    # Parse Markdown delimiters before font fallback.  Treating ``**`` as text
    # first can put the opening and closing ``<b>`` tags inside separate
    # fallback-font spans, producing invalid ReportLab markup such as
    # ``<font><b></font>...<font></b></font>``.
    token_pattern = re.compile(
        r"(?P<math>" + INLINE_MATH_TOKEN + r")"
        r"|`(?P<code>[^`\n]+)`"
        r"|\*\*(?P<strong>.+?)\*\*"
        # Do not let an unpaired multiplication asterisk swallow a following
        # math token and turn its delimiters into literal PDF text.
        r"|(?<!\*)\*(?P<emphasis>[^*\\\n]+?)\*(?!\*)"
    )
    from project_ensemble.orchestration.publication_layout import compact_citation_list

    # In-text citations are typographic superscripts, including a lone [1].
    # A bibliography entry's leading [1] is passed with citation_mode=False.
    citation_list = re.compile(
        r"\[\s*\d+(?:\s*(?:[,，]|[-–—])\s*\d+)*\s*\](?!\()"
    )

    def plain(fragment: str) -> str:
        chunks: list[str] = []
        cursor = 0
        for citation in citation_list.finditer(fragment) if citation_mode else ():
            chunks.append(_safe_pdf_text(fragment[cursor:citation.start()], glyph_context=glyph_context))
            label = citation.group()
            if "," in label:
                label = compact_citation_list(label)
            chunks.append(
                '<super><font size="7.4" color="#455A70">'
                + _safe_pdf_text(label, glyph_context=glyph_context)
                + "</font></super>"
            )
            cursor = citation.end()
        chunks.append(_safe_pdf_text(fragment[cursor:], glyph_context=glyph_context))
        return "".join(chunks)

    parts: list[str] = []
    cursor = 0
    for match in token_pattern.finditer(value):
        parts.append(plain(value[cursor : match.start()]))
        if match.group("math") is not None:
            if math_renderer is None:
                parts.append(plain(match.group("math")))
            else:
                parts.append(math_renderer.inline_markup(inline_math_content(match.group("math"))))
            cursor = match.end()
            continue
        if match.group("code") is not None:
            tag = "u"
            content = match.group("code")
            inner = _safe_pdf_text(content, glyph_context=glyph_context)
        elif match.group("strong") is not None:
            tag = "b"
            content = match.group("strong")
            inner = _inline_markup(
                content, glyph_context=glyph_context, math_renderer=math_renderer,
                citation_mode=citation_mode,
            )
        else:
            tag = "i"
            content = match.group("emphasis")
            inner = _inline_markup(
                content, glyph_context=glyph_context, math_renderer=math_renderer,
                citation_mode=citation_mode,
            )
        if tag == "b":
            parts.append('<b><font color="#173A5E">' + inner + '</font></b>')
        else:
            parts.append(f"<{tag}>" + inner + f"</{tag}>")
        cursor = match.end()
    parts.append(plain(value[cursor:]))
    return "".join(parts)


def _safe_pdf_text(
    value: str,
    *,
    glyph_context: tuple[str, frozenset[int], str, frozenset[int]] | None = None,
) -> str:
    normalized = unicodedata.normalize("NFC", value).translate(
        {
            0x00A0: " ",  # no-break space
            0x2007: " ",  # figure space
            0x202F: " ",  # narrow no-break space
            0x00AD: None,  # soft hyphen
            0x200B: None,  # zero-width space (was visible in some CJK fonts)
            0x200C: None,
            0x200D: None,
            0x2060: None,
            0xFEFF: None,
            0x3008: "⟨",  # mathematical angle bracket supported by the symbol font
            0x3009: "⟩",
        }
    )
    normalized = "".join(
        character
        for character in normalized
        if character in "\n\t" or unicodedata.category(character) not in {"Cc", "Cs"}
    )
    if glyph_context is None:
        return html.escape(normalized, quote=False)
    _primary_name, primary_glyphs, fallback_name, fallback_glyphs = glyph_context
    replacements = {
        "✅": "[通过]",
        "❌": "[未通过]",
        "⚠": "[注意]",
        "🔍": "[检索]",
        "📌": "[要点]",
    }
    parts: list[str] = []
    fallback_run: list[str] = []

    def flush_fallback() -> None:
        if fallback_run:
            parts.append(
                f'<font name="{fallback_name}">'
                + html.escape("".join(fallback_run), quote=False)
                + "</font>"
            )
            fallback_run.clear()

    for character in normalized:
        codepoint = ord(character)
        if codepoint in primary_glyphs:
            flush_fallback()
            parts.append(html.escape(character, quote=False))
        elif codepoint in fallback_glyphs:
            fallback_run.append(character)
        else:
            flush_fallback()
            replacement = replacements.get(character, f"[符号 U+{codepoint:04X}]")
            parts.append(html.escape(replacement, quote=False))
    flush_fallback()
    return "".join(parts)


def _join_markdown_paragraph_lines(lines: list[str]) -> str:
    """Join Markdown soft wraps without inserting spaces inside Chinese prose."""
    result = ""
    for raw in lines:
        line = raw.strip()
        if not result:
            result = line
            continue
        previous = result[-1]
        current = line[0]
        if _cjk_or_cjk_punctuation(previous) or _cjk_or_cjk_punctuation(current):
            result += line
        else:
            result += " " + line
    return result


def _cjk_or_cjk_punctuation(character: str) -> bool:
    codepoint = ord(character)
    return (
        0x2E80 <= codepoint <= 0x9FFF
        or 0xF900 <= codepoint <= 0xFAFF
        or 0xFF00 <= codepoint <= 0xFFEF
        or character in "，。！？；：、）》】」』…—"
    )
