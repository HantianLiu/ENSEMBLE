"""Reader-facing writing controls; these never lower evidence requirements."""

from __future__ import annotations

import json

from project_ensemble.orchestration.literature_style import READER_PROSE_LEXICON_RULES


PROFICIENCY_ANCHORS = {
    1: "该学科完全没有先验知识；首次使用关键概念时解释对象与用途。",
    2: "读过一些大众科普；解释专业术语、关键背景和非直观方法。",
    3: "相关专业本科生水平；解释进阶方法、本文特有口径与跨学科借入概念。",
    4: "相关专业新近毕业生水平，能阅读论文；解释非标准构造、本文自定义量和容易误解的口径。",
    5: "相关专业长期学术工作者；常规术语可直接使用，但本文自定义量和非标准方法仍须解释。",
}

SIGNPOSTING_ANCHORS = {
    1: "只在论证方向转换时给必要提示。",
    2: "明确重要比较对象与转折，不额外重复结论。",
    3: "在多步论证中标明当前问题、下一步及结论的作用。",
    4: "主动交代比较维度、为何转向下一步、证据与推论的连接；复杂处增加导航。",
    5: "对复杂论证逐层标明问题、条件变化、推论层次与未解决处，但不制造标题碎片。",
}

SEGMENTATION_ANCHORS = {
    1: "优先保持连续论证，仅在主题改变时分段。",
    2: "按中心判断分段，避免将一条推导切碎。",
    3: "按信息功能分段；比较维度改变时添加有意义的标题。",
    4: "积极区分判断、关键依据与重要边界，但保留连续机制链。",
    5: "允许较短段落和更多导航；标题、列表仍须承担真实信息功能。",
}

LIVELINESS_ANCHORS = {
    1: "严肃、克制，不使用比喻。",
    2: "自然完整的技术句法，少量过渡。",
    3: "流畅而专业，允许简短说明性例子。",
    4: "适当使用贴切比喻，但紧接准确的定义和边界。",
    5: "较鲜活的讲述方式；比喻不能替代公式、证据或限定。",
}


def _level(value: object, default: int = 4) -> int:
    try:
        level = int(value)
    except (TypeError, ValueError):
        return default
    return level if 1 <= level <= 5 else default


def reader_style_policy(preferences: dict | None, disciplines: dict | None = None) -> dict:
    preferences = preferences or {}
    signposting = _level(preferences.get("signposting_1_to_5"))
    segmentation = _level(preferences.get("segmentation_1_to_5"), 3)
    liveliness = _level(preferences.get("liveliness_1_to_5"), 2)
    return {
        "argument_signposting": {
            "ordinary_level": signposting,
            "ordinary_instruction": SIGNPOSTING_ANCHORS[signposting],
            "complex_level": min(signposting + 1, 5),
            "complex_instruction": SIGNPOSTING_ANCHORS[min(signposting + 1, 5)],
        },
        "segmentation": {"level": segmentation, "instruction": SEGMENTATION_ANCHORS[segmentation]},
        "liveliness": {"level": liveliness, "instruction": LIVELINESS_ANCHORS[liveliness]},
        "discipline_proficiency": {
            name: {"level": _level(level), "instruction": PROFICIENCY_ANCHORS[_level(level)]}
            for name, level in (disciplines or {}).items()
        },
        "invariants": "路标只显露原有逻辑，不得新增事实、机制或改变论断强度；专业度不影响证据标准。",
    }


def reader_facing_prose_contract(
    preferences: dict | None = None, disciplines: dict | None = None,
) -> str:
    """Shared instructions for text that will appear in the reader publication.

    Intermediate plans, evidence records, votes, and audit notes must not use
    this contract. It changes presentation, never the authority of a stage.
    """

    style = reader_style_policy(preferences, disciplines)
    return (
        "\n\n读者正文写作契约（只约束本次获准生成或修改的读者可见文字）："
        + READER_PROSE_LEXICON_RULES
        + "先让读者找到本段的中心判断，再写关键依据、必要解释与真正改变结论的边界；"
        "一个段落原则上只推进一个主要判断。用完整的主谓、介词和连接关系展开压缩名词链，"
        "只显明原有逻辑，不补造机制、事实或因果。"
        "正文优先呈现科学主线；次要实现条件、仅摘要可读等来源状态，可另写单行"
        " > **实现说明**：或 > **来源状态**：，由 HTML 显示为小号[注]侧栏。"
        "改变结论适用范围或证据强度的限制仍须在正文可见，不能藏进注释。"
        "第一次完整说明重要证据边界；后文没有新边界时简短指回，避免反复强调资料经过何种审查，"
        "或使用‘本章不能据此宣布’‘尚不足以证明’等审计腔。直接说明来源实际报告了什么，以及哪些问题仍未解决。"
        "压缩重复，不压缩异议或不确定性；不要让每段重新成为独立的审计记录。"
        "引文紧贴它实际支持的具体主张；不得移动引文后改变对应关系。"
        "保留已核验事实、作者推论、建议与待验证假设之间的区别。"
        "数值、单位、对象、样本、条件、时间窗、公式、变量定义、因果方向和结论强度均不得擅改；"
        "‘没有证据证明’不能改成‘不成立’，‘一致’不能改成‘证明’。"
        "公式前后说明所求对象、新符号、关键假设变化以及结果的论证作用。"
        "术语解释按各学科读者熟悉度决定：本文自定义量、非标准方法、跨学科借入术语和"
        "容易误解的操作口径须在首次承担论证作用时解释；高频易懂词不必凑入术语表。"
        "标题、列表和表格按信息功能使用，不制造标题碎片或把连续推导切碎。"
        "如果当前任务只是局部修订，只在明确授权的位置实施这些表达规则；"
        "不得以改善可读性为由扩张修订范围、重做研究或改写冻结内容。"
        "读者风格参数（文字锚点优先于数字；复杂论证采用更积极的一档路标）："
        + json.dumps(style, ensure_ascii=False, separators=(",", ":"))
    )


def structured_result_prose_contract(
    preferences: dict | None = None,
    disciplines: dict | None = None,
    *,
    planning_scope: bool = False,
) -> str:
    """Style only the natural-language values inside structured model output."""
    style = reader_style_policy(preferences, disciplines)
    contract = (
        "\n\n结构化结果的自由文本表达规则（只约束自然语言字段，不改变本阶段任务权限）："
        "写给研究者看，不要复述提示词、字段名、内部状态或工作流程，也不要把内部分类直接当成科学结论。"
        "自然语言字段不得带出 RM-XX、RP-...、C00007-XXXX、Project ENSEMBLE、LR-... 等内部标识；"
        "来源编号或模块编号若为 schema 所需，只放在指定的编号字段或引文位置。不要把‘发现卡’‘有界材料’、"
        "‘证据成熟度’‘核验层级’‘证据性质四分法’‘待验证假设’或‘[来源待核]’写成读者标签。"
        "避免‘题名级’‘冻结目录’‘核对条目’‘登记缺口’‘接口交付’‘记账’等内部或生硬说法；"
        "不要创造记账隐喻、审计标签或复杂复合名词。必要时把名词串拆成主谓完整的短句，说明谁做了什么、"
        "比较什么以及条件如何起作用。专业固定术语可以保留，但首次影响理解时按读者背景解释。"
        "主要比较或论点转换处给出明确路标；推理格外复杂时再多给一档路标。句长和段落长度随内容变化，"
        "不套固定句数或字数模板。不要增加科学事实、机制、因果或结论强度；保持对象、范围、条件、"
        "来源对应关系与不确定性。"
        "本规则只管解释性文字；检索式、来源标识、引文、公式、数值、专名、枚举值、状态代码、"
        "原文摘录及 schema 要求的机器字段必须保持准确，不要改写成自然语言。"
        "表达档位（以文字锚点为准）："
        + json.dumps(style, ensure_ascii=False, separators=(",", ":"))
    )
    if planning_scope:
        contract += (
            "\n规划权限边界：只规划研究内容——研究问题、主题范围、模块关系、所需的学术证据类型和"
            "有意义的比较。可以提出学科检索词或主题检索式；不得决定联网能力、检索后端、供应商、"
            "模型调用方式、费用控制、通用自动质量控制、审计程序或报告出版流程，也不要把这些系统职责"
            "改写成研究模块、子问题或留给下一阶段执行的要求。可提出回答本题必需的学术方法与证据比较，"
            "但不要把它升级为适用于系统或整篇报告的审查规则。用户明确提出的科学内容硬约束必须保留；"
            "若某项流程本身就是研究对象，才按其科学含义讨论。"
        )
    return contract


def structured_prose_context_from_repo(repo) -> tuple[dict, dict]:
    """Load meeting-local style inputs without changing frozen preferences."""
    root = repo.root / "public/literature_report"
    preferences_path = root / "writing_preferences.json"
    preferences = json.loads(preferences_path.read_text(encoding="utf-8")) if preferences_path.is_file() else {}
    profile_paths = sorted(root.glob("audience_profile-*.json"))
    profile = json.loads(profile_paths[-1].read_text(encoding="utf-8")) if profile_paths else {}
    disciplines = profile.get("disciplines", {})
    return preferences, disciplines if isinstance(disciplines, dict) else {}
