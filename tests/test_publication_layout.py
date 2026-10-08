import io
import re

import pytest
from pypdf import PdfReader

from project_ensemble.orchestration.final_publication import _inline_markup, resolve_cjk_font
from project_ensemble.orchestration.academic_pdf import (
    _pdf_reader_note_fallback, render_academic_review_pdf,
)
from project_ensemble.orchestration.academic_html import (
    normalize_reader_citation_groups, render_academic_review_html,
)
from project_ensemble.orchestration.math_rendering import (
    normalize_fragmented_inline_math,
    normalize_math_operator_commands,
    safe_pdf_font_grouping_repair,
)
from project_ensemble.orchestration.math_rendering import should_display_inline_equation


def test_pdf_repairs_unbraced_indicator_font_without_changing_frozen_formula():
    from project_ensemble.orchestration.math_rendering import PdfMathRenderer

    formula = r"Q_{\mathrm{2ph}}=\int_0^1\mathbf1_{\mathcal C}(x)\,dx"
    repaired = safe_pdf_font_grouping_repair(formula)
    assert repaired == r"Q_{\mathrm{2ph}}=\int_0^1\mathbf{1}_{\mathcal C}(x)\,dx"
    with PdfMathRenderer(repair_formula=lambda value, error, display: safe_pdf_font_grouping_repair(value)) as renderer:
        assert renderer._image(formula, display=True)[1] > 0
    assert r"\mathbf1" in formula


def test_pdf_repairs_single_atom_root_and_double_subscript_grouping():
    from project_ensemble.orchestration.math_rendering import PdfMathRenderer

    formula = r"L_x=\sqrt A,\quad M=s_{xx}_j"
    assert safe_pdf_font_grouping_repair(formula) == r"L_x=\sqrt{A},\quad M={s_{xx}}_j"
    with PdfMathRenderer(repair_formula=lambda value, error, display: safe_pdf_font_grouping_repair(value)) as renderer:
        assert renderer._image(formula, display=True)[1] > 0
from project_ensemble.orchestration.publication_layout import (
    compact_numeric_citations,
    convert_intro_headings,
    heading_numbering_issues,
    markdown_to_latex,
    normalize_publication_headings,
    replace_internal_references,
    sort_numeric_citations,
    split_inline_enumerations,
)


def test_heading_numbers_are_continuous_and_contents_are_not_renumbered():
    source = (
        "# 标题\n\n## 目录\n\n1. 原章\n2. 下章\n\n"
        "## 一、原章\n\n### 5.1 背景\n\n### （一）边界\n\n"
        "## 9. 下章\n\n### 1. 结果\n\n#### 三、方法\n\n"
        "##### 2. 细节\n\n#### 表2：比较\n"
    )
    actual = normalize_publication_headings(source)
    assert "1. 原章\n2. 下章" in actual
    assert "## 1. 原章" in actual
    assert "### 1.1 背景" in actual
    assert "### 1.2 边界" in actual
    assert "## 2. 下章" in actual
    assert "### 2.1 结果" in actual
    assert "#### 2.1.1 方法" in actual
    assert "##### 细节" in actual
    assert "#### 表2：比较" in actual
    assert heading_numbering_issues(actual) == []
    assert heading_numbering_issues("# 标题\n\n## 2. 错号\n\n### 2.3 跳号\n")
    english = normalize_publication_headings("# Review\n\n## Contents\n\n1. Evidence\n\n## Evidence\n\n### Scope\n")
    assert "## Contents" in english and "## 1. Evidence" in english
    assert heading_numbering_issues(english) == []


def test_latex_renders_markdown_structure_and_compacts_citations():
    source = (
        "# 标题\n\n## 目录\n\n1. 章节\n\n## 1. 章节\n\n"
        "### 1.1 证据\n\n**重要**结论 [1, 2, 3, 8, 9]。\n\n"
        "1. **第一项**\n2. 第二项\n\n"
        "| 栏一 | 栏二 |\n| --- | --- |\n| **甲** | 50%<br>多行 |\n\n"
        "## 2. 参考文献\n\n[1] 文献 & DOI。\n"
    )
    latex = markdown_to_latex(source)
    assert latex.count(r"\tableofcontents") == 1
    assert r"\section{章节}" in latex
    assert r"\subsection{证据}" in latex
    assert r"\textbf{重要}" in latex
    assert r"[1–3,\allowbreak 8,\allowbreak 9]" in latex
    assert r"\begin{enumerate}" in latex
    assert r"\begin{longtable}" in latex
    assert r"\textbf{甲}" in latex
    assert r"50\%\newline{}多行" in latex
    assert r"[1] 文献 \& DOI" in latex
    assert "**" not in latex
    assert "| --- |" not in latex


def test_citation_compaction_preserves_order_and_repeated_ids():
    assert compact_numeric_citations("[1, 2, 3, 7, 9, 10, 11, 1]") == "[1–3, 7, 9–11, 1]"
    assert compact_numeric_citations("[1, 342, 4]") == "[1, 342, 4]"
    assert "[1–3]" in _inline_markup("证据 [1, 2, 3]")


def test_in_text_citations_are_superscripts_but_reference_labels_are_not():
    rendered = _inline_markup("证据 [1] 与 [2, 3, 4]；摘要框内 [5–7]")
    assert rendered.count("<super>") == 3
    assert "<super><font" in rendered
    assert "[2–4]" in rendered
    assert "[5–7]" in rendered
    assert "<super>" not in _inline_markup("[1] 作者与题名", citation_mode=False)
    assert "<super>" not in _inline_markup("`[1]` 是代码")


def test_presentation_sorts_numeric_citations_without_changing_membership():
    source = "证据 [9, 1, 4, 1]。\n\n```\n原始 [9, 1]\n```\n"
    actual = sort_numeric_citations(source)
    assert "[1, 1, 4, 9]" in actual
    assert "原始 [9, 1]" in actual


def test_inline_enumeration_becomes_numbered_list_only_for_clear_parallel_items():
    source = "主要发现如下：（1）第一项包含足够的说明。（2）第二项同样包含说明。\n\n比较 (1) 和 (2) 需要额外证据。\n"
    actual = split_inline_enumerations(source)
    assert "主要发现如下：\n\n1. 第一项包含足够的说明。\n2. 第二项同样包含说明。" in actual
    assert "比较 (1) 和 (2) 需要额外证据。" in actual


def test_internal_module_reference_maps_to_actual_chapter_only_in_publication():
    source = "详见 RM-05；证据登记于（RM-09 INF-04）。另见 RM-05-Q14、RD03 和 record_id。"
    actual = replace_internal_references(source, {"RM-05": 6, "RM-09": 10})
    assert "第6章" in actual
    assert "第10章的核验记录" in actual
    assert "第6章的研究问题" in actual
    assert "RM-" not in actual and "RD03" not in actual and "record_id" not in actual


def test_intro_heading_becomes_reader_callout_without_losing_paragraph():
    source = "## 1. 背景\n\n### 1.1 本节摘要\n\n摘要内容。\n\n### 1.2 方法\n\n正文。\n"
    converted = convert_intro_headings(source)
    assert "### 1.1 本节摘要" not in converted
    assert "> **章节导读**\n>\n> 摘要内容。" in converted
    assert "### 1.2 方法" in converted


def test_academic_pdf_renders_chapters_callout_and_wide_table():
    source = (
        "# 综述题目\n\n## 目录\n\n1. 第一章\n2. 参考文献\n\n"
        "## 1. 第一章\n\n> **章节导读**\n>\n> 概括证据边界 [1]。\n\n"
        "| A | B | C | D | E |\n| --- | --- | --- | --- | --- |\n"
        "| 一 | 二 | 三 | 四 | 五 |\n\n## 2. 参考文献\n\n[1] 文献条目。\n"
    )
    pdf, _font = render_academic_review_pdf(source, meeting_id="M-TEST")
    assert pdf.startswith(b"%PDF-")
    assert b"/Outlines" in pdf


def test_academic_html_has_navigation_reader_drawer_and_math():
    source = (
        "# 线张力综述\n\n## 术语表\n\n- **线张力**: 每单位界面长度的自由能 [1]。\n\n"
        "## 1. 方法\n\n线张力可由实验估计 [1]。\n\n"
        "$$\n\\mathcal N(i)=1\n$$\n\n"
        "| 方法 | 条件 |\n|---|---|\n| A | B |\n\n"
        "## 参考文献\n\n[1] Example paper.\n"
    )
    rendered = render_academic_review_html(source, meeting_id="LR-TEST")
    assert '<nav aria-label="目录">' in rendered
    assert '<a href="#section-3">1. 方法</a>' in rendered
    assert '<button type="button" class="term-trigger" data-term="线张力">' in rendered
    assert 'data-references="1"' in rendered
    assert "Example paper." in rendered
    assert '<table>' in rendered
    assert r"\mathcal N(i)" in rendered
    assert '<div class="math display"' in rendered
    assert '<p><div class="math display"' not in rendered
    assert '<aside class="drawer"' in rendered
    assert 'id="drawer-back"' in rendered
    assert "function appendLinkedText(container, value, currentTerm)" in rendered
    assert "if (event.target.closest('main') && !drawer.hidden) closeReaderCards();" in rendered
    assert "每单位界面长度的自由能 [1]。" in rendered
    assert 'id="annotation-highlight"' in rendered
    assert 'id="annotation-create"' in rendered
    assert 'id="annotation-panel"' in rendered


def test_academic_html_citation_cards_link_to_source_and_pdf_safely():
    source = (
        "# Report\n\n## 1. Findings\n\nClaim [1].\n\n"
        "## References\n\n[1] Example article. https://doi.org/10.1234/example.\n"
    )
    rendered = render_academic_review_html(
        source,
        meeting_id="LR-TEST",
        reference_links={
            "1": {
                "source": "https://doi.org/10.1234/example",
                "pdf": "https://publisher.example/article.pdf",
            },
            "2": {"source": "javascript:alert(1)"},
        },
    )
    assert '"referenceLinks": {"1": {"source": "https://doi.org/10.1234/example", "pdf": "https://publisher.example/article.pdf"}}' in rendered
    assert "打开原文 PDF ↗" in rendered
    assert "打开来源网页 ↗" in rendered
    assert "javascript:alert(1)" not in rendered
    assert "noopener noreferrer" in rendered
    assert 'id="selection-menu"' in rendered
    assert 'role="tablist" aria-label="阅读导航"' in rendered
    assert "const inReadingOrder = [...records].sort(" in rendered
    assert 'id="annotation-collapse"' in rendered
    assert "window.EnsembleReaderUI.tab('toc')" in rendered
    assert "content.className = 'annotation-locate'" in rendered
    assert 'white-space:nowrap; overflow:hidden; text-overflow:ellipsis; cursor:pointer' in rendered
    assert "openRecord(record.id, true)" in rendered
    assert "const editing = forceEdit || pendingNotes.has(id)" in rendered
    assert "setMode(editing)" in rendered
    assert 'id="annotation-save-html"' in rendered
    assert 'id="embedded-annotations">[]</script>' in rendered
    assert "copy.dataset.annotationKey = key + ':shared:'" in rendered
    assert "frame.removeAttribute('srcdoc'); frame.removeAttribute('src');" in rendered
    assert 'id="annotation-export"' not in rendered
    assert 'id="annotation-preview"' in rendered
    assert '可使用 $...$ 或 $$...$$ 写公式' in rendered
    assert "window.MathJax.typesetPromise([preview])" in rendered
    assert "preview.replaceChildren(notePreviewNodes(value))" in rendered
    assert "window.EnsembleReaderMarkdown.render(nodes, value)" in rendered
    assert 'id="qa-open"' in rendered
    assert 'id="annotation-ask-ai"' in rendered
    assert 'function selectedTextAcrossMath(block, range, allowOverlap)' in rendered
    assert 'textOutsideMath(block)' in rendered
    assert "请在公式之外至少选中一段普通文字" in rendered
    assert 'ENSEMBLE_QA_OPEN_SAVED' in rendered
    assert 'qaThreads' in rendered
    assert 'id="qa-frame"' in rendered
    assert 'sandbox="allow-scripts"' in rendered
    assert 'data-srcdoc=' in rendered
    assert 'qaFrame.srcdoc = qaSource' in rendered
    assert 'qaHistoryKey = key + \':qa-history-v1\'' in rendered
    assert "event.data.type === 'ENSEMBLE_QA_SAVE'" in rendered
    assert "event.data.type === 'ENSEMBLE_QA_DELETE'" in rendered
    assert "window.EnsembleReaderMarkdown.render(summary, value)" in rendered
    assert 'id="reader-entry-panel"' in rendered
    assert "window.EnsembleReaderMarkdown.render(entryBody, value)" in rendered
    assert "if (!record.note.trim() && !record.qaThreads?.length) continue" in rendered
    assert "() => deleteRecord(record.id)" in rendered
    assert "removeLegacyQaThreads(threadIds)" in rendered


def test_academic_html_normalizes_parenthesized_citation_runs_without_touching_source():
    source = "# 标题\n\n结论。（[5]；[6]；[7]）另一个判断 [8]。\n"
    rendered = render_academic_review_html(source, meeting_id="LR-TEST")
    assert "（[5]；[6]；[7]）" in source
    assert 'data-references="5,6,7"' in rendered
    assert "（[5]；[6]；[7]）" not in rendered
    assert 'data-references="8"' in rendered


def test_single_parenthesized_citation_is_normalized_without_touching_prose_or_links():
    source = "结论（[356]）；补充说明 ([357])。参见（[358]；[359]）。另见（[360]，[361]）。"
    assert normalize_reader_citation_groups(source) == (
        "结论[356]；补充说明 [357]。参见[358, 359]。另见[360, 361]。"
    )
    assert normalize_reader_citation_groups("解释（见 [1]）；[链接]([2])。") == (
        "解释（见 [1]）；[链接]([2])。"
    )
    rendered = render_academic_review_html("# 标题\n\n" + source, meeting_id="LR-TEST")
    assert "（[356]）" not in rendered.split("<main><article>", 1)[1].split("</article>", 1)[0]
    assert 'data-references="356"' in rendered


def test_academic_html_renders_reader_note_in_clickable_drawer_without_raw_definition():
    source = "# 标题\n\n判断仍限于平面体系。[^note-evidence-1]\n\n[^note-evidence-1]: 当前仅核对到摘要，曲面推广未验证。\n"
    rendered = render_academic_review_html(source, meeting_id="LR-TEST")
    assert 'class="note-trigger" data-note="note-evidence-1"' in rendered
    assert '<small>[注]</small>' in rendered
    assert '"note-evidence-1": "当前仅核对到摘要，曲面推广未验证。"' in rendered
    assert "[^note-evidence-1]:" not in rendered.split('<main><article>', 1)[1].split('</article>', 1)[0]


def test_pdf_reader_note_falls_back_to_numbered_endnote():
    source = "# 标题\n\n判断仍限于平面体系。[^note-evidence-1]\n\n[^note-evidence-1]: 仅核对到摘要。\n"
    assert _pdf_reader_note_fallback(source) == (
        "# 标题\n\n判断仍限于平面体系。〔注1〕\n\n## 注释\n\n1. 仅核对到摘要。\n"
    )


def test_html_auxiliary_callout_becomes_note_without_hiding_pdf_source():
    source = "# 标题\n\n结论只限平面体系。\n\n> **来源状态**：相关曲面研究仅见摘要。\n"
    rendered = render_academic_review_html(source, meeting_id="LR-TEST")
    assert 'data-note="note-callout-1"' in rendered
    assert '<small>[注]</small>' in rendered
    assert '来源状态：相关曲面研究仅见摘要。' in rendered
    assert _pdf_reader_note_fallback(source) == source


def test_html_qa_is_sandboxed_and_escapes_report_content():
    from html.parser import HTMLParser

    class IframeAttributes(HTMLParser):
        attributes = None
        def handle_starttag(self, tag, attrs):
            if tag == "iframe" and dict(attrs).get("id") == "qa-frame":
                self.attributes = dict(attrs)

    rendered = render_academic_review_html(
        "# 报告\n\n恶意文本 </script><script>alert('bad')</script> 与正常材料。\n",
        meeting_id="LR-TEST",
    )
    parser = IframeAttributes()
    parser.feed(rendered)
    frame = parser.attributes
    assert frame is not None
    assert frame["sandbox"] == "allow-scripts"
    assert "srcdoc" not in frame  # Load the isolated frame only after a Human opens it.
    source = frame["data-srcdoc"]
    assert "connect-src https://api.deepseek.com" in source
    assert "credentials: 'omit'" in source
    assert "redirect: 'error'" in source
    assert 'id="qa-save"' in source
    assert "type: 'ENSEMBLE_QA_SAVE'" in source
    assert "type: 'ENSEMBLE_QA_DELETE'" in source
    assert 'className = \'qa-history-delete\'' in source
    assert 'id="qa-new"' in source
    assert 'id="qa-clear"' in source
    assert 'renderMarkdown(a, turn.answer)' in source
    assert "window.MathJax.typesetPromise([conversationNode])" in source
    assert "turns.slice(-6)" in source
    assert "localStorage" not in source
    assert "sessionStorage" not in source
    assert "\\u003c/script\\u003e" in source
    assert "</script><script>alert('bad')" not in source
    assert "localStorage.setItem(key, JSON.stringify(records))" in rendered
    assert 'id="toc-panel" role="tabpanel"' in rendered
    assert rendered.index('id="annotation-list"') < rendered.index('</nav>')
    assert rendered.index('id="annotation-highlight"') > rendered.index('</nav>')
    assert 'target.scrollIntoView' in rendered
    assert "appendEntry(record.note, () => openNoteEntry(record)" in rendered
    assert 'id="annotation-close">收起 ×' in rendered
    assert "type: 'ENSEMBLE_QA_NEW_FOCUS'" in rendered
    assert 'selected_text: focusText' in source
    assert '<main><article>' in rendered
    assert 'position:fixed; z-index:10; display:flex' in rendered


def test_html_annotations_are_scoped_to_exact_report_content():
    first = render_academic_review_html("# 标题\n\n原文。\n", meeting_id="LR-TEST")
    same = render_academic_review_html("# 标题\n\n原文。\n", meeting_id="LR-TEST")
    revised = render_academic_review_html("# 标题\n\n修改后的原文。\n", meeting_id="LR-TEST")
    import re
    key = lambda text: re.search(r'data-annotation-key="([^"]+)"', text).group(1)
    assert key(first) == key(same)
    assert key(first) != key(revised)
    assert 'role="status" aria-live="polite"' in first


def test_academic_html_escapes_untrusted_markup_and_preserves_code():
    rendered = render_academic_review_html(
        "# Test\n\n<script>alert(1)</script>\n\n```tex\n$x$\n```\n",
        meeting_id="LR-TEST",
    )
    assert "&lt;script&gt;alert(1)&lt;/script&gt;" in rendered
    assert "<script>alert(1)</script>" not in rendered
    assert "<code class=\"language-tex\">$x$" in rendered


def test_academic_html_does_not_leave_dollars_around_display_math():
    source = (
        "# Formulae\n\n"
        "$$x^2+y^2=1$$\n\n"
        "$$\n\\mathcal N(i)=1\n$$\n\n"
        "The inline value $E=mc^2$ remains inline.\n\n"
        "```tex\n$$literal code$$\n```\n"
    )
    rendered = render_academic_review_html(source, meeting_id="LR-TEST")
    assert rendered.count('<div class="math display"') == 2
    assert '<span class="math inline"' in rendered
    assert '<p>$' not in rendered
    assert '<code class="language-tex">$$literal code$$' in rendered


def test_academic_html_repairs_duplicate_glossary_fences_and_inline_delimiters_in_display():
    from project_ensemble.orchestration.math_rendering import repair_nested_display_fences
    source = (
        "# 报告\n\n## 术语表\n\n"
        "- **幂距离**: 定义。\n\n"
        "$$\n$$\n" + r"\pi_i(x)=\|x-\(p_i\)\|^2-\(w_i\)." + "\n$$\n"
        + r"其中 \(p_i\) 是站点，\(w_i\) 是权重。" + "\n$$\n\n"
        "- **质量**: 定义。\n\n$$\n$$\n"
        + r"\(M_i\)(w)=\int_{\(V_i\)(w)}\rho(x)\,\mathrm{d}x," + "\n"
        + r"\qquad \sum_i \(M_i\)^*=\int_\Omega\rho(x)\,\mathrm{d}x." + "\n$$\n"
        + r"\(\Omega\) 是区域，\(M_i^*\) 是目标质量。" + "\n$$\n\n"
        "- **后续词条**: 仍应正常换行。\n"
    )
    repaired = repair_nested_display_fences(source)
    assert repaired.count("$$") == 4
    assert repair_nested_display_fences(repaired) == repaired
    rendered = render_academic_review_html(source, meeting_id="LR-TEST")
    article = rendered.split("<main><article>", 1)[1].split("</article>", 1)[0]
    assert article.count('<div class="math display"') == 2
    assert 'data-tex="\\pi_i(x)=\\|x-p_i\\|^2-w_i."' in article
    assert 'data-tex="M_i(w)=\\int_{V_i(w)}' in article
    assert r'\sum_i M_i^*' in article
    assert r'data-tex="\(' not in article
    assert "是目标质量。" in re.sub(r"<[^>]+>", "", article)
    assert '<p>$$' not in article
    assert 'data-term="后续词条">后续词条</button></strong>' in article


def test_glossary_formula_formatter_preserves_existing_math_and_explanation_boundaries():
    from project_ensemble.orchestration.math_rendering import glossary_formula_markdown
    formula = "$$\n" + r"V_i=\{x:\|x-p_i\|\leq\|x-p_j\|\}." + "\n$$\n" + r"其中 \(x\) 是位置。"
    assert glossary_formula_markdown(formula) == formula
    assert glossary_formula_markdown(r"\[x=y\]") == r"\[x=y\]"
    assert glossary_formula_markdown(r"\(x=y\)") == "$$\nx=y\n$$"
    assert glossary_formula_markdown(r"\(x=y\)，其中 \(x\) 是位置。") == r"\(x=y\)，其中 \(x\) 是位置。"
    assert glossary_formula_markdown(r"\gamma = \partial F/\partial L") == "$$\n" + r"\gamma = \partial F/\partial L" + "\n$$"


def test_duplicate_fence_repair_also_handles_expressions_without_an_equal_sign():
    from project_ensemble.orchestration.math_rendering import repair_nested_display_fences
    source = "$$\n$$\n" + r"\mathbb E_{\boldsymbol X\sim\mathcal N(\mu,\Sigma)}[\gamma(\boldsymbol X)]" + "\n$$\n" + r"\(\boldsymbol X\) 是随机向量。" + "\n$$\n"
    repaired = repair_nested_display_fences(source)
    assert repaired.count("$$") == 2
    assert r"\(\boldsymbol X\) 是随机向量。" in repaired


def test_display_fence_repair_preserves_valid_math_code_and_ordinary_prose():
    from project_ensemble.orchestration.math_rendering import repair_nested_display_fences
    source = (
        "# Report\n\n$$\nx=y\n$$\n\n"
        "The description stays outside.\n\n"
        "```tex\n$$\n$$\nx=y\n$$\n中文解释。\n$$\n```\n\n"
        + r"普通文字 \(x\) 不变。" + "\n\n" + r"\[a=b\]" + "\n"
    )
    assert repair_nested_display_fences(source) == source


def test_academic_html_does_not_swallow_glossary_after_nested_display_fences():
    source = (
        "# 报告\n\n## 术语表\n\n"
        "- **第一项**: 定义。\n\n"
        "$$\n$$x=1$$\n其中，x 是一个量。\n$$\n\n"
        "- **第二项**: 后续定义。\n\n"
        "$$\n$$y=2$$\n其中，y 是另一个量。\n$$\n\n"
        "- **第三项**: 仍应正常换行。\n"
    )
    rendered = render_academic_review_html(source, meeting_id="LR-TEST")
    article = rendered.split("<main><article>", 1)[1].split("</article>", 1)[0]
    assert article.count('<div class="math display"') == 2
    assert 'data-term="第二项">第二项</button></strong>' in article
    assert 'data-term="第三项">第三项</button></strong>' in article
    assert "其中，x 是一个量。" in article
    assert "- **第二项**" not in article


def test_academic_html_emphasizes_module_headings_abstract_and_bold():
    source = (
        "# 研究标题\n\n## 摘要\n\n**关键判断**在这里。\n\n"
        "## 分模块调研结果\n\n### 1. 第一项研究问题\n\n正文包含**重要条件**。\n\n"
        "### 2. 第二项研究问题\n\n后续内容。\n"
    )
    rendered = render_academic_review_html(source, meeting_id="LR-TEST")
    assert '<section class="abstract-section">' in rendered
    assert '<section class="report-section module-section">' in rendered
    assert rendered.count('class="module-heading"') == 2
    assert '<strong>关键判断</strong>' in rendered
    assert '<strong>重要条件</strong>' in rendered
    assert 'article strong, article b' in rendered


def test_academic_html_numbers_nested_sections_and_styles_deep_headings_readably():
    source = (
        "# 报告标题\n\n## 分模块调研结果\n\n"
        "### 5. 粗粒化与重整化群\n\n"
        "#### 第一层小节\n\n##### 本章要回答的问题，以及两个必须先固定的术语\n\n"
        "正文。\n\n#### 第二层小节\n\n正文。\n"
    )
    rendered = render_academic_review_html(source, meeting_id="LR-TEST")
    article = rendered.split("<main><article>", 1)[1].split("</article>", 1)[0]

    assert "<h3 id=\"section-3\" class=\"module-heading\">5. 粗粒化与重整化群</h3>" in article
    assert "<h4 id=\"section-4\">5.1 第一层小节</h4>" in article
    assert "<h5 id=\"section-5\">5.1.1 本章要回答的问题，以及两个必须先固定的术语</h5>" in article
    assert "<h4 id=\"section-6\">5.2 第二层小节</h4>" in article
    assert "h5 { margin-top:1.55em; font-size:1.16rem; }" in rendered
    assert "h6 { margin-top:1.4em; font-size:1.05rem; }" in rendered


def test_academic_html_recognizes_bold_sentence_adjacent_to_chinese_prose():
    source = (
        "# 标题\n\n**有边界的判断。**紧接着的正文不应吞掉加粗。\n\n"
        "**中文结论。**Lennard-Jones 后接英文也要加粗。\n\n"
        "`**代码。**原样` 和 $x$ 仍应保持各自含义。\n"
    )
    rendered = render_academic_review_html(source, meeting_id="LR-TEST")
    article = rendered.split("<main><article>", 1)[1].split("</article>", 1)[0]
    assert "<strong>有边界的判断。</strong>" in article
    assert "<strong>中文结论。</strong>" in article
    assert "**有边界的判断。**" not in article
    assert '<code>**代码。**原样</code>' in article
    assert '<span class="math inline"' in article


def test_academic_pdf_keeps_three_column_comparison_as_table(monkeypatch):
    from project_ensemble.orchestration import academic_pdf

    observed = []
    original = academic_pdf._table_flowables

    def record(rows, styles, markup, palette_colors):
        observed.append([row[:] for row in rows])
        return original(rows, styles, markup, palette_colors)

    monkeypatch.setattr(academic_pdf, "_table_flowables", record)
    source = (
        "# 研究报告\n\n## 摘要\n\n摘要独立成框。\n\n"
        "## 分模块调研结果\n\n### 1. 第一章\n\n#### 对照结果\n\n"
        "| 同一对照字段 | 现有证据支持的判断 | 尚不能判定的事项 |\n"
        "|---|---|---|\n| 效价 | 已核对方法。 | 产品限度待核。 |\n"
    )
    pdf, _ = render_academic_review_pdf(source, meeting_id="LR-TEST")
    reader = PdfReader(io.BytesIO(pdf))
    assert len(reader.pages) >= 4  # cover, TOC, abstract, numbered chapter
    assert len(reader.outline) >= 1
    assert len(observed) == 1 and len(observed[0]) == 2
    assert observed[0][0] == ["同一对照字段", "现有证据支持的判断", "尚不能判定的事项"]


def test_bundled_harmony_font_is_the_default_and_has_its_license(monkeypatch):
    monkeypatch.delenv("ENSEMBLE_CJK_FONT", raising=False)
    font = resolve_cjk_font()
    assert font.name == "HarmonyOS_Sans_SC_Regular.ttf"
    assert font.with_name("HarmonyOS_Sans_SC_Medium.ttf").is_file()
    assert font.with_name("HarmonyOS_Sans_SC_Bold.ttf").is_file()
    assert font.with_name("LICENSE.txt").is_file()


def test_academic_pdf_contents_preserves_chinese_latin_and_page_numbers(monkeypatch):
    monkeypatch.delenv("ENSEMBLE_CJK_FONT", raising=False)
    monkeypatch.delenv("ENSEMBLE_CJK_HEADING_FONT", raising=False)
    source = (
        "# 二维线张力综述\n\n## 分模块调研结果\n\n"
        "### 1. Delaunay 与界面归属\n\n正文。\n"
    )
    pdf, _ = render_academic_review_pdf(source, meeting_id="LR-TEST")
    reader = PdfReader(io.BytesIO(pdf))
    contents = reader.pages[1].extract_text()
    assert "目录" in contents
    assert "1. Delaunay 与界面归属" in contents
    assert "3" in contents
    assert "\x00" not in contents
    embedded_fonts = " ".join(
        str(ref.get_object().get("/BaseFont", ""))
        for page in reader.pages
        for ref in page["/Resources"].get("/Font", {}).values()
    )
    assert "HarmonyOS_Sans_SC_Bold" in embedded_fonts
    assert "HarmonyOS_Sans_SC" in embedded_fonts


def test_math_is_preserved_in_latex_and_rendered_in_academic_pdf():
    source = (
        "# 公式测试\n\n## 方法\n\n"
        r"面积 $A=L_x L_y$，压力为 \(P=F/A\)。" "\n\n"
        "$$\n" r"P=\frac{F}{A}" "\n$$\n\n"
        r"非公式字符 50% 和 a_b 仍须转义。" "\n"
    )
    latex = markdown_to_latex(source)
    assert r"\usepackage{amsmath,amssymb}" in latex
    assert r"$A=L_x L_y$" in latex
    assert r"\(P=F/A\)" in latex
    assert "\\[\nP=\\frac{F}{A}\n\\]" in latex
    assert r"50\%" in latex and r"a\_b" in latex
    pdf, _ = render_academic_review_pdf(source, meeting_id="LR-TEST")
    assert sum(len(page.images) for page in PdfReader(io.BytesIO(pdf)).pages) == 0


def test_fragmented_equation_is_one_formula_without_changing_symbols():
    source = (
        "# 数学测试\n\n## 方法\n\n"
        r"面积 A=\(A^α\)+\(A^β\)，且 \(N_i^s\)≡\(N_i\)−\(ρ_i^αA^α\)。"
    )
    normalized = normalize_fragmented_inline_math(source)
    assert r"\(A=A^α+A^β\)" in normalized
    assert r"\(N_i^s≡N_i−ρ_i^αA^α\)" in normalized
    pdf, _ = render_academic_review_pdf(source, meeting_id="LR-TEST")
    assert sum(len(page.images) for page in PdfReader(io.BytesIO(pdf)).pages) == 0


def test_bare_integrals_and_greek_symbols_are_vector_math_not_prose():
    source = (
        "# 线张力\n\n## 方法\n\n"
        "在 α／β 相界，候选式为 γ_mech=(1/n)∫_盒宽dx[⟨P_xx(x)⟩−⟨P_yy(x)⟩]。\n\n"
        "令 h_q≡L^−1∫_0^Lh(y)e^(−iqy)dy；这是 Fourier 归一。\n"
    )
    normalized = normalize_fragmented_inline_math(source)
    assert r"\(α\)／\(β\)" in normalized
    assert r"\(γ_mech=(1/n)∫_盒宽dx[⟨P_xx(x)⟩−⟨P_yy(x)⟩]\)" in normalized
    assert r"\(h_q≡L^−1∫_0^Lh(y)e^(−iqy)dy\)" in normalized
    pdf, _ = render_academic_review_pdf(source, meeting_id="LR-TEST")
    reader = PdfReader(io.BytesIO(pdf))
    extracted = "\n".join(page.extract_text() or "" for page in reader.pages)
    assert "盒宽" in "".join(extracted.split()) and "∫" in extracted
    assert sum(len(page.images) for page in reader.pages) == 0


def test_integral_equation_with_delimited_variables_is_rejoined():
    source = (
        "# 线张力\n\n## 几何长度\n\n"
        r"再取 ℓ(\(q_c\))=∫₀ᴸ√{1+[∂ₓh_{\(q_c\)}(x)]²}dx；过滤尺度另行说明。"
    )
    normalized = normalize_fragmented_inline_math(source)
    assert r"\(ℓ(q_c)=∫₀ᴸ√{1+[∂ₓh_{q_c}(x)]²}dx\)" in normalized
    assert normalize_fragmented_inline_math(normalized) == normalized
    pdf, _ = render_academic_review_pdf(source, meeting_id="LR-TEST")
    assert pdf.startswith(b"%PDF-")
    assert sum(len(page.images) for page in PdfReader(io.BytesIO(pdf)).pages) == 0


def test_math_repair_does_not_nest_greek_in_larger_expressions():
    source = "A=A^α+A^β；Δ𝒢_R 与 Ω^s。h_q＝L⁻¹∫₀ᴸ[h(x)−h̄]e^(−iqx)dx，这里的长波主项为"
    normalized = normalize_fragmented_inline_math(source)
    assert r"\(A=A^α+A^β\)" in normalized
    assert r"\(Δ𝒢_R\)" in normalized
    assert r"\(Ω^s\)" in normalized
    assert r"\(h_q＝L⁻¹∫₀ᴸ[h(x)−h̄]e^(−iqx)dx\)" in normalized
    assert "这里的长波主项为" in normalized
    assert r"\(\(" not in normalized


def test_math_repair_balances_limit_and_contour_subscripts():
    source = (
        r"\(γ_∞\)≡lim_{\(L_Σ\)→∞}\(Ω^s\)/\(L_Σ\)；"
        r"\(ℓ_𝒪\)(X)=∫_{\(𝒞_𝒪\)(X)}ds。"
    )
    normalized = normalize_fragmented_inline_math(source)
    assert r"\(γ_∞≡lim_{L_Σ→∞}Ω^s/L_Σ\)" in normalized
    assert r"\(ℓ_𝒪(X)=∫_{𝒞_𝒪(X)}ds\)" in normalized
    assert normalize_fragmented_inline_math(normalized) == normalized


def test_math_typography_groups_word_subscripts_and_stacks_clear_fractions():
    source = (
        r"能量 \(U_pot=E_kin/N\)，极限 \(γ_∞=lim_{L_Σ→∞}Ω^s/L_Σ\)。"
    )
    normalized = normalize_math_operator_commands(source)
    assert r"U_{\mathrm{pot}}=\frac{E_{\mathrm{kin}}}{N}" in normalized
    assert r"\lim_{L_Σ→∞}\frac{Ω^s}{L_Σ}" in normalized
    assert normalize_math_operator_commands(normalized) == normalized


def test_math_typography_leaves_prose_code_and_ambiguous_ratios_alone():
    source = (
        "正文 lim_x 与 U_pot。\n\n"
        "```text\nlim_x U_pot=A/B\n```\n\n"
        r"\(A/B+C\) 与 \(k_BTΣ_iN_i^s\)。"
    )
    normalized = normalize_math_operator_commands(source)
    assert "正文 lim_x 与 U_pot。" in normalized
    assert "```text\nlim_x U_pot=A/B\n```" in normalized
    assert r"\(A/B+C\)" in normalized
    assert r"\(k_BTΣ_iN_i^s\)" in normalized


def test_fragmented_polynomial_superscript_stays_in_displayed_formula(monkeypatch):
    from project_ensemble.orchestration.math_rendering import PdfMathRenderer

    displayed = []
    original = PdfMathRenderer.display_flowables

    def record(self, formula, **kwargs):
        displayed.append(formula)
        return original(self, formula, **kwargs)

    monkeypatch.setattr(PdfMathRenderer, "display_flowables", record)
    source = (
        "# 张力综述\n\n## 曲率效应\n\n"
        r"在固定半径定义和约束后，\(γ_{\mathrm{app}}(R)=γ_∞+a_1/R+a_2/R\)²+⋯ 只是待验证参数化。"
    )
    normalized = normalize_math_operator_commands(normalize_fragmented_inline_math(source))
    expected = r"γ_{\mathrm{app}}(R)=γ_∞+\frac{a_1}{R}+\frac{a_2}{R^{2}}+\cdots"
    assert rf"\({expected}\)" in normalized
    pdf, _ = render_academic_review_pdf(source, meeting_id="LR-TEST")
    assert displayed == [expected]
    assert pdf.startswith(b"%PDF-")


def test_trailing_superscript_keeps_original_scope_for_non_polynomial():
    source = r"\(A+B\)² 与 \(γ_3D^*≡γ_3Dσ\)²/ε。还有 \(k_BT/(κLq\)²)。"
    normalized = normalize_fragmented_inline_math(source)
    assert r"\((A+B)^{2}\)" in normalized
    assert r"\(γ_3D^*≡γ_3Dσ^{2}/ε\)" in normalized
    assert r"\(k_BT/(κLq^{2})\)" in normalized


def test_complete_equations_are_displayed_including_short_conditions(monkeypatch):
    from project_ensemble.orchestration.math_rendering import PdfMathRenderer

    displayed = []
    original = PdfMathRenderer.display_flowables

    def record(self, formula, **kwargs):
        displayed.append(formula)
        return original(self, formula, **kwargs)

    monkeypatch.setattr(PdfMathRenderer, "display_flowables", record)
    source = (
        "# 数学测试\n\n## 方法\n\n"
        r"设面积为 \(A=A^α+A^β\)，取 \(q=0\) 时再计算 "
        r"\(γ_∞≡lim_{L_Σ→∞}Ω^s/L_Σ\)。"
    )
    render_academic_review_pdf(source, meeting_id="LR-TEST")
    assert displayed == ["A=A^α+A^β", "q=0", r"γ_∞≡\lim_{L_Σ→∞}\frac{Ω^s}{L_Σ}"]


def test_incomplete_formula_fragment_is_not_promoted():
    assert not should_display_inline_equation("ΔΓ_i=−(ρ_i^α−ρ_i^β", "得到 ", ")δ。")
    assert not should_display_inline_equation("Γ_i=N_i^s", "定义 ", "／界面长度。")


def test_fullwidth_equality_in_formula_uses_supported_math_glyph():
    source = "# 数学测试\n\n## 方法\n\n" + r"\(P_i\)＝\(F_i\)/A。"
    assert r"\(P_i=F_i/A\)" in normalize_fragmented_inline_math(source)
    pdf, _ = render_academic_review_pdf(source, meeting_id="LR-TEST")
    assert sum(len(page.images) for page in PdfReader(io.BytesIO(pdf)).pages) == 0


def test_math_repair_does_not_touch_code_display_or_references():
    source = (
        "# 标题\n\n## 方法\n\n"
        "```text\nP_i=F_i/A\n```\n\n"
        "$$\nP_i=F_i/A\n$$\n\n"
        "## 参考文献\n\n[1] a_b。\n"
    )
    normalized = normalize_fragmented_inline_math(source)
    assert "```text\nP_i=F_i/A\n```" in normalized
    assert "$$\nP_i=F_i/A\n$$" in normalized
    assert "[1] a_b。" in normalized


def test_unclosed_display_formula_is_reported_explicitly():
    with pytest.raises(ValueError, match="LATEX_MATH_RENDERING_FAILED"):
        markdown_to_latex("# 测试\n\n$$\nx^2\n")
    with pytest.raises(ValueError, match="PDF_MATH_RENDERING_FAILED"):
        render_academic_review_pdf("# 测试\n\n$$\nx^2\n", meeting_id="LR-TEST")


def test_multiline_aligned_equations_render_as_two_readable_rows():
    source = (
        "# 公式测试\n\n## 方法\n\n$$\n"
        r"\begin{aligned} P &= F/A \\ E &= mc^2 \end{aligned}"
        "\n$$\n"
    )
    pdf, _ = render_academic_review_pdf(source, meeting_id="LR-TEST")
    assert sum(len(page.images) for page in PdfReader(io.BytesIO(pdf)).pages) == 0
    assert r"\begin{aligned}" in markdown_to_latex(source)


def test_pdf_math_accepts_unbraced_single_letter_font_commands():
    source = (
        "# 公式测试\n\n## 邻域定义\n\n"
        r"令 \(\mathcal N(i)\) 为粒子邻域，\(\mathbb E[X]\) 为期望。"
        "\n\n$$\n"
        r"\mathbf r_i \in \mathcal N(i),\quad \mathbb E[X] = 1"
        "\n$$\n"
    )
    pdf, _ = render_academic_review_pdf(source, meeting_id="LR-TEST")
    assert sum(len(page.images) for page in PdfReader(io.BytesIO(pdf)).pages) == 0


def test_pdf_math_accepts_explicit_tex_delimiter_sizes():
    source = (
        "# 公式测试\n\n## 自由能\n\n$$\n"
        r"\Delta F=-k_{\mathrm B}T\ln\left\langle "
        r"J_{\Phi}(x)\exp\!\left[-\beta\bigl(U_1(\Phi x)-U_0(x)\bigr)\right]"
        r"\right\rangle_{0}"
        "\n$$\n"
    )
    pdf, _ = render_academic_review_pdf(source, meeting_id="LR-TEST")
    assert sum(len(page.images) for page in PdfReader(io.BytesIO(pdf)).pages) == 0


def test_pdf_math_accepts_two_row_vectors_and_matrices():
    source = (
        "# 公式测试\n\n## 矩阵\n\n$$\n"
        r"\mathbf n_e=\begin{pmatrix}-t_y\\t_x\end{pmatrix},\qquad "
        r"\mathbf M=\begin{pmatrix}e^x&0\\0&e^{-x}\end{pmatrix}"
        "\n$$\n"
    )
    pdf, _ = render_academic_review_pdf(source, meeting_id="LR-TEST")
    assert sum(len(page.images) for page in PdfReader(io.BytesIO(pdf)).pages) == 0


def test_pdf_math_accepts_common_tex_aliases_and_bold_symbols():
    source = (
        "# 公式测试\n\n## 误差长度\n\n$$\n"
        r"\mathbb E L_N\ge N\,\mathbb E\left\|"
        r"\boldsymbol\varepsilon_2-\boldsymbol\varepsilon_1\right\|-L_{\gamma}"
        "\n$$\n"
    )
    pdf, _ = render_academic_review_pdf(source, meeting_id="LR-TEST")
    assert sum(len(page.images) for page in PdfReader(io.BytesIO(pdf)).pages) == 0
