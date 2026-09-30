"""Shared reader-facing writing contract for literature-report tasks.

This is a task instruction, not a Representative persona.  The checks below
identify objective presentation defects; scholarly judgments remain with the
ordinary review process.
"""

from __future__ import annotations

import re


LITERATURE_WRITING_RULES = (
    "写作任务规则（适用于初稿、整合与修订；不改变各阶段权限）："
    "不要把文献调研写成法规、实施细则、职责清单或审批流程；不要机械套用固定章节结构。"
    "不要让会议内部的 RM/RD/SR/INF 等模块号、证据包号、record_id、知识状态代码或表决过程进入读者正文；"
    "如需交叉引用，改写为读者可理解的章节名称，内部映射留在审计材料。"
    "涉及过程或机制时，写清对象、条件、动作与结果；不要用连续抽象名词替代关系。"
    "全文中用于解释结果或比较文献的物理量及其他定量指标，首次实质使用时必须给出"
    "来源支持的数学定义或可复现的操作性定义：说明符号、公式中各项含义、"
    "量纲及单位或约化单位，并区分"
    "输入控制参数、实测量和推导估计量。说明相关的测量/计算方法、时间或空间平均口径、"
    "采样窗口与适用条件；不同研究对同名量定义不同时，并列其定义及可比条件。"
    "例如讨论非高斯热浴时，相关的阻尼系数、噪声强度与分布、高阶矩或非高斯指标、"
    "相关时间、热浴设定温度和实测动能温度不能仅以形容词代替定义，也不得混为一量。"
    "只展开支撑论证的关键量，不堆砌公式；来源未给出或尚未核实的定义、数值与单位"
    "应明确标为待核实，不得凭模型记忆补造。"
    "单个数学变量及短条件可用行内定界符 \\( ... \\)；完整等式、不等式、极限、积分和推导关系"
    "应单独成行，使用 $$ ... $$，前后以正文说明其意义与适用条件。"
    "不要把同一公式拆成多个行内片段，也不要把 P_i、k_BT、N_i^s 或等式直接当作普通文本输出。"
    "JSON 中的 LaTeX 反斜杠必须双重转义，否则 \\beta、\\rho 等会被解码成控制字符。"
    "相邻句子的因果、条件、转折、补充或证据关系应清楚，但不要机械插入套话。"
    "不要用‘研究表明’‘学界普遍认为’等空泛断语代替具体证据，也不要把单项研究外推为领域共识。"
    "结论仅适用于特定对象、方法或条件时，在结论附近写明边界。"
    "不要把‘本次检索未找到’写成‘现象不存在’或‘方法无效’。"
    "直接陈述已有证据支持的部分；把不确定性限定到具体命题与缺少的证据；术语前后一致。"
    "以连贯段落为默认形式，只有真正并列的数据、比较或步骤才使用列表或表格。"
    "标题仅写语义名称，不手写中文或阿拉伯数字层级编号；出版时统一编号和制作目录。"
    "若本块位于章节开头且确需导读，使用 > **章节导读** 引用块；"
    "章节中间块不要重复导读，不把摘要、导言或概述写成独立的 ## 标题。"
    "同段内连续写（1）（2）等并列事项时，改用 1.、2. 的 Markdown 有序列表，"
    "不要把单一论证拆成列表。数字文献引文在同一方括号内按升序列出，来源不遗漏。"
    "引文编号是出处标记，不是研究的名称或句子的主语；不要写‘[25] 是……’。"
    "先用可辨认的研究对象、作者或方法说明谁做了什么，再把编号紧贴其支持的具体陈述；"
    "完整题名与链接放在参考文献表。"
    "连续编号仅在其中每一条文献确实支持同一论断时合写为 [A-B]；不得为凑区间加入无关文献。"
    "表格以四列以内为宜；五列及以上须保留每一字段、单位与数值，出版时可转为字段卡。"
    "可以说明有证据支持的建议，但不能把建议伪装成已证实的事实。"
)


LITERATURE_MODULE_WRITING_RULES = (
    "模块综述写作目标：依据已核实的材料回答本模块的研究问题，不把正文写成检索日志、"
    "文件目录、法规清单或会议文书。开头简要说明现有证据允许的主要发现及其边界，"
    "不要只预告本章将讨论什么。围绕研究问题、研究对象、方法或证据分歧组织段落；"
    "跨体系、地区或研究路线比较时，先确认对象、证据层级和评价口径具有可比性，再并列比较。"
    "每段尽量先写具体对象和发现，再紧贴相应论断给出本章 C 引文，随后交代适用条件或分歧；"
    "避免连续罗列文件编号、检索入口和未取得的材料，让读者自行推导结论。"
    "准确说明哪个命题仍不确定、缺少哪类原始证据；共同的检索限制集中写在证据边界，"
    "只有直接改变某项结论的限制才在相应段落重复。资料不足时如实留空，"
    "不为达到篇幅目标补造国别比较，也不要让缺口说明占据大部分正文。"
    "涉及多个不同主题时，用简短、有信息量的章内标题帮助阅读，不机械套固定模板。"
    "首次使用可能妨碍跨学科读者理解的缩写时解释含义；不要重复前章已经说明的背景。"
    "本章临时 C 引文必须指向冻结文献目录中支持相邻陈述的来源；"
    "内部 RP 证据包字段由程序根据 C 引文映射校正，不要为填满该字段增添无关正文引文。"
)


LITERATURE_CHAIR_INTEGRATION_RULES = (
    "整合时优先消除重复的范围声明和检索过程叙述，统一章内标题、段落顺序与术语；"
    "把主要发现放在读者容易找到的位置。只在不改变证据含义的范围内改善行文，"
    "不得新增事实、把资料缺口改写成比较结论，或删除影响结论的限制条件。"
)


_INTERNAL_IDENTIFIER = re.compile(
    r"(?i:\b(?:RM[-‐‑‒–—−_ ](?:\d{1,3}|xx)|SR-\d{3}|RD[-_ ]?\d{2,4}|RP-[A-Z0-9]+|INF-\d+|ASM-\d+|record_id|Evidence Packet)\b)"
    r"|\b(?:MODEL_PRIOR|SOURCE_BACKED|INFERENCE|ASSUMPTION|UNRESOLVED|QUALIFIED)\b",
)

_READER_MODULE_ID = re.compile(r"(?i:\bRM[-‐‑‒–—−_ ](?P<number>\d{1,3}|xx)\b)")


def replace_reader_module_ids(
    markdown: str, module_chapters: dict[str, int], *, language: str = "zh",
) -> tuple[str, list[dict[str, str]]]:
    """Replace internal module handles with reader-facing chapter references.

    Unknown or placeholder IDs become a neutral section reference, never a
    guessed chapter number. The caller retains the replacement trace privately.
    """

    replacements: list[dict[str, str]] = []
    generic = {"zh": "相关研究章节", "en": "the relevant research section",
               "fr": "la section de recherche concernée"}.get(language, "相关研究章节")

    def substitute(match: re.Match[str]) -> str:
        raw = match.group(0)
        number = match.group("number")
        chapter = module_chapters.get(f"RM-{int(number):02d}") if number.isdigit() else None
        if chapter is None:
            replacement = generic
        else:
            replacement = {
                "zh": f"第 {chapter} 章", "en": f"Chapter {chapter}",
                "fr": f"chapitre {chapter}",
            }.get(language, f"第 {chapter} 章")
        replacements.append({"original": raw, "replacement": replacement})
        return replacement

    return _READER_MODULE_ID.sub(substitute, markdown), replacements


def leaked_internal_identifiers(text: str) -> list[str]:
    """Return presentation leaks, not a scientific-validity verdict."""

    return list(dict.fromkeys(match.group(0) for match in _INTERNAL_IDENTIFIER.finditer(text)))


def is_identifier_only_rewrite(original: str, revised: str) -> bool:
    """Accept only local replacements of internal labels, not prose rewrites.

    All non-identifier bytes of the reader-facing sentence must survive in the
    same order. This is a scope guard for Chair's lightweight readability edit;
    it is not a scientific-validity assessment.
    """

    matches = list(_INTERNAL_IDENTIFIER.finditer(original))
    if not matches or "\n" in revised:
        return False
    pieces: list[str] = []
    cursor = 0
    for match in matches:
        pieces.append(re.escape(original[cursor:match.start()]))
        pieces.append("(.*?)")
        cursor = match.end()
        # A copied machine ID often carries one separator space that is
        # unnatural after its reader-facing Chinese replacement.
        if cursor < len(original) and original[cursor] == " ":
            cursor += 1
    pieces.append(re.escape(original[cursor:]))
    result = re.fullmatch("".join(pieces), revised, flags=re.DOTALL)
    if result is None:
        return False
    return all(len(replacement) <= 100 for replacement in result.groups())
