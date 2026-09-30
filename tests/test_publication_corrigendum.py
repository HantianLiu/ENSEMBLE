import json
from pathlib import Path

from project_ensemble.chair_qa import latest_draft
from project_ensemble.domain import GenerationResponse, ReasoningEffort
from project_ensemble.publication_corrigendum import ChairCorrigendumService
from project_ensemble.storage.human_outputs import titled_report_stem


class _Chair:
    provider_id = "test"

    def generate(self, request):
        return GenerationResponse(
            text=json.dumps({
                "answer": "已把无信息量的占位语替换为具体证据缺口。",
                "old_text": "- 当前证据不足，未在正文中展开。\n\n- 当前证据不足，未在正文中展开。",
                "new_text": "- 尚缺少目标二维体系的独立有限尺寸标度数据；现有材料不能确定临界温度。",
            }, ensure_ascii=False),
            provider_id="test", model_id=request.model_id,
        )


def test_completed_archive_can_receive_non_destructive_chair_corrigendum(tmp_path: Path):
    root = tmp_path / "LR-TEST"
    (root / "public").mkdir(parents=True)
    original = (
        "# 研究报告\n\n## 未解决问题附录\n\n"
        "- 当前证据不足，未在正文中展开。\n\n"
        "- 当前证据不足，未在正文中展开。\n"
    )
    (root / "FINAL_REPORT.md").write_text(original, encoding="utf-8")
    (root / "public/archive_manifest.json").write_text(json.dumps({
        "status": "ARCHIVED", "retained_document_path": "FINAL_REPORT.md",
    }), encoding="utf-8")
    service = ChairCorrigendumService(
        root=root, adapter=_Chair(), model_id="test-model",
        reasoning_effort=ReasoningEffort.DEFAULT,
    )
    retypeset = service.retypeset()
    assert Path(retypeset["pdf"]).read_bytes().startswith(b"%PDF-")
    result = service.converse("把未解决附录的重复占位语改成具体缺口", edit=True)
    assert result["output"] is not None
    assert (root / "FINAL_REPORT.md").read_text(encoding="utf-8") == original
    corrected = Path(result["output"]["markdown"])
    assert "独立有限尺寸标度数据" in corrected.read_text(encoding="utf-8")
    assert Path(result["output"]["pdf"]).read_bytes().startswith(b"%PDF-")
    assert (root / "FINAL_REPORT_REVISED.md").resolve() == corrected.resolve()
    assert (root / "FINAL_REPORT_REVISED.pdf").resolve() == Path(result["output"]["pdf"]).resolve()
    assert corrected.name == "研究报告（文献调研报告·修订版）.md"
    assert Path(result["output"]["pdf"]).name == "研究报告（文献调研报告·修订版）.pdf"
    assert latest_draft(root) == corrected.resolve()
    assert service.current() == corrected.resolve()


def test_titled_report_name_is_portable_and_bounded():
    stem = titled_report_stem("# 中国/美国：DTaP?\n", kind="文献调研报告", revised=True)
    assert stem == "中国-美国：DTaP（文献调研报告·修订版）"
    assert len(titled_report_stem("# " + "很长" * 1000, kind="文献调研报告").encode("utf-8")) <= 200
