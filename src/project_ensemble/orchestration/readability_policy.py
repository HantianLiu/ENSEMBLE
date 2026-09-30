"""Reader-facing writing controls; these never lower evidence requirements."""

from __future__ import annotations

import json


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
        "先让读者找到本段的中心判断，再写关键依据、必要解释与真正改变结论的边界；"
        "一个段落原则上只推进一个主要判断。用完整的主谓、介词和连接关系展开压缩名词链，"
        "只显明原有逻辑，不补造机制、事实或因果。"
        "正文优先呈现科学主线；次要实现条件、仅摘要可读等来源状态，可另写单行"
        " > **实现说明**：或 > **来源状态**：，由 HTML 显示为小号[注]侧栏。"
        "改变结论适用范围或证据强度的限制仍须在正文可见，不能藏进注释。"
        "第一次完整说明重要证据边界；后文没有新边界时简短指回，避免反复使用"
        "‘现有可核对材料’‘本章不能据此宣布’‘尚不足以证明’等审计腔。"
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
