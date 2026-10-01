import io
import json
from types import SimpleNamespace

from project_ensemble.runtime.fast_science_consultation import render_fast_science_consultation


def _write_json(root, relative, value):
    path = root / relative
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(value, ensure_ascii=False), encoding="utf-8")


def test_local_patch_failure_shows_error_and_original_review_objections(tmp_path):
    module = "RM-08"
    _write_json(
        tmp_path,
        "public/literature_report/fast/RM-08/science_review_v1.json",
        {
            "reviews": [{
                "reviewer_id": "R-ONE",
                "checklist": {
                    "issues": [{
                        "location_excerpt": "该处说明热力学一致性。",
                        "questioned_claim": "当前稿中的受质疑说法",
                        "why_it_matters": "没有把检验条件说清楚。",
                        "suggested_response": "补充成立条件。",
                    }],
                    "glossary_corrections": ["补充关键算法名的操作定义。"],
                },
            }],
        },
    )
    _write_json(
        tmp_path,
        "public/literature_report/modules/RM-08/writing_v071/writer_v1_validated.json",
        {"draft": {"body_markdown": "本段说明热力学一致性检验需要适用条件。", "short_summary": "摘要。"}},
    )
    issue = SimpleNamespace(
        issue_id="HC-FAST-SCIENCE-RM-08-LOCAL-FORMAT-V2-C1",
        context={
            "module_id": module,
            "recheck_path": "public/literature_report/fast/RM-08/science_review_v1.json",
            "last_problem": "old_text must occur exactly once in one editable field",
        },
    )

    output = io.StringIO()
    render_fast_science_consultation(SimpleNamespace(root=tmp_path), issue, output=output)
    rendered = output.getvalue()

    assert "局部修稿遇到定位/格式问题" in rendered
    assert "补丁没有应用，不表示科学审阅意见为空" in rendered
    assert "Original review points" not in rendered
    assert "原审阅意见：2 条" in rendered
    assert "异议 1/2" in rendered
    assert "术语表修订意见" in rendered
    assert "技术原因" in rendered
    assert "不会重放这次失败的输出" in rendered
    assert "按编号段落定位" in rendered
